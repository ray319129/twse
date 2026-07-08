"""Vercel Python Serverless Function — 券商庫存截圖 → 持倉 JSON(Claude vision)。

POST /api/portfolio_ocr   body: {"image": "<base64>", "media_type": "image/jpeg"}

用途:使用者在網頁「我的持倉」上傳券商『庫存 / 即時未實現損益』畫面截圖,由 Claude
(Haiku 4.5 vision)抽出每一列持股,回傳與前端 portParse 相同的 {id,name,shares,cost,price}
陣列 → 前端進同一個「預覽表 → 逐列修正 → 匯入」流程,dedup 由前端 portUpsert(依代號)處理。

為何走 serverless 而非瀏覽器直呼 Claude:本站是 public 靜態頁,API key 放前端會被偷。
key 存 Vercel 環境變數 ANTHROPIC_API_KEY,只在伺服器端用。**誠實邊界**:截圖(含成本)會
一次性經過此函式 → Anthropic 辨識,不留存;成本數字最終仍只由前端存 localStorage,不寫任何檔。

成本:一張截圖約 1500 input + 300 output tokens;Haiku 4.5 約 US$0.003(不到 1 美分)。
"""
from __future__ import annotations
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

log = logging.getLogger("twse.portfolio_ocr")

_MODEL = "claude-haiku-4-5"          # OCR/抽取任務,便宜夠用;與 catalyst.py 同顆
_MAX_TOKENS = 1500
_MAX_BODY = 6 * 1024 * 1024          # 6MB 上限(前端已縮圖,正常遠低於此)

_SYSTEM = (
    "你是台股券商庫存截圖的資料抽取器。使用者上傳券商『庫存 / 即時未實現損益』畫面的截圖,"
    "請逐列抽出持股,只輸出 JSON,不要任何解釋或 markdown 圍欄。\n"
    "輸出格式:\n"
    '{"positions":[{"id":"股票代號","name":"股票名稱","shares":股數,"cost":每股成本均價,"price":現價}]}\n'
    "規則:\n"
    "- id:4~6 位數字代號(去掉括號、>> 等符號;代號常在名稱後的括號內,如『聯發科(2454)』→ 2454)。\n"
    "- name:股票名稱,不含代號與括號。\n"
    "- shares:股數(整數)。取『即時庫存 / 庫存 / 股數』欄位——那是股數不是張數,直接填該數字"
    "(例如『3』就是 3 股,不要 ×1000)。畫面若無此欄位填 null。\n"
    "- cost:每股成本『均價 / 成本均價』。**不要填『付出成本』(那是總額)**。無此欄位填 null。\n"
    "- price:現價 / 成交價 / 市價(去掉尾綴如 's')。無則 null。\n"
    "- 只抽個股/ETF 持股列;忽略『合計 / 總計 / 差異數』等彙總列與表頭。\n"
    "- 讀不到的欄位一律填 null,不要猜測數字。"
)
_USER = "這是我的券商庫存截圖,請抽出每一列持股成 JSON。"


def _extract_json(text: str) -> dict:
    """從模型回覆抽出 JSON 物件(容忍 ```json 圍欄或前後雜訊)。失敗回 {}。"""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return {}
    return {}


def _clean_positions(data: dict) -> list[dict]:
    import re
    out = []
    for p in (data.get("positions") or []):
        if not isinstance(p, dict):
            continue
        m = re.search(r"\d{4,6}", str(p.get("id") or p.get("name") or ""))
        if not m:
            continue
        def num(v):
            try:
                f = float(str(v).replace(",", ""))
                return f if f == f else None      # NaN → None
            except (TypeError, ValueError):
                return None
        name = re.sub(r"[（(]\s*\d{4,6}[A-Za-z]?\s*[）)]", "", str(p.get("name") or "")).strip()
        out.append({
            "id": m.group(0),
            "name": name,
            "shares": num(p.get("shares")),
            "cost": num(p.get("cost")),
            "price": num(p.get("price")),
        })
    return out


def recognize(image_b64: str, media_type: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "伺服器未設定 ANTHROPIC_API_KEY(需在 Vercel 環境變數加入)。"}
    if not image_b64:
        return {"error": "缺少 image(base64)。"}
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        media_type = "image/jpeg"
    try:
        import anthropic
    except ImportError:
        return {"error": "伺服器未安裝 anthropic 套件。"}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": _USER},
            ]}],
        )
    except Exception as e:
        log.warning(f"portfolio_ocr Claude 呼叫失敗:{e}")
        return {"error": f"辨識服務失敗:{e}"}

    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    positions = _clean_positions(_extract_json(text))
    return {"positions": positions}


class handler(BaseHTTPRequestHandler):
    """Vercel 慣例:檔名=路由(api/portfolio_ocr.py → /api/portfolio_ocr),匯出 handler。"""

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
            self._send_json(400, {"error": "請求為空或圖片過大(上限 6MB,請縮小截圖)。"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "請求格式錯誤(需 JSON:{image, media_type})。"})
            return
        try:
            result = recognize(payload.get("image") or "", payload.get("media_type") or "image/jpeg")
        except Exception as e:
            log.exception("portfolio_ocr 失敗")
            self._send_json(500, {"error": f"伺服器處理失敗:{e}"})
            return
        self._send_json(200 if "error" not in result else 502, result)
