from __future__ import annotations
import time
import urllib.parse
from datetime import date, datetime, timedelta
import pandas as pd
import feedparser
import yfinance as yf

from .config import FINMIND_TOKEN, META_DIR
from .utils import http_get_json, log, chunked

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"


# ---------- FinMind ----------

def fetch_finmind(dataset: str, **params) -> list[dict]:
    payload = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
    j = http_get_json(FINMIND_API, params=payload, retries=2, delay=2.0)
    if isinstance(j, dict) and "data" in j:
        return j["data"]
    log.warning(f"FinMind {dataset} unexpected response keys: {list(j.keys()) if isinstance(j, dict) else type(j)}")
    return []


def fetch_stock_info(force: bool = False) -> pd.DataFrame:
    """List of all listed (TWSE) and OTC (TPEX) stocks, with name and industry.
    Cached on disk; refresh once a month.
    """
    cache = META_DIR / "stock_info.parquet"
    if cache.exists() and not force:
        try:
            df = pd.read_parquet(cache)
            mtime = datetime.fromtimestamp(cache.stat().st_mtime)
            if (datetime.now() - mtime).days < 25:
                return df
        except Exception as e:
            log.warning(f"stock_info cache read failed: {e}")

    rows = fetch_finmind("TaiwanStockInfo")
    if not rows:
        if cache.exists():
            log.warning("FinMind stock_info empty; using stale cache")
            return pd.read_parquet(cache)
        raise RuntimeError("Cannot fetch TaiwanStockInfo and no cache available")

    df = pd.DataFrame(rows)
    # FinMind returns columns: industry_category, stock_id, stock_name, type, date
    # type: 'twse' = 上市, 'tpex' = 上櫃
    df.to_parquet(cache, index=False)
    return df


def filter_tradable_stocks(df: pd.DataFrame) -> pd.DataFrame:
    """Keep 4-digit numeric codes only (excludes ETFs starting with 00, warrants, etc.)."""
    out = df.copy()
    out = out[out["stock_id"].str.match(r"^\d{4}$")]
    out = out[~out["stock_id"].str.startswith("00")]
    if "type" in out.columns:
        out = out[out["type"].isin(["twse", "tpex"])]
    out = out.drop_duplicates(subset=["stock_id"]).reset_index(drop=True)
    return out


# ---------- yfinance (price history) ----------

def yf_ticker(stock_id: str, market: str) -> str:
    suffix = ".TW" if market == "twse" else ".TWO"
    return f"{stock_id}{suffix}"


def fetch_price_history(stock_id: str, market: str, days: int = 400) -> pd.DataFrame:
    """Single-stock OHLCV via yfinance. Returns DataFrame indexed by date with
    columns: open, high, low, close, volume. Empty DataFrame on failure.
    """
    ticker = yf_ticker(stock_id, market)
    try:
        df = yf.download(
            ticker,
            period=f"{days}d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as e:
        log.warning(f"yfinance {ticker} failed: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
    })
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df.index.name = "date"
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def bulk_fetch_history(stocks: list[tuple[str, str]], days: int = 400, sleep: float = 0.3) -> dict[str, pd.DataFrame]:
    """Fetch history for many (stock_id, market) pairs. Returns {stock_id: df}."""
    out: dict[str, pd.DataFrame] = {}
    for i, (sid, mkt) in enumerate(stocks):
        df = fetch_price_history(sid, mkt, days=days)
        if not df.empty:
            out[sid] = df
        if (i + 1) % 50 == 0:
            log.info(f"history fetched {i+1}/{len(stocks)}")
        time.sleep(sleep)
    return out


# ---------- Google News RSS ----------

def fetch_news(stock_id: str, name: str, limit: int = 10) -> list[dict]:
    q = urllib.parse.quote(f"{stock_id} {name}")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning(f"news {stock_id} failed: {e}")
        return []
    items = []
    for e in feed.entries[:limit]:
        items.append({
            "title": getattr(e, "title", ""),
            "link": getattr(e, "link", ""),
            "published": getattr(e, "published", ""),
            "source": getattr(getattr(e, "source", None), "title", ""),
        })
    return items
