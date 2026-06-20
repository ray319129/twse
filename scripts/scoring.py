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


def _g(cfg: dict, path: str, default):
    """讀巢狀 config(如 'rs.ratio_hi'),缺則回 default(= 現行硬寫值,確保不設 config 時行為不變)。"""
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def compute_conviction(df: pd.DataFrame, valuation: dict | None = None, *, cfg: dict | None = None) -> dict | None:
    """回傳評分 dict;資料不足或流動性過低回 None(直接淘汰,不進排序)。

    df 需是 compute_all()(+ compute_relative_strength())後的價格 DataFrame。
    所有門檻/權重皆可由 cfg(= config/screeners.yaml 的 scoring 區塊)覆寫;預設值等同原硬寫值。
    """
    cfg = cfg or {}
    min_len = int(_g(cfg, "min_history", 120))
    if df is None or len(df) < min_len:
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
    slope_lb = int(_g(cfg, "trend.ma60_slope_lookback", 21))
    ma60_past = df["ma60"].iloc[-slope_lb] if (len(df) >= slope_lb and "ma60" in df.columns) else np.nan

    # ---------- 流動性:日均成交金額 = 收盤 × 20 日均量。太低直接淘汰 ----------
    min_dollar = float(cfg.get("min_dollar_volume", _g(cfg, "liquidity.min_dollar_volume", 30_000_000)))
    span = float(_g(cfg, "liquidity.span", 50))
    dollar_vol = close * vol_ma20 if pd.notna(vol_ma20) else np.nan
    if pd.isna(dollar_vol) or dollar_vol < min_dollar:
        return None
    liquidity = _clip01(np.log10(dollar_vol / min_dollar) / np.log10(span))   # min→0,min×span→1

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
    rs_lo = float(_g(cfg, "rs.ratio_lo", 0.95)); rs_hi = float(_g(cfg, "rs.ratio_hi", 1.30))
    rs = 0.0
    if pd.notna(rs_ratio) and rs_hi > rs_lo:
        rs = _clip01((rs_ratio - rs_lo) / (rs_hi - rs_lo))
    rs_rising = False
    rs_rise_lb = int(_g(cfg, "rs.rising_lookback", 10))
    rs_blend = float(_g(cfg, "rs.rising_blend", 0.8))
    if "rs_line" in df.columns:
        rl = df["rs_line"].dropna()
        if len(rl) >= rs_rise_lb + 1:
            rs_rising = float(rl.iloc[-1]) > float(rl.iloc[-1 - rs_rise_lb])
            if rs_rising:
                rs = _clip01(rs * rs_blend + (1 - rs_blend))

    # ---------- 短線時機 / 量能 (0~1) ----------
    vr_lo = float(_g(cfg, "setup.vol_ratio_lo", 0.8)); vr_hi = float(_g(cfg, "setup.vol_ratio_hi", 2.0))
    vb_lo = float(_g(cfg, "setup.vbias_lo", 0.8)); vb_hi = float(_g(cfg, "setup.vbias_hi", 1.6))
    mom_lb = int(_g(cfg, "setup.mom_lookback", 10)); mom_full = float(_g(cfg, "setup.mom_full", 0.15))
    w_volx = float(_g(cfg, "setup.w_volx", 0.45)); w_vbias = float(_g(cfg, "setup.w_vbias", 0.25))
    w_mom = float(_g(cfg, "setup.w_mom", 0.30))
    volx = _clip01((vol_ratio - vr_lo) / (vr_hi - vr_lo)) if (pd.notna(vol_ratio) and vr_hi > vr_lo) else 0.0
    vbias = 0.0
    if pd.notna(vol_ma5) and pd.notna(vol_ma20) and vol_ma20 > 0 and vb_hi > vb_lo:
        vbias = _clip01((vol_ma5 / vol_ma20 - vb_lo) / (vb_hi - vb_lo))
    ret_mom = np.nan
    if len(df) >= mom_lb + 1:
        cN = df["close"].iloc[-1 - mom_lb]
        if pd.notna(cN) and cN > 0:
            ret_mom = close / cN - 1
    mom = _clip01(ret_mom / mom_full) if (pd.notna(ret_mom) and mom_full) else 0.0
    setup = w_volx * volx + w_vbias * vbias + w_mom * mom

    # ---------- 品質估值 (0~1) from 估值快照 ----------
    quality = float(_g(cfg, "quality.default", 0.5))   # 沒資料給中性
    pe_good = float(_g(cfg, "quality.pe_good", 25)); pe_ok = float(_g(cfg, "quality.pe_ok", 40))
    yld_full = float(_g(cfg, "quality.yield_full", 5.0))
    pb_good = float(_g(cfg, "quality.pb_good", 3)); pb_ok = float(_g(cfg, "quality.pb_ok", 6))
    if valuation:
        pe = valuation.get("pe"); yld = valuation.get("yield_pct"); pb = valuation.get("pb")
        q = []
        if pe is not None:
            q.append(1.0 if 0 < pe <= pe_good else 0.5 if 0 < pe <= pe_ok else 0.1 if pe > pe_ok else 0.0)
        if yld is not None:
            q.append(_clip01(yld / yld_full) if yld_full else 0.0)
        if pb is not None:
            q.append(1.0 if 0 < pb <= pb_good else 0.5 if 0 < pb <= pb_ok else 0.2)
        if q:
            quality = sum(q) / len(q)

    # ---------- 過熱懲罰 ----------
    ex_ret5 = float(_g(cfg, "exhausted.ret5_max", 0.22))
    ex_ext = float(_g(cfg, "exhausted.ext_ma20_max", 0.18))
    ex_rsi = float(_g(cfg, "exhausted.rsi_max", 88))
    ex_limit = float(_g(cfg, "exhausted.limit_up", 0.095))
    ex_penalty = float(_g(cfg, "exhausted.penalty", 0.55))
    ret5 = np.nan
    if len(df) >= 6:
        c5 = df["close"].iloc[-6]
        if pd.notna(c5) and c5 > 0:
            ret5 = close / c5 - 1
    ext_ma20 = (close / ma20 - 1) if (pd.notna(ma20) and ma20 > 0) else 0.0
    chg = (close / prev_close - 1) if (pd.notna(prev_close) and prev_close > 0) else 0.0
    limit_up_today = bool(chg >= ex_limit)
    exhausted = bool(
        (pd.notna(ret5) and ret5 > ex_ret5)
        or ext_ma20 > ex_ext
        or (pd.notna(rsi14) and rsi14 > ex_rsi)
        or limit_up_today
    )

    # ---------- 今天新鮮觸發(可進場) ----------
    bo_lb = int(_g(cfg, "trigger.breakout_lookback", 20))
    bo_vr = float(_g(cfg, "trigger.breakout_vol_ratio", 1.5))
    pb_near = float(_g(cfg, "trigger.pullback_near_ma20", 0.04))
    pb_tol = float(_g(cfg, "trigger.pullback_ma60_tol", 0.98))
    breakout = False
    if len(df) >= bo_lb:
        hi_n = df["close"].iloc[-bo_lb:].max()
        breakout = bool(
            close >= hi_n and pd.notna(vol_ratio) and vol_ratio >= bo_vr
            and pd.notna(open_) and close > open_
        )
    pullback_turn = False
    if pd.notna(ma20) and ma20 > 0 and pd.notna(ma60) and ma60 > 0 and pd.notna(ma60_past) and ma60 > ma60_past:
        near20 = abs(close - ma20) / ma20 <= pb_near
        above60 = close >= ma60 * pb_tol
        green = pd.notna(prev_close) and close > prev_close
        kd_up = pd.notna(k) and pd.notna(d) and k >= d
        pullback_turn = bool(near20 and above60 and (green or kd_up))
    trigger = bool((breakout or pullback_turn) and not exhausted)

    # ---------- 醞釀中(還沒觸發,但在蓄勢)→ 觀察層 ----------
    co_lb = int(_g(cfg, "brewing.coiling_lookback", 120))
    co_q = float(_g(cfg, "brewing.coiling_quantile", 0.20))
    co_abs = float(_g(cfg, "brewing.coiling_abs_width", 0.06))
    coiling = False
    if "bb_width" in df.columns:
        bw = df["bb_width"].dropna()
        if len(bw) >= 60:
            coiling = bool(float(bw.iloc[-1]) <= float(bw.tail(co_lb).quantile(co_q)) or float(bw.iloc[-1]) <= co_abs)
    brewing = bool(
        (not trigger) and (not exhausted)
        and (pd.notna(ma60) and close > ma60)
        and (coiling or rs_rising)
    )

    # ---------- 加總 ----------
    w_trend = float(_g(cfg, "weights.trend", 0.25)); w_rs = float(_g(cfg, "weights.rs", 0.25))
    w_setup = float(_g(cfg, "weights.setup", 0.25)); w_quality = float(_g(cfg, "weights.quality", 0.15))
    w_liq = float(_g(cfg, "weights.liquidity", 0.10))
    raw = 100.0 * (w_trend * trend + w_rs * rs + w_setup * setup + w_quality * quality + w_liq * liquidity)
    if exhausted:
        raw *= ex_penalty

    mom_margin = float(_g(cfg, "profile.momentum_margin", 0.30))
    q_min_q = float(_g(cfg, "profile.quality_min_q", 0.70))
    q_min_trend = float(_g(cfg, "profile.quality_min_trend", 0.60))
    if rs + setup > trend + quality + mom_margin:
        profile = "動能"
    elif quality >= q_min_q and trend >= q_min_trend:
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
