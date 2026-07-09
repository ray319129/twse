"""Vercel Python Serverless Function — 我的持倉「AI 總覽」。

POST /api/portfolio_review
body: {"task":"positions"|"overall", "positions":[...], "totals":{...}}

用途:一次讀完使用者所有持倉,逐檔整理重點 + 依該股「預期」(基本面/成長/估值/題材)判斷
何時停利、何時停損,並給整體組合總結。

**為何分兩種 task + 由前端分塊呼叫(2026-07-10 重構)**:Vercel Hobby 方案 serverless 硬上限
60 秒,且 anthropic SDK 的 `timeout` 是「讀取逾時」(只要持續有 token 進來就一直重置),**無法**
當成整體 wall-clock 上限。實測:單次一口氣分析 ~10 檔(即使 Sonnet)產出時間會超過 60s → 平台
回 504 FUNCTION_INVOCATION_TIMEOUT 非 JSON 錯誤頁 → 前端 JSON.parse 失敗。故改成:
  - task="positions":只分析『前端丟進來的這一小批(≤_MAX_PER_CALL 檔)』,只回 positions[]。
    前端把持倉切成小塊(每塊 4 檔)平行呼叫,每次都遠低於 60s。
  - task="overall":吃『所有檔的精簡摘要 + 各檔已算好的 verdict』,只回一段 overall 總結,輸出短、快。
每一次 HTTP 呼叫都小而快,徹底避開 60s 硬砍。

**誠實邊界(同 portfolio_ocr)**:持倉(含成本)會一次性經此函式 → Anthropic 分析,不留存;
成本最終仍只由前端 localStorage 保存,不寫任何檔、不進 GitHub、不進信件。

**為何純 messages.create + 自己 parse JSON(不用 output_config)**:本專案 Vercel 上安裝的
anthropic 版本對 output_config 結構化輸出參數會丟例外(見 HANDOFF 2026-07-09),OCR/ai_summary
用純呼叫都正常,故統一純呼叫,用 scripts.utils.extract_json 穩健抽 JSON。

模型:claude-sonnet-4-6(速度優先以吃住 60s 限制;結構化綜合判斷品質仍足)。
成本:on-demand,分塊後每次數百~千 output token,Sonnet $3/$15 每百萬,整份約 US$0.02~0.08。
"""
from __future__ import annotations
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

log = logging.getLogger("twse.portfolio_review")

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS_POS = 3500             # 逐檔任務:≤_MAX_PER_CALL 檔的逐檔 JSON
_MAX_TOKENS_OVERALL = 1400         # 總結任務:一段 overall,短
_MAX_BODY = 4 * 1024 * 1024
_MAX_PER_CALL = 6                  # 單次逐檔任務上限(前端以 4 分塊,這裡留餘裕)
_MAX_OVERALL = 15                  # 總結最多納入檔數
_SDK_TIMEOUT = 55.0

_VERDICTS = {"續抱", "加碼", "減碼", "停利了結", "停損", "觀望"}

_SYS_POSITIONS = (
    "你是台股資深投資組合顧問。我會給你使用者持倉中的『一批』個股,每檔含成本均價、現價、"
    "未實現損益%,以及該檔『個股健檢』的七面向分數(財務體質/成長能力/估值分析/風險分析/技術面/"
    "籌碼分析/新聞分析)、關鍵指標數值、短線評估、AI 摘要優缺點與新聞摘要。請為『每一檔』做完整分析。\n"
    "每檔要判斷:\n"
    "- verdict:一詞定調,只能是【續抱/加碼/減碼/停利了結/停損/觀望】其中之一。\n"
    "- outlook:該股的『預期』——綜合成長能力/財務體質、估值(是否已貴、還有多少空間)、新聞題材,"
    "判斷往上空間或往下風險,2~3 句。\n"
    "- key_points:3~5 條最重要的重點(涵蓋基本面、估值、技術、籌碼、新聞裡最關鍵訊號)。\n"
    "- take_profit:何時『停利』——**依該股預期**給條件與/或參考價位並說明理由"
    "(例:估值達歷史高位百分位、成長趨緩、題材兌現、RSI 過熱、外資轉賣)。\n"
    "- stop_loss:何時『停損』——條件與/或參考價位 + 理由"
    "(例:跌破關鍵均線/前低、營收年增轉負、風險面重大訊號;系統預設硬停損為成本 −7%,可參考但依該股波動與體質調整)。\n"
    "- pnl_note:目前損益處境的一句短評。\n"
    "重要:只依提供的數據判斷,不得杜撰不存在的數字或指標;所有價位標『參考』;繁體中文;"
    "結尾不做獲利保證,本質為研究參考非投資建議。\n"
    "只輸出一個 JSON 物件,不要 markdown 圍欄、不要任何解釋文字。格式:\n"
    '{"positions":[{"id":"代號","verdict":"續抱/加碼/減碼/停利了結/停損/觀望",'
    '"outlook":"","key_points":["",""],"take_profit":"","stop_loss":"","pnl_note":""}]}'
)

_SYS_OVERALL = (
    "你是台股資深投資組合顧問。我會給你使用者的整體持倉:總成本/總市值/總損益,以及每檔的"
    "代號、名稱、產業、未實現損益%、市值、健檢總分與七面向分數、以及該檔已判定的操作定調 verdict。"
    "請只輸出『整體組合』的總結,不需逐檔重述。\n"
    "overall 要有:summary(組合體質與損益總評,2~4 句)、health(整體體質一句話)、"
    "concentration_risk(部位集中度/產業曝險提醒,請依市值算出最大持股佔比與主要產業曝險)、"
    "action_priority(依急迫性列出最該處理的 1~3 件事)。\n"
    "只依提供數據判斷,不杜撰;繁體中文;非投資建議。\n"
    "只輸出一個 JSON 物件,不要 markdown 圍欄、不要任何解釋。格式:\n"
    '{"overall":{"summary":"","health":"","concentration_risk":"","action_priority":["",""]}}'
)


def _clean_in_positions(raw: list, limit: int) -> list:
    out = []
    for p in raw or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        out.append({
            "id": str(p.get("id"))[:8],
            "name": str(p.get("name") or "")[:20],
            "industry": str(p.get("industry") or "")[:20],
            "shares": p.get("shares"), "cost": p.get("cost"), "price": p.get("price"),
            "pnl_pct": p.get("pnl_pct"), "market_value": p.get("market_value"),
            "verdict": (str(p.get("verdict") or "") if p.get("verdict") in _VERDICTS else ""),
            "health": p.get("health"),
        })
    out.sort(key=lambda x: (x.get("market_value") or 0), reverse=True)
    return out[:limit]


def _call(system: str, user: str, max_tokens: int):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=_SDK_TIMEOUT)
    resp = client.messages.create(model=_MODEL, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    from scripts.utils import extract_json
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    return extract_json(text), getattr(resp, "stop_reason", None), text


def _clean_pos_item(it: dict) -> "dict | None":
    if not isinstance(it, dict) or not it.get("id"):
        return None
    v = str(it.get("verdict") or "").strip()
    return {
        "id": str(it.get("id"))[:8],
        "verdict": v if v in _VERDICTS else "觀望",
        "outlook": str(it.get("outlook") or "")[:600],
        "key_points": [str(x)[:200] for x in (it.get("key_points") or []) if str(x).strip()][:6],
        "take_profit": str(it.get("take_profit") or "")[:500],
        "stop_loss": str(it.get("stop_loss") or "")[:500],
        "pnl_note": str(it.get("pnl_note") or "")[:300],
    }


def review(payload: dict) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "伺服器未設定 ANTHROPIC_API_KEY(需在 Vercel 環境變數加入)。"}
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"error": "伺服器未安裝 anthropic 套件。"}

    task = payload.get("task") or "positions"

    if task == "overall":
        positions = _clean_in_positions(payload.get("positions"), _MAX_OVERALL)
        if not positions:
            return {"error": "沒有可分析的持倉。"}
        user = json.dumps({"totals": payload.get("totals") or {}, "positions": positions}, ensure_ascii=False)
        try:
            data, stop, text = _call(_SYS_OVERALL, user, _MAX_TOKENS_OVERALL)
        except Exception as e:
            log.warning(f"portfolio_review[overall] 失敗:{type(e).__name__}: {e}")
            return {"error": f"整體總結失敗:{e}"}
        ov = (data or {}).get("overall")
        if not isinstance(ov, dict):
            log.warning(f"portfolio_review[overall] 解析失敗:stop={stop}, head={text[:120]!r}")
            return {"error": "整體總結解析失敗,請稍後再試。"}
        return {"overall": {
            "summary": str(ov.get("summary") or "")[:800],
            "health": str(ov.get("health") or "")[:400],
            "concentration_risk": str(ov.get("concentration_risk") or "")[:500],
            "action_priority": [str(x)[:200] for x in (ov.get("action_priority") or []) if str(x).strip()][:4],
        }}

    # task == "positions":只分析這一小批
    positions = _clean_in_positions(payload.get("positions"), _MAX_PER_CALL)
    if not positions:
        return {"error": "沒有可分析的持倉。"}
    user = json.dumps({"positions": positions}, ensure_ascii=False)
    try:
        data, stop, text = _call(_SYS_POSITIONS, user, _MAX_TOKENS_POS)
    except Exception as e:
        log.warning(f"portfolio_review[positions] 失敗:{type(e).__name__}: {e}")
        return {"error": f"AI 分析失敗:{e}"}
    if not data or not isinstance(data.get("positions"), list):
        log.warning(f"portfolio_review[positions] 解析失敗:stop={stop}, len={len(text)}, head={text[:160]!r}")
        return {"error": "AI 回應解析失敗,請稍後再試。"}
    out = [x for x in (_clean_pos_item(it) for it in data["positions"]) if x]
    return {"positions": out}


class handler(BaseHTTPRequestHandler):
    """Vercel 慣例:檔名=路由(api/portfolio_review.py → /api/portfolio_review),匯出 handler。"""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")   # 含成本,絕不快取
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > _MAX_BODY:
            self._send_json(400, {"error": "請求為空或過大。"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "請求格式錯誤(需 JSON)。"})
            return
        try:
            result = review(payload)
        except Exception as e:
            log.exception("portfolio_review 失敗")
            self._send_json(500, {"error": f"伺服器處理失敗:{e}"})
            return
        self._send_json(200 if "error" not in result else 502, result)
