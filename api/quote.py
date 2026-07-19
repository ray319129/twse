"""Vercel Serverless — 即時報價(讓網頁真的即時,2026-07-19)。

GET /api/quote?ids=2330,2317,6669

## 為什麼要有這支

在這支之前,**網頁完全不即時**:它讀的是批次 commit 上去的靜態 JSON
(data.json / premarket.json / alerts.json),所以永遠是上一次批次的快照。
持倉頁的價格是點進去才現抓 yfinance(延遲報價,而且慢),使用者實測要等 5~15 秒。

這支改用 `scripts/quotes.get_quotes()` —— 也就是 FinMind Sponsor 全市場快照
(2852 檔 / **一次呼叫** / 0.7 秒),所以查 1 檔跟查 50 檔的成本一樣。
前端可以每 N 秒輪詢一次,整頁報價一起換。

## 三段降級照舊(鐵則二)

回傳每筆都帶 `source`(sponsor / mis / close)與 `ts`,前端必須顯示是即時還是昨收。
訂閱到期後這支不會壞,只是 source 變成 close —— 網頁會自己標示出來。

## 額度

一次呼叫換整份全市場,所以成本跟前端要幾檔無關。
`quotes.fetch_snapshot_all` 內建 TTL 快取,同一個 serverless 實例在 TTL 內重複呼叫不會再打 API。
"""
from __future__ import annotations
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MAX_IDS = 200          # 一次最多查幾檔:防止有人用超長 query 把 payload 撐爆


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            raw = (qs.get("ids") or [""])[0]
            ids = [s.strip() for s in raw.split(",") if s.strip()][:MAX_IDS]
            if not ids:
                return self._send(400, {"error": "缺少 ids 參數,例:/api/quote?ids=2330,2317"})

            from scripts.quotes import get_quotes, market_snapshot_source
            quotes = get_quotes(ids)
            src = market_snapshot_source()
            payload = {
                "quotes": {sid: q.to_dict() for sid, q in quotes.items()},
                # 鐵則二:資料源與訂閱狀態一起回,前端才有東西可以標示
                "source": src.get("source"),
                "source_label": src.get("label"),
                "sponsor_days_left": (src.get("sponsor") or {}).get("days_left"),
            }
            self._send(200, payload)
        except Exception as e:
            # 即時報價掛掉不該讓整頁壞掉;前端收到 error 就沿用靜態資料
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 盤中報價不該被 CDN 快取久;5 秒足以擋住連點,又不會讓數字凍住
        self.send_header("Cache-Control", "public, max-age=5")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
