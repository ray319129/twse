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


def bbands(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std()
    return mid - k * std, mid, mid + k * std


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Append indicators to a price DataFrame.

    Input must have columns: open, high, low, close, volume; index = date.
    """
    if df.empty:
        return df
    out = df.sort_index().copy()

    # ---------- 雙軌價格(P2-D):指標吃「還原價」(adj_close 去除息假跳空),raw OHLC 另存供漲停/停損/顯示 ----------
    # close_raw 永遠保留原始成交價;若 adj_close 覆蓋率夠高,整段等比例縮放 OHLC 成還原價後再算指標,
    # 除息旺季的自然下跳就不會污染均線/動能/RSI/KD/布林。覆蓋率不足(舊快取只有部分列有 adj_close)時
    # 整體退回原始價,避免「一半還原一半原始」的接縫斷層(比不還原更糟)。
    out["close_raw"] = out["close"]
    if "adj_close" in out.columns and len(out) and out["adj_close"].notna().mean() >= 0.95:
        factor = (out["adj_close"] / out["close"].replace(0, np.nan)).ffill().bfill().fillna(1.0)
        out["open"] = out["open"] * factor
        out["high"] = out["high"] * factor
        out["low"] = out["low"] * factor
        out["close"] = out["adj_close"]

    for n in (5, 10, 20, 60, 120, 240):
        out[f"ma{n}"] = sma(out["close"], n)

    out["k"], out["d"] = kd(out["high"], out["low"], out["close"], 9, 3, 3)
    out["dif"], out["dea"], out["macd_hist"] = macd(out["close"], 12, 26, 9)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out["high"], out["low"], out["close"], 14)

    out["bb_lower"], out["bb_mid"], out["bb_upper"] = bbands(out["close"], 20, 2.0)
    # 布林帶寬度(相對中軌的百分比);越小 = 波動越收斂 = 越可能在盤底蓄勢。
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"].replace(0, np.nan)

    out["vol_ma5"] = sma(out["volume"], 5)
    out["vol_ma20"] = sma(out["volume"], 20)
    # 量比分母用「前 5 日均量」(shift(1),不含今日)。若含今日,爆量會稀釋自己的分母,
    # 系統性低估爆量:真實 3 倍量只顯示 2.14、真實 2 倍顯示 1.67,且數學上限被壓到 5.0。
    # vol_ma5 / vol_ma20 兩條均量本身維持含今日(coiling 量縮、setup vbias 用比值,對稱不受影響)。
    out["vol_ma5_prev"] = out["vol_ma5"].shift(1)
    out["vol_ratio"] = out["volume"] / out["vol_ma5_prev"]

    out["discount60"] = out["close"].shift(60)

    return out


def compute_relative_strength(df: pd.DataFrame, index_close: pd.Series, n: int = 60) -> pd.DataFrame:
    """Append relative-strength-vs-index columns to a price DataFrame.

    rs_line   = 個股收盤 / 大盤收盤(Mansfield 式相對強弱線,看趨勢方向)
    rs_ratio  = 個股 n 日報酬 / 大盤 n 日報酬(>1 代表這段期間贏大盤)

    領先邏輯:資金輪動時,下一段主流通常先在「相對強度轉強」上露出馬腳,
    股價創新高之前,rs 往往已經先創高。大盤回檔時還能維持 rs>1 的更是領頭羊。
    """
    if df.empty or index_close is None or len(index_close) == 0:
        return df
    out = df.copy()
    idx = pd.to_numeric(index_close, errors="coerce").reindex(out.index).ffill()
    out["rs_line"] = out["close"] / idx.replace(0, np.nan)
    stock_ret = out["close"] / out["close"].shift(n)
    idx_ret = (idx / idx.shift(n)).replace(0, np.nan)
    out["rs_ratio"] = stock_ret / idx_ret
    return out


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14):
    """ADX / +DI / -DI(Wilder's smoothing,風格比照本檔其餘指標用 ewm(alpha=1/n) 近似)。
    個股健檢 Technical Engine 用,既有 compute_all() 短線評分管線不需要這個,故不塞進
    compute_all() 輸出欄位(避免影響既有呼叫端的欄位假設),呼叫端各自取用。
    回傳 (plus_di, minus_di, adx) 三條 Series。"""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_n = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_n.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_n.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx_val = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return plus_di, minus_di, adx_val


def reference_levels(df: pd.DataFrame, atr_mult: float = 2.0) -> dict:
    """Snapshot of technical reference levels for the latest bar.
    Returns a dict of value-or-None for each level. Distances ('% from close')
    are returned as float (None if undefined). NOT a buy/sell signal — just
    coordinates: where the price sits relative to common reference points.
    """
    if df.empty:
        return {}
    last = df.iloc[-1]
    close = float(last["close"]) if pd.notna(last["close"]) else None
    if close is None:
        return {}

    def pct_from(level):
        if level is None or pd.isna(level) or close == 0:
            return None
        return round((close - float(level)) / close * 100, 2)

    levels: dict = {"close": close}

    for k in ("ma5", "ma20", "ma60", "ma120", "ma240"):
        v = last.get(k)
        levels[k] = float(v) if pd.notna(v) else None
        levels[f"{k}_diff_pct"] = pct_from(levels[k])

    if len(df) >= 60:
        levels["high_60"] = float(df["close"].tail(60).max())
        levels["low_60"] = float(df["close"].tail(60).min())
    if len(df) >= 20:
        levels["high_20"] = float(df["close"].tail(20).max())
        levels["low_20"] = float(df["close"].tail(20).min())

    bbl = last.get("bb_lower"); bbu = last.get("bb_upper")
    levels["bb_lower"] = float(bbl) if pd.notna(bbl) else None
    levels["bb_upper"] = float(bbu) if pd.notna(bbu) else None

    a = last.get("atr14")
    if pd.notna(a):
        atr_v = float(a)
        levels["atr14"] = atr_v
        levels["stop_2atr"] = round(close - atr_mult * atr_v, 2)
        levels["stop_2atr_pct"] = round(-atr_mult * atr_v / close * 100, 2)
    return levels
