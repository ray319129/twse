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

_LEVELS = None         # data/levels.parquet 隨 repo 部署,serverless 讀本機檔不打 API


def _levels():
    """盤前算好的均線/前高/產業別/流通股數/營收/EPS。讀一次快取在實例裡。"""
    global _LEVELS
    if _LEVELS is None:
        try:
            import pandas as pd
            p = os.path.join(_ROOT, "data", "levels.parquet")
            df = pd.read_parquet(p)
            _LEVELS = {str(r["stock_id"]): r for r in df.to_dict("records")}
        except Exception:
            _LEVELS = {}
    return _LEVELS


def _n(v):
    """NaN / None → None(NaN 在 Python 是 truthy,是本專案最常見的雷)。"""
    try:
        import math
        if v is None:
            return None
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


def _enrich(ids, quotes, snap):
    """把快照原始欄位 + levels 靜態欄位 + 衍生指標組成前端要的完整報價。
    衍生的每一項都標明算法,不做黑盒子:
      委買賣比 = 委買量 ÷ (委買量+委賣量)   ← 是「掛單」失衡,不是內外盤
      換手率   = 成交張數×1000 ÷ 流通股數
      是否突破 = 現價 > 20日高 × 1.003(與盤中掃描同一條規則)
      回測五日線 = |現價/MA5 − 1| ≤ 1.5% 且現價 ≥ MA5×0.985
    """
    raw = {}
    if snap is not None and not getattr(snap, "empty", True):
        want = set(ids)
        for r in snap[snap["stock_id"].isin(want)].to_dict("records"):
            raw[str(r.get("stock_id"))] = r
    lv = _levels()
    out = {}
    for sid in ids:
        q = quotes.get(sid)
        d = q.to_dict() if q else {"stock_id": sid}
        r, L = raw.get(sid, {}), lv.get(sid, {})
        px = _n(d.get("price"))
        bv, sv = _n(r.get("buy_volume")), _n(r.get("sell_volume"))
        shares, tv = _n(L.get("shares")), _n(r.get("total_volume"))
        h20, ma5 = _n(L.get("high20")), _n(L.get("ma5"))
        d.update({
            "change_price": _n(r.get("change_price")),        # 漲跌
            "tick_volume": _n(r.get("volume")),               # 單量(最後一筆)
            "total_volume": _n(r.get("total_volume")),        # 總量(張)
            "total_amount": _n(r.get("total_amount")),        # 成交金額(元)
            "bid_volume": bv, "ask_volume": sv,               # 委買/委賣量
            "bid_ask_ratio": round(bv / (bv + sv), 3) if (bv and sv and bv + sv) else None,
            "turnover_rate": round(tv * 1000 / shares * 100, 3) if (tv and shares) else None,
            "industry": L.get("industry"),
            "revenue_yoy": _n(L.get("revenue_yoy")),
            "eps_ttm": _n(L.get("eps_ttm")),
            "eps_last": _n(L.get("eps_last")),
            "ma5": ma5, "ma20": _n(L.get("ma20")), "high20": h20,
            "breakout": bool(px and h20 and px > h20 * 1.003),
            "at_ma5": bool(px and ma5 and abs(px / ma5 - 1) <= 0.015 and px >= ma5 * 0.985),
        })
        out[sid] = d
    return out


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            raw = (qs.get("ids") or [""])[0]
            ids = [s.strip() for s in raw.split(",") if s.strip()][:MAX_IDS]
            if not ids:
                return self._send(400, {"error": "缺少 ids 參數,例:/api/quote?ids=2330,2317"})

            from scripts.quotes import get_quotes, market_snapshot_source, fetch_snapshot_all
            quotes = get_quotes(ids)
            src = market_snapshot_source()
            rich = _enrich(ids, quotes, fetch_snapshot_all())
            payload = {
                "quotes": rich,
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
