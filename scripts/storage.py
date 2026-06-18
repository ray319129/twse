from __future__ import annotations
import pandas as pd
from pathlib import Path
from .config import PRICES_DIR, DATA_DIR

CHIPS_DIR = DATA_DIR / "chips"
REVENUE_DIR = DATA_DIR / "revenue"
EPS_DIR = DATA_DIR / "eps"
PER_DIR = DATA_DIR / "per"
for _d in (CHIPS_DIR, REVENUE_DIR, EPS_DIR, PER_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.name != "date":
        if "date" in df.columns:
            df = df.set_index("date")
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.sort_index()


def _upsert(path: Path, new_df: pd.DataFrame, loader) -> pd.DataFrame:
    if new_df.empty:
        return loader(path)
    cur = loader(path)
    new_df = _normalize_index(new_df.copy())
    if cur.empty:
        new_df.to_parquet(path)
        return new_df
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(path)
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
        new_df.to_parquet(revenue_path(stock_id))
        return new_df
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(revenue_path(stock_id))
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
