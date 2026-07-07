"""Vercel Python Serverless Function — 個股詳情頁資料(任意代號)。

GET /api/detail?stock=2330[&days=250]

對應規劃 [[twse-stock-detail-page]] 的 MVP:回傳前端 ECharts 畫「K線+量+MA + 三大法人長條」
所需的時間序列。刻意獨立於 api/health.py(健檢面向卡),不把圖表序列塞進 health payload —
兩者關注點不同、payload 大小也不同,分開比較好維護。

設計跟 api/health.py 一致:
- serverless 無持久磁碟,每次現抓現算。
- 價格走 yfinance(還原股價/分K 免費),FinMind 只用免費 dataset(此處=三大法人買賣超)。
- ThreadPoolExecutor 平行抓,壓低總延遲。
- 所有數字過 _json_safe(NaN→null,本專案最常見雷)。

回傳 schema(MVP):
{
  "stock_id", "name", "industry", "market",
  "kline":  [[date, open, high, low, close, volume], ...],   # 日期升冪
  "ma":     {"ma5": [...], "ma20": [...], "ma60": [...]},    # 與 kline 等長,對齊;NaN→null
  "inst":   [[date, foreign, invest, dealer], ...]           # 三大法人「淨買賣(張)」,可能較 kline 短
}
"""
from __future__ import annotations
import json
import logging
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

log = logging.getLogger("twse.detail_api")

# 圖表回看的交易日上限(綁 payload 大小)。yfinance period 是日曆天,取多一點再裁。
_MAX_BARS_DEFAULT = 250
_CAL_DAYS_FETCH = 400        # ~270 交易日,足夠算 ma60 且裁到 250 根仍滿


def _json_safe(o):
    import numpy as np
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, np.floating):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    return o


def _resolve_stock(stock_id: str):
    """回傳 (market, name, industry);查不到用合理預設續跑(清單只用來判上市/上櫃與名稱,非關鍵)。
    與 api/health.py._resolve_stock 同策略。"""
    try:
        from scripts.fetchers import fetch_stock_info
        info = fetch_stock_info()
        if info is not None and not info.empty:
            row = info[info["stock_id"] == stock_id]
            if not row.empty:
                r = row.iloc[0]
                return (
                    str(r.get("type") or "twse"),
                    str(r.get("stock_name") or stock_id),
                    str(r.get("industry_category") or ""),
                )
    except Exception as e:
        log.warning(f"_resolve_stock 失敗,改用預設值續跑:{e}")
    return ("twse", stock_id, "")


def _round(x, nd=2):
    try:
        f = float(x)
        return round(f, nd) if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def compute_detail(stock_id: str, max_bars: int = _MAX_BARS_DEFAULT) -> dict:
    import pandas as pd
    from scripts.fetchers import fetch_price_history, _fetch_institutional
    from scripts.indicators import compute_all

    stock_id = (stock_id or "").strip()
    if not stock_id.isdigit() or len(stock_id) != 4:
        return {"error": f"股票代號格式錯誤:「{stock_id}」需為4位數字(例如 2330)"}

    market, name, industry = _resolve_stock(stock_id)
    today = date.today()
    start = today - timedelta(days=_CAL_DAYS_FETCH)

    # 平行:價格(yfinance)+ 三大法人(FinMind)。序列抓容易撞 serverless timeout。
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_price = ex.submit(fetch_price_history, stock_id, market, _CAL_DAYS_FETCH)
        f_inst = ex.submit(_fetch_institutional, stock_id, start, today)
        price_df = f_price.result()
        inst_df = f_inst.result()

    if price_df is None or price_df.empty or len(price_df) < 60:
        return {"error": f"{stock_id} 價格資料不足(< 60 根 K 棒)。可能是錯誤代號、剛上市不久、"
                          f"或資料來源暫時失敗,可稍後再試。"}

    price_df = compute_all(price_df)
    if len(price_df) > max_bars:
        price_df = price_df.iloc[-max_bars:]

    dates = [d.strftime("%Y-%m-%d") for d in price_df.index]
    kline = []
    for d, row in zip(dates, price_df.itertuples(index=False)):
        kline.append([
            d,
            _round(getattr(row, "open", None)),
            _round(getattr(row, "high", None)),
            _round(getattr(row, "low", None)),
            _round(getattr(row, "close", None)),
            _round(getattr(row, "volume", None), 0),
        ])

    def ma_series(col):
        if col not in price_df.columns:
            return [None] * len(price_df)
        return [_round(v) for v in price_df[col].tolist()]

    ma = {"ma5": ma_series("ma5"), "ma20": ma_series("ma20"), "ma60": ma_series("ma60")}

    # 三大法人:_fetch_institutional 回 index=date、欄 inst_foreign/inst_invest/inst_dealer(淨股數)。
    # 轉「淨買賣張數」(股/1000)並裁到與 kline 同一時間窗。抓不到就給空陣列(前端該面板不畫)。
    inst = []
    if inst_df is not None and not inst_df.empty:
        lo = price_df.index.min()
        sub = inst_df[inst_df.index >= lo]
        for idx, r in sub.iterrows():
            inst.append([
                idx.strftime("%Y-%m-%d"),
                _round(r.get("inst_foreign", 0) / 1000, 0),
                _round(r.get("inst_invest", 0) / 1000, 0),
                _round(r.get("inst_dealer", 0) / 1000, 0),
            ])

    return {
        "stock_id": stock_id, "name": name, "industry": industry, "market": market,
        "kline": kline, "ma": ma, "inst": inst,
    }


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime 慣例入口:檔名=路由(api/detail.py → /api/detail),
    匯出名為 handler 的 BaseHTTPRequestHandler 子類別。"""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(_json_safe(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        # 詳情圖表變動慢(收盤後才更新),給短 CDN 快取降 FinMind 額度壓力。
        self.send_header("Cache-Control", "public, max-age=1800")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        stock_id = (qs.get("stock") or [""])[0].strip()
        if not stock_id:
            self._send_json(400, {"error": "缺少 stock 參數,例如 /api/detail?stock=2330"})
            return
        try:
            max_bars = int((qs.get("days") or [_MAX_BARS_DEFAULT])[0])
        except (ValueError, TypeError):
            max_bars = _MAX_BARS_DEFAULT
        max_bars = max(60, min(max_bars, 500))
        try:
            result = compute_detail(stock_id, max_bars=max_bars)
        except Exception as e:
            log.exception(f"compute_detail({stock_id}) 失敗")
            self._send_json(500, {"error": f"伺服器計算失敗:{e}"})
            return
        status = 200 if "error" not in result else 404
        self._send_json(status, result)
