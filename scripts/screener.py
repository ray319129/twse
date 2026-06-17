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
<<<<<<< HEAD
    valuation: dict | None = None,
=======
>>>>>>> 414df8c9b457775ced4be6676c0b06ea699cba4d
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
<<<<<<< HEAD
        # 殖利率優先用 per_df,沒有就退而用全市場估值快照(valuation snapshot)。
        # 以前這裡只吃 per_df,而 main.py 把 per_df 寫死 None,導致本策略永遠 False。
        fallback_yield = None
        if valuation and valuation.get("yield_pct") is not None:
            fallback_yield = float(valuation["yield_pct"])
        hits["eps_positive_high_yield"] = _eps_positive_high_yield(eps_df, per_df, q, ymin, fallback_yield)

    # F. 領先 / 醞釀型(目標:在「發動之前」就抓到,代價是假訊號較多)
    lead_cfg = cfg.get("leading", {})

    sq_cfg = lead_cfg.get("coiling_squeeze", {})
    if sq_cfg.get("enabled"):
        hits["coiling_squeeze"] = _coiling_squeeze(
            df,
            lookback=int(sq_cfg.get("lookback", 120)),
            pct=float(sq_cfg.get("width_pct", 0.15)),
            abs_width_max=float(sq_cfg.get("abs_width_max", 0.06)),
            vol_contract=bool(sq_cfg.get("require_volume_contraction", True)),
            near_ma60_tol=float(sq_cfg.get("near_ma60_tol", 0.10)),
        )

    pb_cfg = lead_cfg.get("pullback_to_support", {})
    if pb_cfg.get("enabled"):
        hits["pullback_to_support"] = _pullback_to_support(
            df,
            near_pct=float(pb_cfg.get("near_pct", 0.03)),
            min_drawdown=float(pb_cfg.get("min_drawdown", 0.05)),
            lookback_high=int(pb_cfg.get("lookback_high", 20)),
            require_turn_up=bool(pb_cfg.get("require_turn_up", True)),
        )

    rs_cfg = lead_cfg.get("relative_strength_leader", {})
    if rs_cfg.get("enabled"):
        hits["relative_strength_leader"] = _relative_strength_leader(
            df,
            rs_min=float(rs_cfg.get("rs_min", 1.0)),
            rising_days=int(rs_cfg.get("rising_days", 10)),
        )

    ca_cfg = lead_cfg.get("chip_accumulation", {})
    if ca_cfg.get("enabled"):
        hits["chip_accumulation"] = _chip_accumulation(
            chips_df, df,
            lookback=int(ca_cfg.get("lookback", 10)),
            min_buy_days=int(ca_cfg.get("min_buy_days", 6)),
            flat_ret20_max=float(ca_cfg.get("flat_ret20_max", 0.08)),
        )
=======
        hits["eps_positive_high_yield"] = _eps_positive_high_yield(eps_df, per_df, q, ymin)
>>>>>>> 414df8c9b457775ced4be6676c0b06ea699cba4d

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


<<<<<<< HEAD
def _eps_positive_high_yield(eps_df, per_df, quarters: int, yield_min_pct: float, fallback_yield: float | None = None) -> bool:
=======
def _eps_positive_high_yield(eps_df, per_df, quarters: int, yield_min_pct: float) -> bool:
>>>>>>> 414df8c9b457775ced4be6676c0b06ea699cba4d
    if eps_df is None or eps_df.empty or "eps" not in eps_df.columns:
        return False
    last_eps = eps_df["eps"].dropna().tail(quarters)
    if len(last_eps) < quarters or not (last_eps > 0).all():
        return False
<<<<<<< HEAD
    y = None
    if per_df is not None and not per_df.empty and "yield_pct" in per_df.columns:
        s = per_df["yield_pct"].dropna()
        if not s.empty:
            y = float(s.iloc[-1])
    if y is None:
        y = fallback_yield
    if y is None:
        return False
    return y >= yield_min_pct


# ---------- 領先 / 醞釀型訊號 ----------

def _coiling_squeeze(df, lookback: int, pct: float, abs_width_max: float, vol_contract: bool, near_ma60_tol: float) -> bool:
    """波動壓縮:盤整收斂、蓄勢待發。兩種觸發任一即可:
      (1) 相對:布林帶寬度落在近 lookback 日最低 pct 分位(由放大轉收斂的當下)
      (2) 絕對:帶寬已經很窄(< abs_width_max,例如 ±4% 內)→ 持續盤的也抓得到
    額外要求量縮(vol_ma5 < vol_ma20)且未跌破季線太多(避免接到下跌中的刀)。"""
    if "bb_width" not in df.columns:
        return False
    bw = df["bb_width"].dropna()
    if len(bw) < min(lookback, 60):
        return False
    window = bw.tail(lookback)
    cur = float(bw.iloc[-1])
    thr = float(window.quantile(pct))
    if cur > thr and cur > abs_width_max:
        return False
    if vol_contract:
        vma5 = _last(df, "vol_ma5"); vma20 = _last(df, "vol_ma20")
        if not (pd.notna(vma5) and pd.notna(vma20) and vma5 < vma20):
            return False
    close = _last(df, "close"); ma60 = _last(df, "ma60")
    if pd.notna(close) and pd.notna(ma60) and ma60 > 0 and close < ma60 * (1 - near_ma60_tol):
        return False
    return True


def _pullback_to_support(df, near_pct: float, min_drawdown: float, lookback_high: int, require_turn_up: bool) -> bool:
    """回測支撐:多頭趨勢仍在(季線上揚且站在季線之上),自近期高點回檔 min_drawdown 以上,
    且收盤回到 20MA/60MA 附近,並開始轉強。買在下一段發動之前,而非追突破。"""
    if len(df) < 80:
        return False
    close = _last(df, "close"); prev_close = _prev(df, "close")
    ma20 = _last(df, "ma20"); ma60 = _last(df, "ma60")
    if not (pd.notna(close) and pd.notna(ma20) and pd.notna(ma60)):
        return False
    # 趨勢仍在:季線較 20 日前上揚。收盤允許「測試」季線(可略低於季線,但不可崩破):
    # 多頭回檔到 20MA 時,因 20MA 與 60MA 通常只差幾 %,收盤常會順勢輕觸 60MA,
    # 故放寬到「不可低於季線 near_pct 以上」,真正跌破(>near_pct)才視為轉弱出局。
    ma60_now = df["ma60"].iloc[-1]
    ma60_past = df["ma60"].iloc[-21] if len(df) >= 21 else np.nan
    if not (pd.notna(ma60_past) and ma60_now > ma60_past):
        return False
    if ma60 > 0 and close < ma60 * (1 - near_pct):
        return False
    # 自近期高點回檔一定幅度
    high_src = df["high"] if "high" in df.columns else df["close"]
    recent_high = high_src.tail(lookback_high).max()
    if not (pd.notna(recent_high) and recent_high > 0):
        return False
    if (recent_high - close) / recent_high < min_drawdown:
        return False
    # 回到支撐附近(20MA 或 60MA,或近 3 日最低點曾觸及 20MA)
    near20 = ma20 > 0 and abs(close - ma20) / ma20 <= near_pct
    near60 = ma60 > 0 and abs(close - ma60) / ma60 <= near_pct
    low_touch = False
    if "low" in df.columns and ma20 > 0:
        low_touch = bool((df["low"].tail(3) <= ma20 * (1 + near_pct)).any())
    if not (near20 or near60 or low_touch):
        return False
    # 開始轉強:今日收紅,或 KD 在交叉向上
    if require_turn_up:
        green = pd.notna(prev_close) and close > prev_close
        k = _last(df, "k"); d = _last(df, "d"); pk = _prev(df, "k"); pdd = _prev(df, "d")
        kd_up = pd.notna(k) and pd.notna(d) and k >= d and (pd.isna(pk) or pd.isna(pdd) or pk <= pdd)
        if not (green or kd_up):
            return False
    return True


def _relative_strength_leader(df, rs_min: float, rising_days: int) -> bool:
    """相對強勢領頭羊:近 60 日報酬贏大盤(rs_ratio >= rs_min),且相對強弱線 rs_line
    仍在走揚(今日 > rising_days 日前)。

    用 rs_line(個股/大盤的比值線)判斷「仍在轉強」,而非 rs_ratio 的動能 ——
    因為穩定領漲的股票,其 60 日報酬比值會自然鈍化,但相對強弱線仍持續創高,
    用 rs_line 才不會把「穩定領頭羊」誤判掉。"""
    if "rs_ratio" not in df.columns or "rs_line" not in df.columns:
        return False
    ratio = df["rs_ratio"].dropna()
    line = df["rs_line"].dropna()
    if len(ratio) < 1 or len(line) < rising_days + 1:
        return False
    if float(ratio.iloc[-1]) < rs_min:
        return False
    return float(line.iloc[-1]) > float(line.iloc[-1 - rising_days])


def _chip_accumulation(chips_df, df, lookback: int, min_buy_days: int, flat_ret20_max: float) -> bool:
    """籌碼吸籌:近 lookback 日法人有 min_buy_days 天以上淨買超、且累計為正,
    但股價近 20 日仍持平(|報酬| <= flat_ret20_max)→ 主力在價格還沒動時默默布局。
    這是最領先的訊號:籌碼通常領先價格。"""
    if chips_df is None or chips_df.empty or "inst_total" not in chips_df.columns:
        return False
    it = chips_df["inst_total"].dropna().tail(lookback)
    if len(it) < lookback:
        return False
    if int((it > 0).sum()) < min_buy_days:
        return False
    if float(it.sum()) <= 0:
        return False
    if df is not None and len(df) >= 21:
        c0 = df["close"].iloc[-1]; c20 = df["close"].iloc[-21]
        if pd.notna(c0) and pd.notna(c20) and c20:
            if abs(float(c0) / float(c20) - 1) > flat_ret20_max:
                return False
    return True
=======
    if per_df is None or per_df.empty or "yield_pct" not in per_df.columns:
        return False
    y = per_df["yield_pct"].dropna()
    if y.empty:
        return False
    return float(y.iloc[-1]) >= yield_min_pct
>>>>>>> 414df8c9b457775ced4be6676c0b06ea699cba4d


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
<<<<<<< HEAD
    valuation: dict | None = None,
=======
>>>>>>> 414df8c9b457775ced4be6676c0b06ea699cba4d
) -> dict:
    hits = evaluate_stock(
        df, cfg,
        chips_df=chips_df, revenue_df=revenue_df, eps_df=eps_df, per_df=per_df,
<<<<<<< HEAD
        valuation=valuation,
=======
>>>>>>> 414df8c9b457775ced4be6676c0b06ea699cba4d
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
