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
    fetch_monthly_revenue, fetch_eps_quarterly, fetch_per_yield,
    fetch_valuation_snapshot, fetch_valuation_snapshot_tpex, fetch_index_history,
)
from .storage import (
    load_prices, upsert_prices,
    load_chips, upsert_chips,
    load_revenue, upsert_revenue,
    load_eps, upsert_eps,
    load_per, upsert_per,
)
from .indicators import compute_all, reference_levels, compute_relative_strength
from .screener import screen_stock, stock_summary
from .scoring import compute_conviction
from .industry import compute_industry_trends
from .track import build_report as build_perf_report, compute_entry_plan, compute_position_size, _style_of
from .fundamentals import update_fundamentals, fundamental_summary, fundamental_score
from .catalyst import classify_catalysts, catalyst_score
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
        start = last + timedelta(days=1)
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


def _update_eps(stock_id: str) -> pd.DataFrame:
    new = fetch_eps_quarterly(stock_id, quarters=8)
    if new.empty:
        return load_eps(stock_id)
    return upsert_eps(stock_id, new)


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
            if len(s) >= 30:
                out["foreign_holding_change_30d"] = round(float(s.iloc[-1] - s.iloc[-30]), 2)
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
            scr = screen_stock(df_ind, screen_cfg, valuation=pick.get("valuation"))
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
    chips_df = _update_chips(sid, today)
    if chips_df is not None and not chips_df.empty:
        cs = _chip_summary(chips_df)
        if cs:
            pick["chips"] = cs
    if fundamentals:
        revenue_df = _update_revenue(sid)
        fin, bal, cf = update_fundamentals(sid)              # FinMind 季財報/資產負債/現金流(快取新鮮就不重抓)
        summ = fundamental_summary(fin, bal, cf, revenue_df)
        if summ:
            pick["fundamentals"] = summ
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
    chip_w = float(chip_cfg.get("weight", 10))
    fund_w = float(fund_cfg.get("weight", 5))
    cat_w = float(cat_cfg.get("weight", 8))
    ind_w = float(ind_cfg.get("weight", 4))
    ind_top_n = int(ind_cfg.get("top_n", 5))
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

    log.info("Loading stock universe...")
    info = fetch_stock_info()
    universe = filter_tradable_stocks(info)
    log.info(f"Universe: {len(universe)} tradable stocks")

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
                df = upsert_prices(sid, inc) if not inc.empty else existing
            else:
                df = existing

        if historical:
            df = df[df.index.date <= today]

        if len(df) < 120:
            continue

        df_ind = compute_all(df)
        if index_close is not None:
            df_ind = compute_relative_strength(df_ind, index_close, n=60)

        last = df_ind.iloc[-1]
        close_v = last.get("close"); ma5_v = last.get("ma5")
        ma20_v = last.get("ma20"); ma60_v = last.get("ma60"); ma120_v = last.get("ma120")
        prev_c = df_ind["close"].iloc[-2] if len(df_ind) >= 2 else None
        chg = None
        if pd.notna(close_v) and prev_c is not None and pd.notna(prev_c) and prev_c:
            chg = round((close_v / prev_c - 1) * 100, 2)
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
                "close": float(close_v) if pd.notna(close_v) else None,
                "change_pct": chg,
                "valuation": valuation_snapshot.get(sid, {}),
            })
            scored.append(conv)

    # ---------- 排序 → 候選池 →(stage-2 重排:籌碼+基本面+催化劑)→ 核心 / 觀察 ----------
    scoring_cfg = (cfg.get("scoring", {}) or {})
    chip_cfg = scoring_cfg.get("chip_bonus", {}) or {}
    chip_on = bool(chip_cfg.get("enabled", False))
    cand_n = int(chip_cfg.get("candidate_count", core_count)) if chip_on else core_count

    trigger_sorted = sorted(
        [s for s in scored if s["trigger"] and s["score"] >= min_score],
        key=lambda x: -x["score"],
    )
    # 候選池:基礎信心分最高的觸發股(略多於 core_count,讓 stage-2 加成能改變誰進核心);受 enrich_top_n 上限保護 API
    core_candidates = trigger_sorted[:max(cand_n, core_count)][:enrich_top_n]

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
    with open(docs_dir / "data.json", "w", encoding="utf-8") as f:
        json.dump(_json_safe({
            "date": today.isoformat(),
            "generated_at": now_tpe().strftime("%Y-%m-%d %H:%M"),
            "index_below_ma20": index_below_ma20,
            "scored_count": len(scored),
            "core": core_clean,
            "watch": watch_clean,
            "watchlist": watchlist_clean,
            "performance": performance,
            "label": STRATEGY_LABEL,
        }), f, ensure_ascii=False, indent=2, default=str)

    # 每日選股快照(供網頁「歷史日期切換」)+ 日期索引
    hist_dir = docs_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    with open(hist_dir / f"{today.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump(_json_safe({
            "date": today.isoformat(),
            "index_below_ma20": index_below_ma20,
            "scored_count": len(scored),
            "core": core_clean, "watch": watch_clean, "watchlist": watchlist_clean,
            "label": STRATEGY_LABEL,
        }), f, ensure_ascii=False, indent=2, default=str)
    dates = sorted(p.stem for p in hist_dir.glob("*.json"))
    with open(docs_dir / "dates.json", "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False)

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
        "no_data_count": len(no_data),
        "label": STRATEGY_LABEL,
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
