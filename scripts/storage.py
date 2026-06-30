from __future__ import annotations
import pandas as pd
from pathlib import Path
from .config import PRICES_DIR, DATA_DIR

CHIPS_DIR = DATA_DIR / "chips"
REVENUE_DIR = DATA_DIR / "revenue"
EPS_DIR = DATA_DIR / "eps"
PER_DIR = DATA_DIR / "per"
FINANCIALS_DIR = DATA_DIR / "financials"
BALANCE_DIR = DATA_DIR / "balance"
CASHFLOW_DIR = DATA_DIR / "cashflow"
for _d in (CHIPS_DIR, REVENUE_DIR, EPS_DIR, PER_DIR, FINANCIALS_DIR, BALANCE_DIR, CASHFLOW_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.name != "date":
        if "date" in df.columns:
            df = df.set_index("date")
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.sort_index()


def _try_write_parquet(df: pd.DataFrame, path: Path) -> None:
    """寫入失敗(例如 Vercel serverless 唯讀檔案系統,個股健檢即時查詢路徑會踩到)
    只記錄、不拋例外 —— 呼叫端永遠拿得到記憶體內算好的 DataFrame,只是這次沒能落地快取。"""
    try:
        df.to_parquet(path)
    except Exception as e:
        import logging
        logging.getLogger("twse").warning(f"parquet 寫入失敗(唯讀檔案系統?略過快取):{path.name}: {e}")


def _upsert(path: Path, new_df: pd.DataFrame, loader) -> pd.DataFrame:
    if new_df.empty:
        return loader(path)
    cur = loader(path)
    new_df = _normalize_index(new_df.copy())
    if cur.empty:
        _try_write_parquet(new_df, path)
        return new_df
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    _try_write_parquet(combined, path)
    return combined


def _load_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return _normalize_index(pd.read_parquet(path))


# Prices

def price_path(stock_id: str) -> Path:
    return PRICES_DIR / f"{stock_id}.parquet"


def load_prices(stock_id: str) -> pd.DataFrame:
    df = _load_parquet(price_path(stock_id))
    # 安全網:忽略收盤為 NaN 的壞 K 棒(yfinance 偶爾寫入未定收盤),否則均線/評分全毀
    if not df.empty and "close" in df.columns:
        df = df[df["close"].notna()]
    return df


def save_prices(stock_id: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    out = _normalize_index(df.copy())
    out.to_parquet(price_path(stock_id))


def upsert_prices(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(price_path(stock_id), new_df, _load_parquet)


# Chips (institutional + margin + foreign holding history per stock)

def chips_path(stock_id: str) -> Path:
    return CHIPS_DIR / f"{stock_id}.parquet"


def load_chips(stock_id: str) -> pd.DataFrame:
    return _load_parquet(chips_path(stock_id))


def upsert_chips(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(chips_path(stock_id), new_df, _load_parquet)


# Monthly revenue (index = "YYYY-MM" string)

def revenue_path(stock_id: str) -> Path:
    return REVENUE_DIR / f"{stock_id}.parquet"


def load_revenue(stock_id: str) -> pd.DataFrame:
    p = revenue_path(stock_id)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    if df.index.name != "ym" and "ym" in df.columns:
        df = df.set_index("ym")
    return df.sort_index()


def upsert_revenue(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    if new_df.empty:
        return load_revenue(stock_id)
    cur = load_revenue(stock_id)
    new_df = new_df.copy()
    if cur.empty:
        _try_write_parquet(new_df, revenue_path(stock_id))
        return new_df
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    _try_write_parquet(combined, revenue_path(stock_id))
    return combined


# Quarterly EPS

def eps_path(stock_id: str) -> Path:
    return EPS_DIR / f"{stock_id}.parquet"


def load_eps(stock_id: str) -> pd.DataFrame:
    return _load_parquet(eps_path(stock_id))


def upsert_eps(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(eps_path(stock_id), new_df, _load_parquet)


# PER / dividend yield / PB

def per_path(stock_id: str) -> Path:
    return PER_DIR / f"{stock_id}.parquet"


def load_per(stock_id: str) -> pd.DataFrame:
    return _load_parquet(per_path(stock_id))


def upsert_per(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(per_path(stock_id), new_df, _load_parquet)


# Quarterly fundamentals: financial statements / balance sheet / cash flow (index = quarter-end date)

def financials_path(stock_id: str) -> Path:
    return FINANCIALS_DIR / f"{stock_id}.parquet"


def load_financials(stock_id: str) -> pd.DataFrame:
    return _load_parquet(financials_path(stock_id))


def upsert_financials(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(financials_path(stock_id), new_df, _load_parquet)


def balance_path(stock_id: str) -> Path:
    return BALANCE_DIR / f"{stock_id}.parquet"


def load_balance(stock_id: str) -> pd.DataFrame:
    return _load_parquet(balance_path(stock_id))


def upsert_balance(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(balance_path(stock_id), new_df, _load_parquet)


def cashflow_path(stock_id: str) -> Path:
    return CASHFLOW_DIR / f"{stock_id}.parquet"


def load_cashflow(stock_id: str) -> pd.DataFrame:
    return _load_parquet(cashflow_path(stock_id))


def upsert_cashflow(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(cashflow_path(stock_id), new_df, _load_parquet)
