from __future__ import annotations
import argparse
import json
from datetime import date, timedelta

import pandas as pd

from .config import (
    assert_env, load_screeners, load_watchlist,
    SIGNALS_DIR, now_tpe,
)
from .fetchers import (
    fetch_stock_info, filter_tradable_stocks, fetch_news,
    fetch_price_history, fetch_chips_history,
    fetch_monthly_revenue, fetch_eps_quarterly, fetch_per_yield,
    fetch_valuation_snapshot, fetch_index_history,
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
                 news: bool = False, screen_cfg: dict | None = None) -> dict:
    """為入榜股票補:技術座標(免費)+ FinMind 籌碼/財報。原地更新並回傳同一 dict。"""
    sid = pick["stock_id"]; sname = pick.get("name", "")
    df = load_prices(sid)
    if not df.empty and len(df) >= 60:
        df_ind = compute_all(df)
        if index_close is not None:
            df_ind = compute_relative_strength(df_ind, index_close, n=60)
        pick["levels"] = reference_levels(df_ind)
        if screen_cfg is not None:
            scr = screen_stock(df_ind, screen_cfg, valuation=pick.get("valuation"))
            pick["hits"] = [h for h, v in scr["hits"].items() if v]
            pick["combos"] = scr["combos"]
    chips_df = _update_chips(sid, today)
    if chips_df is not None and not chips_df.empty:
        cs = _chip_summary(chips_df)
        if cs:
            pick["chips"] = cs
    if fundamentals:
        revenue_df = _update_revenue(sid)
        eps_df = _update_eps(sid)
        if (revenue_df is not None and not revenue_df.empty) or (eps_df is not None and not eps_df.empty):
            pick["fundamentals"] = _fund_summary(revenue_df, eps_df, None)
    if news:
        pick["news"] = fetch_news(sid, sname, limit=5)
    return pick


def _clean_for_json(d: dict) -> dict:
    """移除不可序列化 / 過大的鍵。"""
    return {k: v for k, v in d.items() if k != "df_ind"}


def daily_run(test_mode: bool = False) -> None:
    assert_env()
    cfg = load_screeners()
    watchlist = load_watchlist()
    today = now_tpe().date()

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
    score_cfg = {"min_dollar_volume": float(rank_cfg.get("min_dollar_volume", 30_000_000))}

    log.info("Loading stock universe...")
    info = fetch_stock_info()
    universe = filter_tradable_stocks(info)
    log.info(f"Universe: {len(universe)} tradable stocks")

    valuation_snapshot = fetch_valuation_snapshot(today)

    # 大盤指數(相對強度用),整個 run 只抓一次
    index_df = fetch_index_history(days=400)
    index_close = index_df["close"] if not index_df.empty else None
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
            new_df = fetch_price_history(sid, market, days=400)
            if new_df.empty:
                no_data.append(sid)
                continue
            df = upsert_prices(sid, new_df)
        else:
            last_date = existing.index.max().date()
            if (today - last_date).days >= 1:
                inc = fetch_price_history(sid, market, days=10)
                df = upsert_prices(sid, inc) if not inc.empty else existing
            else:
                df = existing

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

    # ---------- 排序 → 核心 / 觀察 ----------
    core = sorted(
        [s for s in scored if s["trigger"] and s["score"] >= min_score],
        key=lambda x: -x["score"],
    )[:core_count]
    core_ids = {s["stock_id"] for s in core}
    watch = sorted(
        [s for s in scored if s["brewing"] and not s["trigger"]
         and s["score"] >= min_score and s["stock_id"] not in core_ids],
        key=lambda x: -x["score"],
    )[:watch_count]

    industry_trends = compute_industry_trends(industry_rows)
    hot_industries = [t["industry"] for t in industry_trends[:HOT_INDUSTRY_TOP_N]]
    hot_set = set(hot_industries)
    for s in core + watch:
        s["hot_industry"] = s.get("industry", "") in hot_set

    # ---------- 第二遍:只對核心 + 自選池補抓 FinMind(控制 API 額度) ----------
    # 觀察層表格只用評分階段已有的欄位,不補抓 → 把稀缺的 FinMind 額度留給真正要進場的核心。
    for s in core[:enrich_top_n]:
        _enrich_pick(s, today, index_close, fundamentals=True, screen_cfg=cfg)

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
        json.dump({
            "date": today.isoformat(),
            "core": [_clean_for_json(s) for s in core],
            "watch": [_clean_for_json(s) for s in watch],
            "watchlist": [_clean_for_json(s) for s in watchlist_results],
            "industry_trends": industry_trends,
            "scored_count": len(scored),
            "no_data_count": len(no_data),
        }, f, ensure_ascii=False, indent=2, default=str)

    ctx = {
        "date_str": today.strftime("%Y-%m-%d (%a)"),
        "core": core,
        "watch": watch,
        "watchlist": watchlist_results,
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
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    daily_run(test_mode=args.test)
