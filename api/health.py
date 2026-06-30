"""Vercel Python Serverless Function — 個股健檢即時查詢(任意代號)。

GET /api/health?stock=2330

對應健檢設計第1節「路徑 B」:批次路徑(scripts/main.py daily_run)只對核心+自選池算,
寫 docs/health/{代號}.json;這支處理「使用者臨時起意查的任何代號」,共用
scripts/health/engine.py 同一套財務公式(單一事實來源,不會批次一套、即時一套)。

跟批次路徑的關鍵差異:
- 沒有本地 parquet 累積可吃(serverless 無持久磁碟跨呼叫保存),每次都是「現抓現算」的完整窗口查詢。
- storage.py 的寫入已加防護(_try_write_parquet,見 2026-06-30 commit):唯讀檔案系統寫入失敗
  只記錄警告,不會讓整個請求 500。
- 平行抓取(ThreadPoolExecutor)壓低總延遲,但無法保證任何情況都在 timeout 內完成
  (查從未入榜過的冷門股、FinMind 當下變慢時風險較高)。

部署需求(見專案根目錄 VERCEL_SETUP.md):
- Vercel 環境變數 FINMIND_TOKEN(必要)、ANTHROPIC_API_KEY(選用,沒設 AI 相關面向自動降級)。
- vercel.json 已設定 maxDuration;若部署方案上限更低,長尾查詢可能逾時,前端已有逾時文案。
- bundle size 風險:reuse 既有 scripts/ 程式碼(含 yfinance),需在實際部署時確認不超過
  Vercel serverless function 的 unzipped size 上限。若超過,可考慮在這支檔案內改用
  FinMind 的 TaiwanStockPrice 資料集取代 fetch_price_history()/fetch_index_history()
  (兩者都走 yfinance),换掉最重的相依套件;本檔先不做這個替換,避免批次/即時兩條路徑
  的價格資料來源不一致,等實測真的卡 size 限制再評估。
"""
from __future__ import annotations
import json
import logging
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

log = logging.getLogger("twse.health_api")


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
    """回傳 (market, name, industry);查不到時用合理預設(twse/代號本身/空產業)續跑,
    不要讓「股票清單抓取失敗」擋掉整次健檢(清單只是用來判斷上市/上櫃與名稱,非必要關鍵資料)。"""
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


def compute_live_health(stock_id: str) -> dict:
    import pandas as pd
    from scripts.fetchers import (
        fetch_price_history, fetch_index_history, fetch_news,
        fetch_valuation_snapshot, fetch_valuation_snapshot_tpex,
    )
    from scripts.indicators import compute_all, compute_relative_strength
    from scripts.storage import load_chips, load_revenue
    from scripts.config import load_screeners
    from scripts.health import engine as health_engine

    stock_id = (stock_id or "").strip()
    if not stock_id.isdigit() or len(stock_id) != 4:
        return {"error": f"股票代號格式錯誤:「{stock_id}」需為4位數字(例如 2330)"}

    market, name, industry = _resolve_stock(stock_id)
    today = date.today()
    cfg = load_screeners() or {}
    health_cfg = cfg.get("health", {}) or {}
    if not health_cfg.get("enabled", True):
        return {"error": "個股健檢目前在 config/screeners.yaml 被關閉(health.enabled=false)"}

    # 平行抓取,壓低總延遲(serverless 有 timeout,序列抓 6~8 支 API 容易逾時)。
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_price = ex.submit(fetch_price_history, stock_id, market, 500)
        f_index = ex.submit(fetch_index_history, 400)
        f_news = ex.submit(fetch_news, stock_id, name, 60)
        f_val_twse = ex.submit(fetch_valuation_snapshot, today)
        f_val_tpex = ex.submit(fetch_valuation_snapshot_tpex)
        price_df = f_price.result()
        index_df = f_index.result()
        news_items = f_news.result()
        val_twse = f_val_twse.result() or {}
        val_tpex = f_val_tpex.result() or {}

    if price_df is None or price_df.empty or len(price_df) < 60:
        return {"error": f"{stock_id} 價格資料不足(< 60 根 K 棒)。可能是錯誤代號、剛上市不久、"
                          f"或資料來源暫時失敗,可稍後再試。"}

    price_df = compute_all(price_df)
    index_close = index_df["close"] if (index_df is not None and not index_df.empty) else None
    if index_close is not None:
        price_df = compute_relative_strength(price_df, index_close, n=60)

    valuation = {**val_tpex, **val_twse}.get(stock_id, {})
    last_close = price_df["close"].iloc[-1]
    current_price = float(last_close) if pd.notna(last_close) else None

    ctx = health_engine.build_ctx_batch(
        stock_id=stock_id, name=name, industry=industry, today=today,
        price_df=price_df, valuation_snapshot=valuation, current_price=current_price,
        revenue_df=load_revenue(stock_id), chips_df=load_chips(stock_id),
        news_items=news_items, health_cfg=health_cfg,
    )
    ctx["live"] = True
    result = health_engine.compute_stock_health(ctx, health_cfg=health_cfg)
    return result


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime 慣例入口:檔名 = 路由(api/health.py → /api/health),
    匯出名為 handler 的 BaseHTTPRequestHandler 子類別。"""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(_json_safe(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
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
            self._send_json(400, {"error": "缺少 stock 參數,例如 /api/health?stock=2330"})
            return
        try:
            result = compute_live_health(stock_id)
        except Exception as e:
            log.exception(f"compute_live_health({stock_id}) 失敗")
            self._send_json(500, {"error": f"伺服器計算失敗:{e}"})
            return
        status = 200 if "error" not in result else 404
        self._send_json(status, result)
