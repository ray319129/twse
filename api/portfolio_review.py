"""Vercel Python Serverless Function — 我的持倉「AI 總覽」。

POST /api/portfolio_review
body: {"positions":[{id,name,industry,shares,cost,price,pnl_pct,market_value,health:{...digest...}}],
       "totals":{cost,market,pnl,pnl_pct}}

用途:一次讀完使用者所有持倉,逐檔整理重點 + 依該股「預期」(基本面/成長/估值/題材)判斷
何時停利、何時停損,並給整體組合總結。每檔的健檢七面向分數與關鍵指標由前端從既有
/api/health(單一事實來源的健檢引擎)萃取成 digest 後帶進來,這支只負責 AI 綜合判讀,
不重複抓資料(避免 serverless timeout)。

**誠實邊界(同 portfolio_ocr)**:持倉(含成本)會一次性經此函式 → Anthropic 分析,不留存;
成本最終仍只由前端 localStorage 保存,不寫任何檔、不進 GitHub、不進信件。

**為何純 messages.create + 自己 parse JSON(不用 output_config)**:本專案 Vercel 上安裝的
anthropic 版本對 output_config 結構化輸出參數會丟例外(見 HANDOFF 2026-07-09),OCR/ai_summary
用純呼叫都正常,故統一純呼叫,用 scripts.utils.extract_json 穩健抽 JSON。

模型:claude-sonnet-4-6。原用 Opus 4.8(判斷品質優先),但 2026-07-10 實測使用者真實持倉
會超過 Vercel Hobby 60s 上限 → 平台回傳非 JSON 錯誤頁、前端 JSON.parse 失敗。Sonnet 產出速度
約 2x,判斷品質對此結構化綜合任務仍足,故改用。Hobby 60s 硬限:SDK timeout 設 50s、分析檔數
上限 15(超過只分析市值前 15 檔),讓超時能回乾淨 JSON 錯誤而非被平台硬砍。
成本:on-demand,一次約數千 output token,Sonnet $3/$15 每百萬,單次約 US$0.02~0.08。
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

_MODEL = "claude-sonnet-4-6"       # 見檔頭:Opus 對真實持倉會逾時,Sonnet ~2x 快、品質仍足
_MAX_TOKENS = 8000
_MAX_BODY = 4 * 1024 * 1024        # 4MB(digest 已壓縮,15 檔遠小於此)
_MAX_POSITIONS = 15                # 只分析市值前 N 檔,控 output 長度 / 60s serverless 逾時
_SDK_TIMEOUT = 50.0                # 低於 Vercel Hobby 60s 上限 → 逾時得到乾淨 JSON 錯誤而非平台硬砍

_SYSTEM = (
    "你是台股資深投資組合顧問。我會給你使用者的完整持倉:每檔含成本均價、現價、未實現損益%,"
    "以及該檔『個股健檢』的七面向分數(財務體質/成長能力/估值分析/風險分析/技術面/籌碼分析/新聞分析)、"
    "關鍵指標數值、短線評估(swing)、AI 摘要優缺點與新聞摘要。請據此為『每一檔』做完整分析,"
    "並給整體組合總結。\n"
    "每檔要判斷:\n"
    "- verdict:一詞定調,只能是【續抱/加碼/減碼/停利了結/停損/觀望】其中之一。\n"
    "- outlook:該股的『預期』——綜合成長能力/財務體質(基本面動能)、估值分析(是否已貴、還有多少空間)、"
    "新聞題材,判斷往上的空間或往下的風險,2~3 句。\n"
    "- key_points:3~5 條最重要的重點(要完整,涵蓋基本面、估值、技術、籌碼、新聞裡最關鍵的訊號)。\n"
    "- take_profit:何時『停利』——**依該股預期**給條件與/或參考價位並說明理由"
    "(例:估值已達歷史高位百分位、成長趨緩、題材兌現、RSI 過熱、外資轉賣;可用『現價 +X% 或跌破月線二選一』式條件)。\n"
    "- stop_loss:何時『停損』——給條件與/或參考價位 + 理由"
    "(例:跌破關鍵均線/前低、基本面轉壞如營收年增轉負、風險面出現重大訊號;系統預設硬停損為成本 −7%,可參考但要依該股波動與體質調整)。\n"
    "- pnl_note:目前損益處境的一句短評(已獲利宜守成/已虧損檢視續抱理由 等)。\n"
    "整體 overall 要有:summary(組合體質與損益總評)、concentration_risk(部位集中度/產業曝險提醒)、"
    "action_priority(依急迫性列出最該處理的 1~3 件事)。\n"
    "重要規則:只依我提供的數據判斷,不得杜撰不存在的數字或指標;所有價位一律標示為『參考』;"
    "用繁體中文;客觀中性,結尾不做獲利保證,本質為研究參考非投資建議。\n"
    "只輸出一個 JSON 物件,不要 markdown 圍欄、不要任何解釋文字。格式:\n"
    '{"overall":{"summary":"","health":"","concentration_risk":"","action_priority":["",""]},'
    '"positions":[{"id":"代號","verdict":"續抱/加碼/減碼/停利了結/停損/觀望",'
    '"outlook":"","key_points":["",""],"take_profit":"","stop_loss":"","pnl_note":""}]}'
)


def _sanitize_positions(positions: list) -> list:
    """只保留必要欄位、依市值排序取前 N 檔,避免 payload 過大/output 過長逾時。"""
    clean = []
    for p in positions or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        clean.append({
            "id": str(p.get("id"))[:8],
            "name": str(p.get("name") or "")[:20],
            "industry": str(p.get("industry") or "")[:20],
            "shares": p.get("shares"),
            "cost": p.get("cost"),
            "price": p.get("price"),
            "pnl_pct": p.get("pnl_pct"),
            "market_value": p.get("market_value"),
            "health": p.get("health"),      # 前端萃取好的 digest(dict 或 None)
        })
    clean.sort(key=lambda x: (x.get("market_value") or 0), reverse=True)
    return clean[:_MAX_POSITIONS]


def review(payload: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "伺服器未設定 ANTHROPIC_API_KEY(需在 Vercel 環境變數加入)。"}
    submitted = sum(1 for p in (payload.get("positions") or [])
                    if isinstance(p, dict) and p.get("id"))
    positions = _sanitize_positions(payload.get("positions"))
    if not positions:
        return {"error": "沒有可分析的持倉。"}
    try:
        import anthropic
    except ImportError:
        return {"error": "伺服器未安裝 anthropic 套件。"}

    user = json.dumps({"totals": payload.get("totals") or {}, "positions": positions},
                      ensure_ascii=False)
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=_SDK_TIMEOUT)
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        log.warning(f"portfolio_review Claude 呼叫失敗:{type(e).__name__}: {e}")
        return {"error": f"AI 分析失敗:{e}"}

    from scripts.utils import extract_json
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    data = extract_json(text)
    if not data or not isinstance(data.get("positions"), list):
        stop = getattr(resp, "stop_reason", None)
        log.warning(f"portfolio_review JSON 解析失敗:stop_reason={stop}, len={len(text)}, head={text[:160]!r}")
        return {"error": "AI 回應解析失敗,請稍後再試(可能分析檔數過多被截斷,建議精簡持倉後重試)。"}

    # 清洗:verdict 限白名單、欄位轉字串、list 欄位保底
    allowed = {"續抱", "加碼", "減碼", "停利了結", "停損", "觀望"}
    out_pos = []
    for it in data.get("positions") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        v = str(it.get("verdict") or "").strip()
        out_pos.append({
            "id": str(it.get("id"))[:8],
            "verdict": v if v in allowed else "觀望",
            "outlook": str(it.get("outlook") or "")[:600],
            "key_points": [str(x)[:200] for x in (it.get("key_points") or []) if str(x).strip()][:6],
            "take_profit": str(it.get("take_profit") or "")[:500],
            "stop_loss": str(it.get("stop_loss") or "")[:500],
            "pnl_note": str(it.get("pnl_note") or "")[:300],
        })
    ov = data.get("overall") or {}
    overall = {
        "summary": str(ov.get("summary") or "")[:800],
        "health": str(ov.get("health") or "")[:400],
        "concentration_risk": str(ov.get("concentration_risk") or "")[:500],
        "action_priority": [str(x)[:200] for x in (ov.get("action_priority") or []) if str(x).strip()][:4],
    }
    return {"overall": overall, "positions": out_pos, "analyzed": len(out_pos), "submitted": submitted}


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
