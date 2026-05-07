from __future__ import annotations
import numpy as np
import pandas as pd

# pandas-ta has occasional numpy 2.x compat issues; we hand-roll the small set
# of indicators we need to avoid surprise breakage on Actions.


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def kd(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9, k_smooth: int = 3, d_smooth: int = 3):
    hh = high.rolling(n).max()
    ll = low.rolling(n).min()
    rsv = ((close - ll) / (hh - ll).replace(0, np.nan)) * 100
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False, min_periods=n).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False, min_periods=n).mean()
    return k, d


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Append indicators to a price DataFrame.

    Input must have columns: open, high, low, close, volume; index = date.
    Output adds: ma5/10/20/60/120/240, k, d, dif, dea, macd_hist, rsi14,
    atr14, vol_ma5, vol_ma20, vol_ratio, discount60.
    """
    if df.empty:
        return df
    out = df.sort_index().copy()

    for n in (5, 10, 20, 60, 120, 240):
        out[f"ma{n}"] = sma(out["close"], n)

    out["k"], out["d"] = kd(out["high"], out["low"], out["close"], 9, 3, 3)
    out["dif"], out["dea"], out["macd_hist"] = macd(out["close"], 12, 26, 9)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out["high"], out["low"], out["close"], 14)

    out["vol_ma5"] = sma(out["volume"], 5)
    out["vol_ma20"] = sma(out["volume"], 20)
    out["vol_ratio"] = out["volume"] / out["vol_ma5"]

    out["discount60"] = out["close"].shift(60)

    return out
