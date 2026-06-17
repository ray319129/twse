from __future__ import annotations
import numpy as np
import pandas as pd

"""短線(隔日沖 / 隔週 / 月內)信心評分。

核心理念:不再「符合條件就全列出」,而是用一個 0~100 的信心總分把全市場排序,
只取最高分的少數。所有計算只用「免費資料」(價格指標 + TWSE 估值快照),
不打 FinMind,排序完才對 Top N 補抓籌碼/財報 → 不會打爆 API 額度。

總分 = 趨勢健康 25 + 相對強度 25 + 短線時機/量能 25 + 品質估值 15 + 流動性 10
並對「已經漲過頭」(連續大漲、爆量乖離、漲停、RSI 過熱)重罰,避免追到已噴出的股票。
"""


def _v(row, col, default=np.nan):
    if col not in row.index:
        return default
    v = row.get(col)
    return v if pd.notna(v) else default


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def compute_conviction(df: pd.DataFrame, valuation: dict | None = None, *, cfg: dict | None = None) -> dict | None:
    """回傳評分 dict;資料不足或流動性過低回 None(直接淘汰,不進排序)。

    df 需是 compute_all()(+ compute_relative_strength())後的價格 DataFrame。
    """
    cfg = cfg or {}
    if df is None or len(df) < 120:
        return None
    last = df.iloc[-1]
    close = _v(last, "close")
    if pd.isna(close) or close <= 0:
        return None

    ma5 = _v(last, "ma5"); ma20 = _v(last, "ma20"); ma60 = _v(last, "ma60"); ma120 = _v(last, "ma120")
    vol_ma5 = _v(last, "vol_ma5"); vol_ma20 = _v(last, "vol_ma20"); vol_ratio = _v(last, "vol_ratio")
    rsi14 = _v(last, "rsi14"); k = _v(last, "k"); d = _v(last, "d")
    rs_ratio = _v(last, "rs_ratio")
    open_ = _v(last, "open")
    prev_close = _v(df.iloc[-2], "close") if len(df) >= 2 else np.nan
    ma60_past = df["ma60"].iloc[-21] if (len(df) >= 21 and "ma60" in df.columns) else np.nan

    # ---------- 流動性:日均成交金額 = 收盤 × 20 日均量。太低直接淘汰 ----------
    min_dollar = float(cfg.get("min_dollar_volume", 30_000_000))   # 3000 萬元
    dollar_vol = close * vol_ma20 if pd.notna(vol_ma20) else np.nan
    if pd.isna(dollar_vol) or dollar_vol < min_dollar:
        return None
    # 3000萬→0,約 15 億→1
    liquidity = _clip01(np.log10(dollar_vol / min_dollar) / np.log10(50))

    # ---------- 趨勢健康 (0~1) ----------
    trend_bits = [
        pd.notna(ma5) and close > ma5,
        pd.notna(ma20) and pd.notna(ma60) and ma20 > ma60,
        pd.notna(ma60) and close > ma60,
        pd.notna(ma60) and pd.notna(ma120) and ma60 > ma120,
        pd.notna(ma60) and pd.notna(ma60_past) and ma60 > ma60_past,
    ]
    trend = sum(1 for b in trend_bits if b) / len(trend_bits)

    # ---------- 相對強度 (0~1) ----------
    rs = 0.0
    if pd.notna(rs_ratio):
        rs = _clip01((rs_ratio - 0.95) / (1.30 - 0.95))   # 0.95→0, 1.30→1
    rs_rising = False
    if "rs_line" in df.columns:
        rl = df["rs_line"].dropna()
        if len(rl) >= 11:
            rs_rising = float(rl.iloc[-1]) > float(rl.iloc[-11])
            if rs_rising:
                rs = _clip01(rs * 0.8 + 0.2)

    # ---------- 短線時機 / 量能 (0~1) ----------
    volx = _clip01((vol_ratio - 0.8) / (2.0 - 0.8)) if pd.notna(vol_ratio) else 0.0
    vbias = 0.0
    if pd.notna(vol_ma5) and pd.notna(vol_ma20) and vol_ma20 > 0:
        vbias = _clip01((vol_ma5 / vol_ma20 - 0.8) / (1.6 - 0.8))
    ret10 = np.nan
    if len(df) >= 11:
        c10 = df["close"].iloc[-11]
        if pd.notna(c10) and c10 > 0:
            ret10 = close / c10 - 1
    mom = _clip01(ret10 / 0.15) if pd.notna(ret10) else 0.0   # 10 日 +15% → 1
    setup = 0.45 * volx + 0.25 * vbias + 0.30 * mom

    # ---------- 品質估值 (0~1) from 估值快照 ----------
    quality = 0.5   # 沒資料給中性
    if valuation:
        pe = valuation.get("pe"); yld = valuation.get("yield_pct"); pb = valuation.get("pb")
        q = []
        if pe is not None:
            q.append(1.0 if 0 < pe <= 25 else 0.5 if 0 < pe <= 40 else 0.1 if pe > 40 else 0.0)
        if yld is not None:
            q.append(_clip01(yld / 5.0))
        if pb is not None:
            q.append(1.0 if 0 < pb <= 3 else 0.5 if 0 < pb <= 6 else 0.2)
        if q:
            quality = sum(q) / len(q)

    # ---------- 過熱懲罰 ----------
    ret5 = np.nan
    if len(df) >= 6:
        c5 = df["close"].iloc[-6]
        if pd.notna(c5) and c5 > 0:
            ret5 = close / c5 - 1
    ext_ma20 = (close / ma20 - 1) if (pd.notna(ma20) and ma20 > 0) else 0.0
    chg = (close / prev_close - 1) if (pd.notna(prev_close) and prev_close > 0) else 0.0
    limit_up_today = chg >= 0.095
    exhausted = bool(
        (pd.notna(ret5) and ret5 > 0.22)        # 5 日漲 > 22%
        or ext_ma20 > 0.18                       # 乖離 20MA > 18%
        or (pd.notna(rsi14) and rsi14 > 88)
        or limit_up_today
    )

    # ---------- 今天新鮮觸發(可進場) ----------
    breakout = False
    if len(df) >= 20:
        hi20 = df["close"].iloc[-20:].max()
        breakout = bool(
            close >= hi20 and pd.notna(vol_ratio) and vol_ratio >= 1.5
            and pd.notna(open_) and close > open_
        )
    pullback_turn = False
    if pd.notna(ma20) and ma20 > 0 and pd.notna(ma60) and ma60 > 0 and pd.notna(ma60_past) and ma60 > ma60_past:
        near20 = abs(close - ma20) / ma20 <= 0.04
        above60 = close >= ma60 * 0.98
        green = pd.notna(prev_close) and close > prev_close
        kd_up = pd.notna(k) and pd.notna(d) and k >= d
        pullback_turn = bool(near20 and above60 and (green or kd_up))
    trigger = bool((breakout or pullback_turn) and not exhausted)

    # ---------- 醞釀中(還沒觸發,但在蓄勢)→ 觀察層 ----------
    coiling = False
    if "bb_width" in df.columns:
        bw = df["bb_width"].dropna()
        if len(bw) >= 60:
            coiling = bool(float(bw.iloc[-1]) <= float(bw.tail(120).quantile(0.20)) or float(bw.iloc[-1]) <= 0.06)
    brewing = bool(
        (not trigger) and (not exhausted)
        and (pd.notna(ma60) and close > ma60)
        and (coiling or rs_rising)
    )

    # ---------- 加總 ----------
    raw = 100.0 * (0.25 * trend + 0.25 * rs + 0.25 * setup + 0.15 * quality + 0.10 * liquidity)
    if exhausted:
        raw *= 0.55

    if rs + setup > trend + quality + 0.30:
        profile = "動能"
    elif quality >= 0.70 and trend >= 0.60:
        profile = "品質"
    else:
        profile = "均衡"

    ret20 = None
    if len(df) >= 21:
        c20 = df["close"].iloc[-21]
        if pd.notna(c20) and c20 > 0:
            ret20 = round((close / c20 - 1) * 100, 1)

    return {
        "score": round(raw, 1),
        "trend": round(trend, 2), "rs": round(rs, 2), "setup": round(setup, 2),
        "quality": round(quality, 2), "liquidity": round(liquidity, 2),
        "exhausted": exhausted, "trigger": trigger, "brewing": brewing,
        "breakout": breakout, "pullback_turn": pullback_turn, "limit_up_today": limit_up_today,
        "profile": profile,
        "ret5_pct": round(float(ret5) * 100, 1) if pd.notna(ret5) else None,
        "ret20_pct": ret20,
        "dollar_vol_m": round(dollar_vol / 1e6, 0),
    }
