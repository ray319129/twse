from __future__ import annotations
import pandas as pd
import numpy as np


def _last(df: pd.DataFrame, col: str, default=np.nan):
    if col not in df.columns or df.empty:
        return default
    val = df[col].iloc[-1]
    return val if pd.notna(val) else default


def _prev(df: pd.DataFrame, col: str, default=np.nan):
    if col not in df.columns or len(df) < 2:
        return default
    val = df[col].iloc[-2]
    return val if pd.notna(val) else default


def evaluate_stock(
    df: pd.DataFrame,
    cfg: dict,
    *,
    chips_df: pd.DataFrame | None = None,
    revenue_df: pd.DataFrame | None = None,
    eps_df: pd.DataFrame | None = None,
    per_df: pd.DataFrame | None = None,
) -> dict:
    """Evaluate all enabled strategies for one stock. Missing data → strategy False."""
    hits: dict[str, bool] = {}
    if df.empty or len(df) < 5:
        return hits

    close = _last(df, "close")
    prev_close = _prev(df, "close")
    open_ = _last(df, "open")

    ma5 = _last(df, "ma5")
    ma20 = _last(df, "ma20")
    ma60 = _last(df, "ma60")
    ma120 = _last(df, "ma120")
    ma240 = _last(df, "ma240")
    prev_ma60 = _prev(df, "ma60")

    k = _last(df, "k"); d = _last(df, "d")
    prev_k = _prev(df, "k"); prev_d = _prev(df, "d")

    dif = _last(df, "dif")
    hist = _last(df, "macd_hist")
    prev_hist = _prev(df, "macd_hist")

    rsi14 = _last(df, "rsi14"); prev_rsi = _prev(df, "rsi14")

    vol_ratio = _last(df, "vol_ratio")

    # A1 多頭排列
    trend_cfg = cfg.get("trend", {})
    if trend_cfg.get("bullish_ma_alignment", {}).get("enabled"):
        hits["bullish_ma_alignment"] = bool(
            pd.notna(ma5) and pd.notna(ma20) and pd.notna(ma60) and pd.notna(ma120)
            and ma5 > ma20 > ma60 > ma120 and close > ma5
        )

    # A2 黃金交叉(5MA 上穿 20MA)
    if trend_cfg.get("golden_cross", {}).get("enabled"):
        prev_ma5 = _prev(df, "ma5"); prev_ma20 = _prev(df, "ma20")
        hits["golden_cross"] = bool(
            pd.notna(prev_ma5) and pd.notna(prev_ma20) and pd.notna(ma5) and pd.notna(ma20)
            and prev_ma5 <= prev_ma20 and ma5 > ma20
        )

    # A3 突破季線
    if trend_cfg.get("break_60ma", {}).get("enabled"):
        hits["break_60ma"] = bool(
            pd.notna(ma60) and pd.notna(prev_ma60) and pd.notna(prev_close)
            and prev_close < prev_ma60 and close > ma60
        )

    # A4 站上年線(連續 5 日)
    above_cfg = trend_cfg.get("above_240ma", {})
    if above_cfg.get("enabled"):
        n = int(above_cfg.get("consecutive_days", 5))
        if "ma240" in df.columns and len(df) >= n:
            tail = df.tail(n)
            hits["above_240ma"] = bool(
                tail["ma240"].notna().all() and (tail["close"] > tail["ma240"]).all()
            )
        else:
            hits["above_240ma"] = False

    # B1 KD 低檔黃金交叉
    mom_cfg = cfg.get("momentum", {})
    kd_cfg = mom_cfg.get("kd_golden_cross_low", {})
    if kd_cfg.get("enabled"):
        thr = kd_cfg.get("low_threshold", 30)
        hits["kd_golden_cross_low"] = bool(
            pd.notna(prev_k) and pd.notna(prev_d) and pd.notna(k) and pd.notna(d)
            and prev_k <= prev_d and k > d and k < thr
        )

    # B2 MACD 翻紅
    if mom_cfg.get("macd_turn_red", {}).get("enabled"):
        hits["macd_turn_red"] = bool(
            pd.notna(prev_hist) and pd.notna(hist) and pd.notna(dif)
            and prev_hist <= 0 and hist > 0 and dif > 0
        )

    # B3 RSI 突破 50
    if mom_cfg.get("rsi_break_50", {}).get("enabled"):
        hits["rsi_break_50"] = bool(
            pd.notna(prev_rsi) and pd.notna(rsi14) and prev_rsi <= 50 and rsi14 > 50
        )

    # C1 量價齊揚
    vp_cfg = cfg.get("volume_price", {})
    vps_cfg = vp_cfg.get("volume_price_surge", {})
    if vps_cfg.get("enabled"):
        vol_mult = vps_cfg.get("volume_multiplier", 1.5)
        pmin = vps_cfg.get("price_change_min", 0.02)
        chg = (close / prev_close - 1) if (pd.notna(close) and pd.notna(prev_close) and prev_close) else np.nan
        hits["volume_price_surge"] = bool(
            pd.notna(chg) and pd.notna(vol_ratio) and pd.notna(open_)
            and close > open_ and vol_ratio >= vol_mult and chg >= pmin
        )

    # C2 N 日新高
    nh_cfg = vp_cfg.get("n_day_high", {})
    if nh_cfg.get("enabled"):
        n = int(nh_cfg.get("period", 60))
        if len(df) >= n and "close" in df.columns:
            window_max = df["close"].tail(n).max()
            hits["n_day_high"] = bool(pd.notna(close) and close >= window_max)
        else:
            hits["n_day_high"] = False

    # D. 籌碼類
    chips_cfg = cfg.get("chips", {})

    d1_cfg = chips_cfg.get("inst_consecutive_buy", {})
    if d1_cfg.get("enabled"):
        n = int(d1_cfg.get("days", 3))
        hits["inst_consecutive_buy"] = _inst_buy_streak_ok(chips_df, n)

    d2_cfg = chips_cfg.get("foreign_holding_increase", {})
    if d2_cfg.get("enabled"):
        period = int(d2_cfg.get("period", 30))
        thr = float(d2_cfg.get("threshold", 0.02)) * 100
        hits["foreign_holding_increase"] = _foreign_holding_up(chips_df, period, thr)

    d3_cfg = chips_cfg.get("short_cover_with_buy", {})
    if d3_cfg.get("enabled"):
        cover_thr = float(d3_cfg.get("cover_threshold", 0.05))
        hits["short_cover_with_buy"] = _short_cover_with_buy(chips_df, cover_thr)

    # E. 基本面快篩
    fund_cfg = cfg.get("fundamental", {})

    e1_cfg = fund_cfg.get("monthly_revenue_growth", {})
    if e1_cfg.get("enabled"):
        m = int(e1_cfg.get("consecutive_months", 3))
        yoy_min = float(e1_cfg.get("latest_yoy_min", 0.10))
        hits["monthly_revenue_growth"] = _monthly_revenue_growth(revenue_df, m, yoy_min)

    e2_cfg = fund_cfg.get("eps_positive_high_yield", {})
    if e2_cfg.get("enabled"):
        q = int(e2_cfg.get("eps_quarters", 4))
        ymin = float(e2_cfg.get("yield_min", 0.04)) * 100
        hits["eps_positive_high_yield"] = _eps_positive_high_yield(eps_df, per_df, q, ymin)

    return hits


def _inst_buy_streak_ok(chips_df, n: int) -> bool:
    if chips_df is None or chips_df.empty or "inst_total" not in chips_df.columns:
        return False
    tail = chips_df["inst_total"].dropna().tail(n)
    if len(tail) < n:
        return False
    return bool((tail > 0).all())


def _foreign_holding_up(chips_df, period: int, threshold_pct_points: float) -> bool:
    if chips_df is None or chips_df.empty or "foreign_holding_pct" not in chips_df.columns:
        return False
    s = chips_df["foreign_holding_pct"].dropna()
    if len(s) < period:
        return False
    delta = float(s.iloc[-1]) - float(s.iloc[-period])
    return delta >= threshold_pct_points


def _short_cover_with_buy(chips_df, cover_threshold: float) -> bool:
    if chips_df is None or chips_df.empty:
        return False
    if "short_balance" not in chips_df.columns or "inst_total" not in chips_df.columns:
        return False
    sb = chips_df["short_balance"].dropna()
    it = chips_df["inst_total"].dropna()
    if len(sb) < 2 or it.empty:
        return False
    last = float(sb.iloc[-1]); prev = float(sb.iloc[-2])
    if prev <= 0:
        return False
    short_change = (last - prev) / prev
    inst_today = float(it.iloc[-1])
    return short_change <= -cover_threshold and inst_today > 0


def _monthly_revenue_growth(rev_df, consecutive: int, latest_yoy_min: float) -> bool:
    if rev_df is None or rev_df.empty or "revenue_yoy" not in rev_df.columns:
        return False
    yoy = rev_df["revenue_yoy"].dropna()
    if len(yoy) < consecutive:
        return False
    tail = yoy.tail(consecutive)
    if not (tail > 0).all():
        return False
    return float(tail.iloc[-1]) >= latest_yoy_min


def _eps_positive_high_yield(eps_df, per_df, quarters: int, yield_min_pct: float) -> bool:
    if eps_df is None or eps_df.empty or "eps" not in eps_df.columns:
        return False
    last_eps = eps_df["eps"].dropna().tail(quarters)
    if len(last_eps) < quarters or not (last_eps > 0).all():
        return False
    if per_df is None or per_df.empty or "yield_pct" not in per_df.columns:
        return False
    y = per_df["yield_pct"].dropna()
    if y.empty:
        return False
    return float(y.iloc[-1]) >= yield_min_pct


def evaluate_combos(hits: dict, combos_cfg: list[dict]) -> list[str]:
    """Return list of combo names whose required strategies all hit."""
    triggered = []
    for c in combos_cfg or []:
        name = c.get("name")
        reqs = c.get("requires", [])
        if reqs and all(hits.get(r, False) for r in reqs):
            triggered.append(name)
    return triggered


def screen_stock(
    df: pd.DataFrame,
    cfg: dict,
    *,
    chips_df: pd.DataFrame | None = None,
    revenue_df: pd.DataFrame | None = None,
    eps_df: pd.DataFrame | None = None,
    per_df: pd.DataFrame | None = None,
) -> dict:
    hits = evaluate_stock(
        df, cfg,
        chips_df=chips_df, revenue_df=revenue_df, eps_df=eps_df, per_df=per_df,
    )
    combos = evaluate_combos(hits, cfg.get("combos", []))
    return {"hits": hits, "combos": combos}


def stock_summary(stock_id: str, name: str, df: pd.DataFrame, screen: dict) -> dict:
    last = df.iloc[-1] if not df.empty else None
    prev = df.iloc[-2] if len(df) >= 2 else None
    close = float(last["close"]) if last is not None and pd.notna(last["close"]) else None
    prev_close = float(prev["close"]) if prev is not None and pd.notna(prev["close"]) else None
    chg_pct = None
    if close is not None and prev_close:
        chg_pct = round((close / prev_close - 1) * 100, 2)
    return {
        "stock_id": stock_id,
        "name": name,
        "close": close,
        "change_pct": chg_pct,
        "hits": [k for k, v in screen["hits"].items() if v],
        "combos": screen["combos"],
    }
