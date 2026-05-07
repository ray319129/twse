from __future__ import annotations
import logging as _logging
import time
import urllib.parse
from datetime import date, datetime, timedelta
import pandas as pd
import feedparser
import yfinance as yf

from .config import FINMIND_TOKEN, META_DIR
from .utils import http_get_json, log, chunked

# yfinance prints its own ERROR-level "possibly delisted" lines for every miss.
# We already retry + log via our own helpers, so suppress yfinance's noise.
_logging.getLogger("yfinance").setLevel(_logging.CRITICAL)

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"

# Datasets that returned 4xx during this run; future calls short-circuit.
_DEAD_DATASETS: set[str] = set()


# ---------- FinMind ----------

def fetch_finmind(dataset: str, **params) -> list[dict]:
    """Resilient FinMind GET. Returns [] on any failure (never raises).
    402/404/400 short-circuits future calls to the same dataset for this run.
    """
    if dataset in _DEAD_DATASETS:
        return []
    payload = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
    try:
        j = http_get_json(FINMIND_API, params=payload, retries=2, delay=2.0)
    except Exception as e:
        msg = str(e)
        if any(code in msg for code in ("400", "402", "404")):
            _DEAD_DATASETS.add(dataset)
            log.warning(f"FinMind {dataset} 不可用(免費版受限或名稱失效);本 run 後續跳過。")
        else:
            log.warning(f"FinMind {dataset} fetch error: {e}")
        return []
    if isinstance(j, dict) and "data" in j:
        return j["data"]
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
    """Keep 4-digit numeric codes only.
    Excludes:
    - 00xx: ETFs / 槓反 ETF
    - 91xx: TDR (Taiwan Depository Receipts), yfinance 沒收錄
    """
    out = df.copy()
    out = out[out["stock_id"].str.match(r"^\d{4}$")]
    out = out[~out["stock_id"].str.startswith("00")]
    out = out[~out["stock_id"].str.startswith("91")]
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


# ---------- FinMind chips (daily institutional + margin + foreign holding) ----------

def _fetch_institutional(stock_id: str, start: date, end: date) -> pd.DataFrame:
    rows = fetch_finmind(
        "TaiwanStockInstitutionalInvestorsBuySell",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if not {"buy", "sell", "name"}.issubset(df.columns):
        return pd.DataFrame()
    df["net"] = df["buy"].fillna(0) - df["sell"].fillna(0)

    def bucket(n: str) -> str:
        if not isinstance(n, str):
            return ""
        s = n.lower()
        if "foreign" in s: return "foreign"
        if "investment" in s or "trust" in s: return "invest"
        if "dealer" in s: return "dealer"
        return ""

    df["bucket"] = df["name"].apply(bucket)
    df = df[df["bucket"] != ""]
    if df.empty:
        return pd.DataFrame()
    pivot = df.pivot_table(index="date", columns="bucket", values="net", aggfunc="sum", fill_value=0)
    pivot = pivot.rename(columns={c: f"inst_{c}" for c in pivot.columns})
    for col in ("inst_foreign", "inst_invest", "inst_dealer"):
        if col not in pivot.columns:
            pivot[col] = 0
    pivot["inst_total"] = pivot[["inst_foreign", "inst_invest", "inst_dealer"]].sum(axis=1)
    pivot.index = pd.to_datetime(pivot.index).tz_localize(None).normalize()
    return pivot[["inst_foreign", "inst_invest", "inst_dealer", "inst_total"]].sort_index()


def _fetch_margin(stock_id: str, start: date, end: date) -> pd.DataFrame:
    rows = fetch_finmind(
        "TaiwanStockMarginPurchaseShortSale",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    margin_col = next((c for c in ("MarginPurchaseTodayBalance", "MarginBalance") if c in df.columns), None)
    short_col = next((c for c in ("ShortSaleTodayBalance", "ShortBalance") if c in df.columns), None)
    if not margin_col and not short_col:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    if margin_col:
        out["margin_balance"] = df[margin_col].astype(float)
    if short_col:
        out["short_balance"] = df[short_col].astype(float)
    out = out.set_index("date").sort_index()
    return out


def _fetch_holding(stock_id: str, start: date, end: date) -> pd.DataFrame:
    """Foreign holding ratio. FinMind has renamed/restructured this several
    times; we try a few dataset+column combos, return empty if none works.
    """
    candidate_columns = (
        "ForeignInvestmentSharesRatio", "ForeignInvestmentRemainRatio",
        "ForeignInvestmentRatio", "HoldingSharesPer", "PercentageHeld",
        "ForeignInvestmentSharesPer", "Foreign_Investment_Ratio",
    )
    for ds in ("TaiwanStockShareholding", "TaiwanStockHoldingSharesPer"):
        rows = fetch_finmind(
            ds, data_id=stock_id,
            start_date=start.isoformat(), end_date=end.isoformat(),
        )
        if not rows:
            continue
        df = pd.DataFrame(rows)
        col = next((c for c in candidate_columns if c in df.columns), None)
        if not col:
            continue
        out = pd.DataFrame()
        out["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        out["foreign_holding_pct"] = pd.to_numeric(df[col], errors="coerce")
        out = out.dropna().set_index("date").sort_index()
        if not out.empty:
            return out
    return pd.DataFrame()


def fetch_chips_history(stock_id: str, start: date, end: date) -> pd.DataFrame:
    """Daily chips: institutional + margin + foreign holding. Empty if all sources fail."""
    parts = []
    for fn in (_fetch_institutional, _fetch_margin, _fetch_holding):
        try:
            df = fn(stock_id, start, end)
        except Exception as e:
            log.warning(f"chips fetch {fn.__name__} {stock_id} failed: {e}")
            df = pd.DataFrame()
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = parts[0]
    for p in parts[1:]:
        out = out.join(p, how="outer")
    return out.sort_index()


# Back-compat alias used by older main.py / storage paths
fetch_institutional_history = fetch_chips_history


# ---------- FinMind fundamentals (monthly revenue, EPS, PER/yield) ----------

def fetch_monthly_revenue(stock_id: str, months: int = 18) -> pd.DataFrame:
    """Monthly revenue history with YoY computed.
    Returns DataFrame indexed by year_month (string YYYY-MM), columns: revenue, revenue_yoy.
    """
    end = date.today()
    start = (end.replace(day=1) - timedelta(days=months * 32))
    rows = fetch_finmind(
        "TaiwanStockMonthRevenue",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "revenue" not in df.columns:
        return pd.DataFrame()
    if "revenue_year" in df.columns and "revenue_month" in df.columns:
        df["ym"] = df["revenue_year"].astype(int).astype(str).str.zfill(4) + "-" + \
                   df["revenue_month"].astype(int).astype(str).str.zfill(2)
    else:
        df["ym"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    df = df.drop_duplicates(subset=["ym"]).sort_values("ym")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df = df.dropna(subset=["revenue"])
    df = df.set_index("ym")[["revenue"]]
    df["revenue_yoy"] = df["revenue"].pct_change(periods=12)
    return df


def fetch_eps_quarterly(stock_id: str, quarters: int = 6) -> pd.DataFrame:
    """Quarterly EPS history. Returns DataFrame indexed by quarter_end date, column: eps."""
    end = date.today()
    start = end - timedelta(days=quarters * 100)
    rows = fetch_finmind(
        "TaiwanStockFinancialStatements",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if not {"type", "value", "date"}.issubset(df.columns):
        return pd.DataFrame()
    eps_keys = {"EPS", "EarningsPerShare", "EPS_Quarter", "BasicEPS", "EarningsPerShareBasic"}
    eps = df[df["type"].isin(eps_keys)].copy()
    if eps.empty:
        return pd.DataFrame()
    eps["value"] = pd.to_numeric(eps["value"], errors="coerce")
    eps = eps.dropna(subset=["value"])
    eps = eps.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(eps["date"]).dt.tz_localize(None).dt.normalize()
    out["eps"] = eps["value"].values
    return out.set_index("date").sort_index()


def fetch_per_yield(stock_id: str, days: int = 10) -> pd.DataFrame:
    """Recent days of PER / dividend yield / PBR. Returns DataFrame indexed by date."""
    end = date.today()
    start = end - timedelta(days=days * 2)
    rows = fetch_finmind(
        "TaiwanStockPER",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    for src, dst in (("PER", "pe"), ("PBR", "pb"), ("dividend_yield", "yield_pct")):
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    return out.set_index("date").sort_index().dropna(how="all")


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
