from __future__ import annotations
import argparse
import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import (
    assert_env, load_screeners, load_watchlist,
    SIGNALS_DIR, DATA_DIR, now_tpe,
)
from .fetchers import (
    fetch_stock_info, filter_tradable_stocks, fetch_news,
    fetch_price_history, fetch_chips_history,
    fetch_monthly_revenue, fetch_per_yield, fetch_day_trade_ratio,
    fetch_valuation_snapshot, fetch_valuation_snapshot_tpex, fetch_index_history,
    fetch_restricted_stocks,
)
from .storage import (
    load_prices, upsert_prices, save_prices, prices_scale_shift,
    load_chips, upsert_chips,
    load_revenue, upsert_revenue,
    load_per, upsert_per,
)
from .indicators import compute_all, reference_levels, compute_relative_strength
from .screener import screen_stock, stock_summary
from .scoring import compute_conviction
from .industry import compute_industry_trends
from .market import compute_market_regime, compute_risk_gate
from .track import build_report as build_perf_report, compute_entry_plan, compute_position_size, _style_of
from .fundamentals import update_fundamentals, fundamental_summary, fundamental_score
from .catalyst import classify_catalysts, catalyst_score
from .events import upcoming_events
from .notify import render_email, send_email
from .utils import log

HOT_INDUSTRY_TOP_N = 5

# 排除的非主流板別(短線流動性/制度考量)
EXCLUDE_INDUSTRY_KEYWORDS = ("創新版", "創新板")

STRATEGY_LABEL = {
    "bullish_ma_alignment": "多頭排列",
    "golden_cross": "黃金交叉",
    "break_60ma": "突破季線",
    "above_240ma": "站上年線",
    "kd_golden_cross_low": "KD 低檔黃金交叉",
    "macd_turn_red": "MACD 翻紅",
    "rsi_break_50": "RSI 突破 50",
    "volume_price_surge": "量價齊揚",
    "n_day_high": "N 日新高",
    "inst_consecutive_buy": "法人連買",
    "foreign_holding_increase": "外資加碼",
    "short_cover_with_buy": "融券回補+主力買超",
    "monthly_revenue_growth": "月營收連續成長",
    "eps_positive_high_yield": "EPS+高殖利率",
    "coiling_squeeze": "盤底蓄勢",
    "pullback_to_support": "回測支撐",
    "relative_strength_leader": "相對強勢",
    "chip_accumulation": "籌碼吸籌",
}


def _is_trading_day(today: date) -> bool:
    sample = load_prices("2330")
    if sample.empty:
        return True
    last = sample.index.max().date()
    if last == today:
        return True
    inc = fetch_price_history("2330", "twse", days=2)
    if inc.empty:
        return False
    return inc.index.max().date() == today


def _update_chips(stock_id: str, today: date, history_days: int = 35) -> pd.DataFrame:
    existing = load_chips(stock_id)
    if not existing.empty:
        last = existing.index.max().date()
        if last >= today:
            return existing
        # 重疊回補:三大法人(~16:00)、融資券(~21:00)、外資持股(隔日)出表時間不同,
        # 當天 16:30 跑時 last 那天的 margin/short/holding 還是 NaN。若從 last+1 起抓,那天的缺值
        # 永遠補不回來。改從 last-4 天起重抓,配合 upsert_chips 的 combine_first(新 NaN 不覆蓋舊值)
        # 讓後續 run 能把先前的缺格補上。
        start = last - timedelta(days=4)
    else:
        start = today - timedelta(days=history_days * 2)
    new = fetch_chips_history(stock_id, start, today)
    if new.empty:
        return existing
    return upsert_chips(stock_id, new)


def _update_revenue(stock_id: str) -> pd.DataFrame:
    new = fetch_monthly_revenue(stock_id, months=18)
    if new.empty:
        return load_revenue(stock_id)
    return upsert_revenue(stock_id, new)


def _chip_summary(chips_df: pd.DataFrame | None) -> dict:
    if chips_df is None or chips_df.empty:
        return {}
    out: dict = {}
    if "inst_total" in chips_df.columns:
        inst = chips_df["inst_total"].dropna()
        streak = 0
        for v in reversed(inst.values):
            if v > 0:
                streak += 1
            else:
                break
        # 用最後一個非 NaN 值;籌碼來源外接合併,最後一列的 inst_total 可能是 NaN。
        out["inst_total_today"] = int(inst.iloc[-1]) if not inst.empty else 0
        out["inst_buy_streak"] = streak
    if "foreign_holding_pct" in chips_df.columns:
        s = chips_df["foreign_holding_pct"].dropna()
        if not s.empty:
            out["foreign_holding_pct"] = round(float(s.iloc[-1]), 2)
            # 用「日期差 30 天」而非「位置差 30 格」:外資持股序列常有缺洞,用 iloc[-30] 會讓
            # 實際窗口飄移到 40~60 個日曆日,30 日變化失真。改取最近一筆日期 <= (最新-30天) 的值當基準。
            target = s.index[-1] - pd.Timedelta(days=30)
            base = s.loc[:target]
            if not base.empty:
                out["foreign_holding_change_30d"] = round(float(s.iloc[-1]) - float(base.iloc[-1]), 2)
    if "short_balance" in chips_df.columns:
        sb = chips_df["short_balance"].dropna()
        if len(sb) >= 2:
            prev = float(sb.iloc[-2])
            if prev > 0:
                out["short_change_pct"] = round((float(sb.iloc[-1]) - prev) / prev * 100, 2)
    return out


def _fund_summary(revenue_df, eps_df, per_df) -> dict:
    out: dict = {}
    if revenue_df is not None and not revenue_df.empty:
        last_ym = revenue_df.index[-1]
        last_rev = revenue_df.iloc[-1]
        out["revenue_latest_ym"] = str(last_ym)
        if pd.notna(last_rev.get("revenue_yoy")):
            out["revenue_yoy"] = round(float(last_rev["revenue_yoy"]) * 100, 2)
        yoy = revenue_df["revenue_yoy"].dropna()
        if len(yoy) >= 3:
            tail = yoy.tail(3)
            out["revenue_consecutive_growth_months"] = int((tail > 0).sum())
    if eps_df is not None and not eps_df.empty:
        last_eps = eps_df["eps"].dropna()
        if not last_eps.empty:
            out["eps_latest"] = round(float(last_eps.iloc[-1]), 2)
            out["eps_quarters_loaded"] = int(len(last_eps))
    if per_df is not None and not per_df.empty:
        for col in ("pe", "yield_pct", "pb"):
            if col in per_df.columns:
                s = per_df[col].dropna()
                if not s.empty:
                    out[col] = round(float(s.iloc[-1]), 2)
    return out


def _enrich_pick(pick: dict, today: date, index_close, *, fundamentals: bool,
                 news: bool = False, screen_cfg: dict | None = None,
                 plan_cfg: tuple | None = None) -> dict:
    """為入榜股票補:技術座標(免費)+ FinMind 籌碼/財報。原地更新並回傳同一 dict。
    plan_cfg = (exit_cfg, max_chase) 時,額外算「明日進場計畫」(參考價/進場上限/停損/TP1/R)。"""
    sid = pick["stock_id"]; sname = pick.get("name", "")
    df = load_prices(sid)
    if not df.empty:
        # 對齊基準日:正常跑時 today=當天、本機資料本就 <= today(無作用);
        # 歷史測試時把價格截到指定日,座標/決策卡才反映那天的收盤。
        df = df[df.index.date <= today]

    # ---- 先抓籌碼/財報,再跑 screen_stock ----
    # screen_stock 的 D 籌碼類(法人連買/外資加碼/融券回補)、E 基本面類(月營收/EPS+殖利率)
    # 與所有 combos 都吃這些資料;若在 screen_stock 之後才抓,這幾類策略與全部 combos 永遠 False
    # (Bug 1:11 天 355 檔 picks 命中 0 次)。故務必在此先備妥。
    chips_df = _update_chips(sid, today)
    if chips_df is not None and not chips_df.empty:
        cs = _chip_summary(chips_df)
        if cs:
            pick["chips"] = cs

    revenue_df = None
    eps_df = None
    if fundamentals:
        revenue_df = _update_revenue(sid)
        fin, bal, cf = update_fundamentals(sid)              # FinMind 季財報/資產負債/現金流(快取新鮮就不重抓)
        summ = fundamental_summary(fin, bal, cf, revenue_df)
        if summ:
            pick["fundamentals"] = summ
        # EPS 直接取自季財報 fin(已含 eps 欄,來源同 fetch_eps_quarterly 的 TaiwanStockFinancialStatements),
        # 供 E2「EPS 正+高殖利率」判定,不必另打一支 FinMind API。
        if fin is not None and not fin.empty and "eps" in fin.columns:
            eps_series = fin["eps"].dropna()
            if not eps_series.empty:
                eps_df = eps_series.to_frame("eps")
        # 當沖比(3.4-2):只在 enrich 階段(核心候選/自選)抓,控 FinMind 額度;抓不到回 None → 不扣分。
        dt_cfg = ((screen_cfg or {}).get("scoring", {}) or {}).get("day_trade_penalty", {}) or {}
        if dt_cfg.get("enabled", False):
            dtr = fetch_day_trade_ratio(sid)
            if dtr is not None:
                pick["day_trade_ratio"] = round(float(dtr), 3)
                if dtr > float(dt_cfg.get("threshold", 0.40)):
                    pick["day_trade_warn"] = True     # 供 email/網頁標記「當沖比偏高」

    if not df.empty and len(df) >= 60:
        df_ind = compute_all(df)
        if index_close is not None:
            df_ind = compute_relative_strength(df_ind, index_close, n=60)
        pick["levels"] = reference_levels(df_ind)
        # 近 40 個交易日 OHLC → 網頁畫迷你日K棒圖(零額外 API,純既有 parquet 價格)。
        ohlc_tail = df.tail(40)[["open", "high", "low", "close"]]
        pick["ohlc"] = [[round(float(o), 2), round(float(h), 2), round(float(l), 2), round(float(c), 2)]
                        for o, h, l, c in ohlc_tail.itertuples(index=False)]
        if screen_cfg is not None:
            scr = screen_stock(df_ind, screen_cfg,
                               chips_df=chips_df, revenue_df=revenue_df, eps_df=eps_df,
                               valuation=pick.get("valuation"))
            pick["hits"] = [h for h, v in scr["hits"].items() if v]
            pick["combos"] = scr["combos"]
        if plan_cfg is not None and pick.get("close"):
            exit_cfg, max_chase = plan_cfg
            pick["plan"] = compute_entry_plan(df_ind, len(df_ind) - 1, float(pick["close"]),
                                              _style_of(pick), exit_cfg, max_chase)
            account_cfg = (screen_cfg or {}).get("account", {})
            position = compute_position_size(pick["plan"], account_cfg)
            if position:
                pick["position"] = position
    if news:
        cat_cfg = ((screen_cfg or {}).get("scoring", {}) or {}).get("catalyst_bonus", {}) or {}
        pick["news"] = fetch_news(sid, sname, limit=int(cat_cfg.get("max_news", 25)))
        if cat_cfg.get("enabled"):
            res = classify_catalysts(sid, sname, pick["news"], cat_cfg)   # 無 key/錯誤 → None,優雅降級
            if res is not None:
                pick["catalysts"] = res.get("catalysts", [])
                pick["catalyst_summary"] = res.get("summary", "")
                pick["risk_flags"] = res.get("risk_flags", [])
                pick["target_prices"] = res.get("target_prices", [])
    return pick


def _clean_for_json(d: dict) -> dict:
    """移除不可序列化 / 過大的鍵。"""
    return {k: v for k, v in d.items() if k != "df_ind"}


def _json_safe(o):
    """遞迴把 NaN / Inf 轉成 None。Python 的 json 預設會把 float('nan') 寫成裸字 NaN,
    那不是合法 JSON,瀏覽器 fetch().json() 會整包失敗。寫檔前一律先過這層。"""
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, np.floating):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    return o


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _chip_signal(chips: dict | None, cfg: dict) -> float | None:
    """把 enrich 抓到的籌碼摘要轉成 0~1 訊號(供 stage-2 重排)。
    無籌碼資料回 None(視為中性、不加也不扣分)。"""
    if not chips:
        return None
    parts: list[float] = []
    streak = chips.get("inst_buy_streak")
    if streak is not None:
        full = float(cfg.get("streak_full", 5)) or 1.0
        parts.append(_clip01(streak / full))                 # 法人連買天數
    fchg = chips.get("foreign_holding_change_30d")
    if fchg is not None:
        full = float(cfg.get("foreign_full", 2.0)) or 1.0
        parts.append(_clip01(fchg / full))                   # 30日外資持股變化(降→0)
    inst_today = chips.get("inst_total_today")
    if inst_today is not None:
        parts.append(1.0 if inst_today > 0 else 0.0)         # 今日法人淨買
    schg = chips.get("short_change_pct")
    if schg is not None:
        thr = float(cfg.get("short_cover_thr", 0.05)) * 100
        parts.append(1.0 if schg <= -thr else 0.0)           # 融券回補
    if not parts:
        return None
    return sum(parts) / len(parts)


def _rank_core(candidates: list[dict], scoring_cfg: dict, core_count: int,
               industry_rank: dict[str, int] | None = None) -> list[dict]:
    """stage-2 重排:對已 enrich 的核心候選算 籌碼 + 基本面 + 新聞催化劑 + 產業相對強度 四種加成,
    全部併入 rank_score 後重排取前 core_count。原 score(信心分)語意不變;
    各加成可由 config 個別開關,無資料 → 該項 bonus 0(中性不扣分)。"""
    chip_cfg = scoring_cfg.get("chip_bonus", {}) or {}
    fund_cfg = scoring_cfg.get("fundamental_bonus", {}) or {}
    cat_cfg = scoring_cfg.get("catalyst_bonus", {}) or {}
    ind_cfg = scoring_cfg.get("industry_bonus", {}) or {}
    combo_cfg = scoring_cfg.get("combo_bonus", {}) or {}
    dt_cfg = scoring_cfg.get("day_trade_penalty", {}) or {}
    chip_w = float(chip_cfg.get("weight", 10))
    fund_w = float(fund_cfg.get("weight", 5))
    cat_w = float(cat_cfg.get("weight", 8))
    ind_w = float(ind_cfg.get("weight", 4))
    ind_top_n = int(ind_cfg.get("top_n", 5))
    combo_w = float(combo_cfg.get("weight", 6))
    combo_per = float(combo_cfg.get("per_combo", 3))
    for s in candidates:
        bonus = 0.0
        if chip_cfg.get("enabled", False):
            sig = _chip_signal(s.get("chips"), chip_cfg)
            s["chip_signal"] = round(sig, 2) if sig is not None else None
            s["chip_bonus"] = round(chip_w * (sig or 0.0), 1)
            bonus += s["chip_bonus"]
        if fund_cfg.get("enabled", False):
            fs = fundamental_score(s.get("fundamentals") or {}, fund_cfg)
            s["fund_signal"] = round(fs, 2) if fs is not None else None
            s["fund_bonus"] = round(fund_w * (fs or 0.0), 1)
            bonus += s["fund_bonus"]
        if cat_cfg.get("enabled", False):
            cs = catalyst_score(s.get("catalysts"), cat_cfg)
            s["catalyst_signal"] = round(cs, 2) if cs is not None else None
            s["catalyst_bonus"] = round(cat_w * (cs or 0.0), 1)
            bonus += s["catalyst_bonus"]
        if ind_cfg.get("enabled", False) and industry_rank is not None:
            rk = industry_rank.get(s.get("industry", ""))
            ind_sig = max(0.0, (ind_top_n - rk) / ind_top_n) if (rk is not None and rk < ind_top_n) else 0.0
            s["industry_signal"] = round(ind_sig, 2) if rk is not None else None
            s["industry_bonus"] = round(ind_w * ind_sig, 1)
            bonus += s["industry_bonus"]
        if combo_cfg.get("enabled", False):
            # combo 共振加分:命中的多訊號交集組合越多,越有把握(每個 combo 加 per_combo,上限 weight)。
            # combos 在 _enrich_pick 由 screen_stock 算出(Bug 1 修好後才真的會有值)。
            n_combo = len(s.get("combos") or [])
            s["combo_bonus"] = round(min(n_combo * combo_per, combo_w), 1) if n_combo else 0.0
            bonus += s["combo_bonus"]
        if dt_cfg.get("enabled", False):
            # 當沖比過高扣分(3.4-2):當沖比 > thr → 隔日沖對手盤多、隔天賣壓重,線性扣到 penalty。
            dtr = s.get("day_trade_ratio")
            if dtr is not None:
                thr = float(dt_cfg.get("threshold", 0.40))
                pen_max = float(dt_cfg.get("penalty", 8))
                over = max(0.0, (float(dtr) - thr) / max(1e-6, 1 - thr))
                s["day_trade_penalty"] = round(-pen_max * min(1.0, over), 1)
                bonus += s["day_trade_penalty"]
        s["rank_score"] = round(float(s["score"]) + bonus, 1)
    return sorted(candidates, key=lambda x: -x["rank_score"])[:core_count]


def daily_run(test_mode: bool = False, as_of: "date | None" = None) -> None:
    assert_env()
    cfg = load_screeners()
    watchlist = load_watchlist()
    today = as_of or now_tpe().date()
    historical = as_of is not None
    if historical:
        # 手動指定日期 = 歷史測試:以本機快取價格截到當天為基準,跳過交易日檢查、
        # 不抓增量資料(結果可重現),寄信前綴 [測試]。完全不影響每日自動跑當天的行為。
        test_mode = True
        log.info(f"歷史測試模式:以 {today} 為基準,使用本機快取價格(截到當天),不抓增量。")

    if not test_mode and not _is_trading_day(today):
        log.info(
            f"{today} 沒有當日資料(可能是台股假日,或台北盤前/盤中觸發);跳過寄信。"
            f" 加 --test 旗標可強制寄信。"
        )
        return

    rank_cfg = cfg.get("ranking", {})
    core_count = int(rank_cfg.get("core_count", 10))
    watch_count = int(rank_cfg.get("watch_count", 20))
    min_score = float(rank_cfg.get("min_score", 45))
    enrich_top_n = int(rank_cfg.get("enrich_top_n", 30))
    # 信心分設定:scoring 區塊(權重/門檻)+ 沿用 ranking 的流動性門檻
    score_cfg = dict(cfg.get("scoring", {}) or {})
    score_cfg["min_dollar_volume"] = float(rank_cfg.get("min_dollar_volume", 30_000_000))
    min_hist_new = int(score_cfg.get("min_history_new", 60))   # 新股軌道:第一遍評分的最低 K 棒數(< min_history 者標 new_stock)

    log.info("Loading stock universe...")
    info = fetch_stock_info()
    universe = filter_tradable_stocks(info)
    log.info(f"Universe: {len(universe)} tradable stocks")

    # 排除受限股(全額交割/處置分盤/管理/停止買賣)—— 這些採分盤撮合、流動性瞬間歸零,隔日沖大忌。
    # 由 config global.exclude_full_cash 開關(預設 true);歷史測試不打網路故略過。任一來源失敗會回空集合 → 不過濾。
    global_cfg = cfg.get("global", {}) or {}
    if not historical and global_cfg.get("exclude_full_cash", True):
        restricted = fetch_restricted_stocks(today)
        if restricted:
            before = len(universe)
            universe = universe[~universe["stock_id"].isin(restricted)].reset_index(drop=True)
            log.info(f"排除受限股後:{len(universe)} 檔(移除 {before - len(universe)})")

    valuation_snapshot = fetch_valuation_snapshot(today)
    n_twse = len(valuation_snapshot)
    valuation_snapshot_tpex = fetch_valuation_snapshot_tpex()
    if valuation_snapshot_tpex:
        valuation_snapshot = {**valuation_snapshot_tpex, **valuation_snapshot}  # 股號不重複,TWSE/TPEx 互補(上櫃股不再恆缺估值)
        log.info(f"Valuation snapshot merged: TWSE {n_twse} + TPEx {len(valuation_snapshot_tpex)}")

    # 大盤指數(相對強度用),整個 run 只抓一次
    index_df = fetch_index_history(days=400)
    index_close = index_df["close"] if not index_df.empty else None
    if historical and index_close is not None:
        index_close = index_close[index_close.index.date <= today]
    index_below_ma20 = False
    if index_close is not None and len(index_close) >= 20:
        idx_ma20 = index_close.rolling(20).mean()
        index_below_ma20 = bool(index_close.iloc[-1] < idx_ma20.iloc[-1])
    log.info(f"Index: {0 if index_close is None else len(index_close)} bars, 大盤站上月線={not index_below_ma20}")

    market_map = dict(zip(universe["stock_id"], universe.get("type", pd.Series(["twse"] * len(universe)))))
    name_map = dict(zip(universe["stock_id"], universe["stock_name"]))
    industry_map = dict(zip(universe["stock_id"], universe.get("industry_category", pd.Series([""] * len(universe)))))

    scored: list[dict] = []          # 全市場評分(只用免費資料)
    no_data: list[str] = []
    industry_rows: list[dict] = []

    # ---------- 第一遍:全市場用免費資料評分 ----------
    for sid, sname in name_map.items():
        industry = industry_map.get(sid, "") or ""
        if any(kw in industry for kw in EXCLUDE_INDUSTRY_KEYWORDS):
            continue

        existing = load_prices(sid)
        market = market_map.get(sid, "twse")
        if existing.empty:
            if historical:
                # 歷史測試不抓網路;沒有本機快取就略過這檔
                continue
            new_df = fetch_price_history(sid, market, days=400)
            if new_df.empty:
                no_data.append(sid)
                continue
            df = upsert_prices(sid, new_df)
        else:
            last_date = existing.index.max().date()
            if not historical and (today - last_date).days >= 1:
                inc = fetch_price_history(sid, market, days=10)
                if not inc.empty and prices_scale_shift(existing, inc):
                    # 減資/分割:yfinance 回溯調整了整條序列,快取(舊尺度)與增量(新尺度)不能 concat,
                    # 否則均線/動能全毀且不自癒。整段重抓 400 天覆蓋(除權息旺季必備防線)。
                    full = fetch_price_history(sid, market, days=400)
                    if not full.empty:
                        save_prices(sid, full)
                        df = full
                        log.warning(f"{sid} 價格尺度偏移(疑減資/分割),已整段重抓覆蓋快取")
                    else:
                        # 重抓失敗:保留舊尺度快取(頂多少幾天),別把新尺度增量合併進去(會毀指標)
                        df = existing
                        log.warning(f"{sid} 疑價格尺度偏移但重抓失敗,暫留舊快取、不合併增量")
                else:
                    df = upsert_prices(sid, inc) if not inc.empty else existing
            else:
                df = existing

        if historical:
            df = df[df.index.date <= today]

        # 新股獨立軌道:不再一律要求 120 根 K 棒;新股(>= min_history_new)也評分,只是長均線/相對強度較弱。
        if len(df) < min_hist_new:
            continue

        df_ind = compute_all(df)
        if index_close is not None:
            df_ind = compute_relative_strength(df_ind, index_close, n=60)

        last = df_ind.iloc[-1]
        close_v = last.get("close"); ma5_v = last.get("ma5")   # close_v = 還原價(與均線同尺度,供 above_ma/bullish 判斷)
        ma20_v = last.get("ma20"); ma60_v = last.get("ma60"); ma120_v = last.get("ma120")
        # 顯示 / 漲跌停家數(regime)用「原始成交價」raw,不用還原價 —— 使用者看到的收盤/漲跌幅要跟券商一致。
        close_disp = last.get("close_raw", close_v)
        prev_disp = df_ind["close_raw"].iloc[-2] if ("close_raw" in df_ind.columns and len(df_ind) >= 2) else None
        chg = None
        if pd.notna(close_disp) and prev_disp is not None and pd.notna(prev_disp) and prev_disp:
            chg = round((float(close_disp) / float(prev_disp) - 1) * 100, 2)
        ret20 = 0.0
        if len(df_ind) >= 21:
            c0, c20 = df_ind["close"].iloc[-1], df_ind["close"].iloc[-21]
            if pd.notna(c0) and pd.notna(c20) and c20:
                ret20 = float(c0 / c20 - 1)
        bullish = bool(
            pd.notna(ma5_v) and pd.notna(ma20_v) and pd.notna(ma60_v) and pd.notna(ma120_v)
            and ma5_v > ma20_v > ma60_v > ma120_v and pd.notna(close_v) and close_v > ma5_v
        )
        industry_rows.append({
            "stock_id": sid, "industry": industry,
            "change_pct": chg or 0.0,
            "above_ma20": bool(pd.notna(close_v) and pd.notna(ma20_v) and close_v > ma20_v),
            "above_ma60": bool(pd.notna(close_v) and pd.notna(ma60_v) and close_v > ma60_v),
            "bullish": bullish, "ret20": ret20, "combo_hit": False,
        })

        conv = compute_conviction(df_ind, valuation_snapshot.get(sid, {}), cfg=score_cfg)
        if conv:
            conv.update({
                "stock_id": sid, "name": sname, "industry": industry,
                "close": float(close_disp) if pd.notna(close_disp) else None,   # 顯示用原始成交價
                "change_pct": chg,
                "valuation": valuation_snapshot.get(sid, {}),
            })
            scored.append(conv)

    # ---------- 大盤閘門(regime,3.3):依 index + 市場廣度 + 漲跌停家數動態調 core_count / min_score ----------
    market_cfg = cfg.get("market", {}) or {}
    lu_thr = float(market_cfg.get("limit_up_pct", 0.095)) * 100     # change_pct 為百分比,門檻換算成 %
    breadth = {
        "n": len(industry_rows),
        "above_ma20": sum(1 for r in industry_rows if r.get("above_ma20")),
        "adv": sum(1 for r in industry_rows if (r.get("change_pct") or 0) > 0),
        "dec": sum(1 for r in industry_rows if (r.get("change_pct") or 0) < 0),
        "limit_up": sum(1 for r in industry_rows if (r.get("change_pct") or 0) >= lu_thr),
        "limit_down": sum(1 for r in industry_rows if (r.get("change_pct") or 0) <= -lu_thr),
    }
    regime = compute_market_regime(index_close, breadth, market_cfg)
    prefer_pb = False
    if regime:
        if regime.get("core_count") is not None:
            core_count = regime["core_count"]
        if regime.get("min_score") is not None:
            min_score = regime["min_score"]
        prefer_pb = bool(regime.get("prefer_pullback"))
        log.info(f"大盤閘門:{regime['level_label']}(votes={regime['votes']})"
                 f" → core_count={core_count}, min_score={min_score}, 弱盤偏好回測={prefer_pb}")

    # ---------- 風險狀態提示(2026-07-18):指數長均線 + 期貨法人未平倉。只提示、不過濾 ----------
    # 整合回測顯示同一批選股在「指數>60MA 且 法人偏多」的日子平均淨 +0.58%/筆,不分日子則 −0.40%。
    # 刻意不做硬過濾(見 compute_risk_gate 註解)。抓不到期貨資料 → 只用指數均線,不影響主流程。
    try:
        from .fetchers import fetch_futures_inst_net_oi
        fut_oi = fetch_futures_inst_net_oi(days=120)
        risk_gate = compute_risk_gate(index_close, fut_oi)
        if risk_gate and regime is not None:
            regime["risk_gate"] = risk_gate
            log.info(f"風險狀態:{risk_gate['label']}({risk_gate['state']}) "
                     f"指數{'>' if risk_gate['trend_ok'] else '<'}{risk_gate['ma_long']}MA, "
                     f"法人未平倉偏{'多' if risk_gate.get('fut_ok') else '空' if risk_gate.get('fut_ok') is False else '?'}")
    except Exception as e:
        log.warning(f"風險狀態提示計算失敗(略過,不影響選股):{e}")

    # ---------- 排序 → 候選池 →(stage-2 重排:籌碼+基本面+催化劑)→ 核心 / 觀察 ----------
    scoring_cfg = (cfg.get("scoring", {}) or {})
    chip_cfg = scoring_cfg.get("chip_bonus", {}) or {}
    chip_on = bool(chip_cfg.get("enabled", False))
    cand_n = int(chip_cfg.get("candidate_count", core_count)) if chip_on else core_count

    # 弱盤(prefer_pullback)時,把「純追突破」(breakout 但非 pullback_turn)的排序分數扣一截,
    # 讓觸發偏好『回測轉強』而非追突破(顯示的 score 不變,只影響誰進候選/核心)。
    bo_pen = float(market_cfg.get("breakout_penalty_weak", 8.0))
    def _trig_key(s):
        base = float(s["score"])
        if prefer_pb and s.get("breakout") and not s.get("pullback_turn"):
            base -= bo_pen
        return -base
    trigger_sorted = sorted(
        [s for s in scored if s["trigger"] and s["score"] >= min_score],
        key=_trig_key,
    )
    # 候選池:基礎信心分最高的觸發股(略多於 core_count,讓 stage-2 加成能改變誰進核心);受 enrich_top_n 上限保護 API
    core_candidates = trigger_sorted[:max(cand_n, core_count)][:enrich_top_n]
    # 新股獨立軌道:確保候選池含最多 new_max 檔「觸發的新股」(否則低分新股會被老股擠出候選池、根本沒機會被 enrich)
    new_cfg = scoring_cfg.get("new_stock", {}) or {}
    new_max = int(new_cfg.get("max_core", 2)) if new_cfg.get("enabled", False) else 0
    if new_max:
        have_ids = {s["stock_id"] for s in core_candidates}
        new_trig = [s for s in trigger_sorted if s.get("new_stock") and s["stock_id"] not in have_ids]
        core_candidates = core_candidates + new_trig[:new_max]

    # 產業排行(用全市場第一遍資料就能算,不需等 enrich)→ 同時供 stage-2 產業加成 + 熱門產業🔥標記
    industry_trends = compute_industry_trends(industry_rows)
    industry_rank = {t["industry"]: i for i, t in enumerate(industry_trends)}  # 0 = 最強產業

    # ---------- 第二遍:對核心候選 + 自選池補抓 FinMind 籌碼/財報 + 新聞催化劑(控制 API 額度) ----------
    # 觀察層只用評分階段已有的欄位,不補抓 → 把稀缺的 FinMind/LLM 額度留給真正要進場的核心候選。
    plan_cfg = (cfg.get("exit", {}), float(cfg.get("entry", {}).get("max_chase", 0.03)))
    for s in core_candidates:
        _enrich_pick(s, today, index_close, fundamentals=True, news=True, screen_cfg=cfg, plan_cfg=plan_cfg)

    # stage-2 加成(籌碼/基本面/催化劑/產業相對強度)後重排取核心;無資料 → 該項 bonus 0(中性不扣分);信心分 score 不變
    core = _rank_core(core_candidates, scoring_cfg, core_count, industry_rank)
    # 新股獨立軌道:保留最多 new_max 個核心名額給新股(_rank_core 已對所有候選寫好 rank_score)。
    # 新股同樣須觸發+達門檻;若核心裡新股不足,用最佳新股替換核心中 rank_score 最低的老股(維持 core_count 不變)。
    if new_max and core:
        n_new = sum(1 for s in core if s.get("new_stock"))
        if n_new < new_max:
            in_core = {id(s) for s in core}
            pool = sorted([s for s in core_candidates if s.get("new_stock") and id(s) not in in_core],
                          key=lambda x: -x.get("rank_score", x["score"]))
            need = min(new_max - n_new, len(pool))
            if need > 0:
                non_new = sorted([s for s in core if not s.get("new_stock")],
                                 key=lambda x: -x.get("rank_score", x["score"]))
                drop = {id(s) for s in non_new[-need:]}          # 移除末段 rank_score 最低的老股
                core = [s for s in core if id(s) not in drop] + pool[:need]
                core.sort(key=lambda x: -x.get("rank_score", x["score"]))
    core_ids = {s["stock_id"] for s in core}
    watch = sorted(
        [s for s in scored if s["brewing"] and not s["trigger"]
         and s["score"] >= min_score and s["stock_id"] not in core_ids],
        key=lambda x: -x["score"],
    )[:watch_count]

    hot_industries = [t["industry"] for t in industry_trends[:HOT_INDUSTRY_TOP_N]]
    hot_set = set(hot_industries)
    for s in core + watch:
        s["hot_industry"] = s.get("industry", "") in hot_set

    watchlist_results: list[dict] = []
    for sid, note in watchlist.items():
        sname = name_map.get(sid, sid)
        base = next((s for s in scored if s["stock_id"] == sid), None)
        pick = dict(base) if base else {
            "stock_id": sid, "name": sname,
            "close": None, "change_pct": None,
            "valuation": valuation_snapshot.get(sid, {}),
        }
        pick["note"] = note
        pick["hot_industry"] = pick.get("industry", "") in hot_set
        _enrich_pick(pick, today, index_close, fundamentals=True, news=True, screen_cfg=cfg)
        watchlist_results.append(pick)

    log.info(
        f"Scored: {len(scored)} | 核心 {len(core)} / 觀察 {len(watch)} / 自選 {len(watchlist_results)} "
        f"| 大盤站上月線={not index_below_ma20}"
    )

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_DIR / f"{today.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump(_json_safe({
            "date": today.isoformat(),
            "core": [_clean_for_json(s) for s in core],
            "watch": [_clean_for_json(s) for s in watch],
            "watchlist": [_clean_for_json(s) for s in watchlist_results],
            "industry_trends": industry_trends,
            "scored_count": len(scored),
            "no_data_count": len(no_data),
        }), f, ensure_ascii=False, indent=2, default=str)

    # 歷史追蹤與績效:回看過去所有核心選股的後續走勢(讀剛寫入的 + 歷史 signals)
    try:
        performance = build_perf_report(as_of=today, exit_cfg=cfg.get("exit", {}), entry_cfg=cfg.get("entry", {}),
                                         cost_cfg=cfg.get("cost", {}))
    except Exception as e:
        log.warning(f"performance report failed: {e}")
        performance = {"overall": {}, "by_horizon": {}, "exit_sim": {}, "ledger": [],
                       "ledger_total": 0, "ledger_cap": 50, "horizons": [], "total_tracked": 0}
    with open(DATA_DIR / "performance.json", "w", encoding="utf-8") as f:
        json.dump(_json_safe(performance), f, ensure_ascii=False, indent=2, default=str)

    # 網頁資料包(docs/data.json):網頁讀這一包就能呈現與 email 相同內容並互動分類
    docs_dir = DATA_DIR.parent / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    core_clean = [_clean_for_json(s) for s in core]
    watch_clean = [_clean_for_json(s) for s in watch]
    watchlist_clean = [_clean_for_json(s) for s in watchlist_results]
    # 事件行事曆:選股當晚預告未來幾天的宏觀/重大事件(留倉風險提示),確定性、不爬網。
    # 市場級公開資訊(非個人資料),寫進 data.json 供網頁「我的持倉」撞事件提醒 client-side join。
    events_cfg = cfg.get("events", {}) or {}
    events = (upcoming_events(today, events_cfg.get("horizon_days", 7))
              if events_cfg.get("enabled", True) else None)
    with open(docs_dir / "data.json", "w", encoding="utf-8") as f:
        json.dump(_json_safe({
            "date": today.isoformat(),
            "generated_at": now_tpe().strftime("%Y-%m-%d %H:%M"),
            "index_below_ma20": index_below_ma20,
            "market_regime": regime,
            "scored_count": len(scored),
            "core": core_clean,
            "watch": watch_clean,
            "watchlist": watchlist_clean,
            "performance": performance,
            "events": events,
            "label": STRATEGY_LABEL,
        }), f, ensure_ascii=False, indent=2, default=str)

    # 每日選股快照(供網頁「歷史日期切換」)+ 日期索引
    hist_dir = docs_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    with open(hist_dir / f"{today.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump(_json_safe({
            "date": today.isoformat(),
            "index_below_ma20": index_below_ma20,
            "market_regime": regime,
            "scored_count": len(scored),
            "core": core_clean, "watch": watch_clean, "watchlist": watchlist_clean,
            "events": events,
            "label": STRATEGY_LABEL,
        }), f, ensure_ascii=False, indent=2, default=str)
    dates = sorted(p.stem for p in hist_dir.glob("*.json"))
    with open(docs_dir / "dates.json", "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False)

    # 族群熱力圖資料包(docs/heatmap.json):前端 ECharts Treemap 讀這一包
    heatmap_stocks = [
        {
            "id": s["stock_id"],
            "n":  s.get("name", ""),
            "ind": s.get("industry", ""),
            "chg": s.get("change_pct"),
            "r20": s.get("ret20_pct"),
            "sc":  round(float(s.get("score") or 0), 1),
            "vol": max(float(s.get("dollar_vol_m") or 1), 1),
        }
        for s in scored
    ]
    with open(docs_dir / "heatmap.json", "w", encoding="utf-8") as f:
        json.dump({"date": today.isoformat(), "stocks": heatmap_stocks}, f, ensure_ascii=False)

    # 個股健檢(Stock Health)改為純即時查詢(api/health.py),不在批次流程內跑。

    # events 已於 data.json 寫入前算好(見上),email ctx 沿用同一份。
    ctx = {
        "date_str": today.strftime("%Y-%m-%d (%a)"),
        "core": core,
        "watch": watch,
        "watchlist": watchlist_results,
        "performance": performance,
        "core_count": len(core),
        "watch_count": len(watch),
        "scored_count": len(scored),
        "industry_trends": industry_trends[:10],
        "hot_industries": hot_industries,
        "index_below_ma20": index_below_ma20,
        "market_regime": regime,
        "no_data_count": len(no_data),
        "label": STRATEGY_LABEL,
        "events": events,
        "test_mode": test_mode,
    }

    html = render_email("daily_email.html", ctx)
    subject_prefix = "[測試] " if test_mode else ""
    subject = (
        f"{subject_prefix}[台股短線] {today.strftime('%Y/%m/%d')} "
        f"核心 {len(core)} / 觀察 {len(watch)} / 自選 {len(watchlist_results)} 檔"
    )

    send_email(subject, html)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="Bypass trading-day check; subject prefixed [測試]")
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="歷史測試:以指定日期為基準,用本機快取價格截到當天(不抓網路增量、"
                        "跳過交易日檢查、寄[測試]信)。不影響每日自動跑當天的行為。")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    as_of = date.fromisoformat(args.date) if args.date else None
    daily_run(test_mode=args.test, as_of=as_of)
