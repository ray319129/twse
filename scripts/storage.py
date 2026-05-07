from __future__ import annotations
import pandas as pd
from pathlib import Path
from .config import PRICES_DIR


def price_path(stock_id: str) -> Path:
    return PRICES_DIR / f"{stock_id}.parquet"


def load_prices(stock_id: str) -> pd.DataFrame:
    p = price_path(stock_id)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    if df.index.name != "date":
        if "date" in df.columns:
            df = df.set_index("date")
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.sort_index()


def save_prices(stock_id: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    out = df.copy()
    if out.index.name != "date":
        out.index.name = "date"
    out.to_parquet(price_path(stock_id))


def upsert_prices(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    if new_df.empty:
        return load_prices(stock_id)
    cur = load_prices(stock_id)
    if cur.empty:
        save_prices(stock_id, new_df)
        return new_df
    new_df = new_df.copy()
    new_df.index = pd.to_datetime(new_df.index).tz_localize(None).normalize()
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    save_prices(stock_id, combined)
    return combined
