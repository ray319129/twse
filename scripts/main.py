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
)
from .storage import (
    load_prices, upsert_prices,
    load_chips, upsert_chips,
    load_revenue, upsert_revenue,
    load_eps, upsert_eps,
    load_per, upsert_per,
)
from .indicators import compute_all, reference_levels
from .screener import screen_stock, stock_summary
from .notify import render_email, send_email
from .utils import log


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
}

CHIP_STRATEGIES = {
    "inst_consecutive_buy",
    "foreign_holding_increase",
    "short_cover_with_buy",
}
FUND_STRATEGIES = {
    "monthly_revenue_growth",
    "eps_positive_high_yield",
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


def _need_extra_data(price_hits: dict, combos_cfg: list) -> tuple[bool, bool]:
    """Returns (need_chips, need_fundamentals) based on which combos this stock could potentially form."""
    need_chips = need_fund = False
    for combo in combos_cfg or []:
        reqs = combo.get("requires", [])
        chip_reqs = [r for r in reqs if r in CHIP_STRATEGIES]
        fund_reqs = [r for r in reqs if r in FUND_STRATEGIES]
        non_extra = [r for r in reqs if r not in CHIP_STRATEGIES and r not in FUND_STRATEGIES]
        if not (chip_reqs or fund_reqs):
            continue
        if all(price_hits.get(r, False) for r in non_extra):
            if chip_reqs:
                need_chips = True
            if fund_reqs:
                need_fund = True
    return need_chips, need_fund


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


def _update_per(stock_id: str) -> pd.DataFrame:
    new = fetch_per_yield(stock_id, days=10)
    if new.empty:
        return load_per(stock_id)
    return upsert_per(stock_id, new)


def _chip_summary(chips_df: pd.DataFrame | None) -> dict:
    if chips_df is None or chips_df.empty:
        return {}
    last = chips_df.iloc[-1]
    out: dict = {}
    if "inst_total" in chips_df.columns:
        streak = 0
        for v in reversed(chips_df["inst_total"].dropna().values):
            if v > 0:
                streak += 1
            else:
                break
        out["inst_total_today"] = int(last.get("inst_total", 0) or 0)
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

    log.info("Loading stock universe...")
    info = fetch_stock_info()
    universe = filter_tradable_stocks(info)
    log.info(f"Universe: {len(universe)} tradable stocks")

    market_map = dict(zip(universe["stock_id"], universe.get("type", pd.Series(["twse"] * len(universe)))))
    name_map = dict(zip(universe["stock_id"], universe["stock_name"]))
    industry_map = dict(zip(universe["stock_id"], universe.get("industry_category", pd.Series([""] * len(universe)))))

    market_results: list[dict] = []
    watchlist_results: list[dict] = []
    no_data: list[str] = []
    chips_fetched = 0
    fund_fetched = 0
    combos_cfg = cfg.get("combos", [])

    for sid, sname in name_map.items():
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

        if len(df) < 60:
            continue

        df_ind = compute_all(df)

        price_screen = screen_stock(df_ind, cfg)
        is_watch = sid in watchlist
        need_chips_combo, need_fund_combo = _need_extra_data(price_screen["hits"], combos_cfg)

        chips_df = revenue_df = eps_df = per_df = None
        if is_watch or need_chips_combo:
            chips_df = _update_chips(sid, today)
            chips_fetched += 1
        if is_watch or need_fund_combo:
            revenue_df = _update_revenue(sid)
            eps_df = _update_eps(sid)
            per_df = _update_per(sid)
            fund_fetched += 1

        full_screen = screen_stock(
            df_ind, cfg,
            chips_df=chips_df, revenue_df=revenue_df, eps_df=eps_df, per_df=per_df,
        )
        summary = stock_summary(sid, sname, df_ind, full_screen)
        summary["industry"] = industry_map.get(sid, "")

        if chips_df is not None:
            summary["chips"] = _chip_summary(chips_df)
        if revenue_df is not None or eps_df is not None or per_df is not None:
            summary["fundamentals"] = _fund_summary(revenue_df, eps_df, per_df)

        if is_watch or summary["combos"]:
            summary["levels"] = reference_levels(df_ind)

        if summary["combos"] or summary["hits"]:
            market_results.append(summary)

        if is_watch:
            summary = dict(summary)
            summary["note"] = watchlist[sid]
            summary["news"] = fetch_news(sid, sname, limit=5)
            watchlist_results.append(summary)

    log.info(
        f"Watchlist hits: {len(watchlist_results)}, market hits: {len(market_results)}, "
        f"chips fetched: {chips_fetched}, fundamentals fetched: {fund_fetched}"
    )

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_DIR / f"{today.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump({
            "date": today.isoformat(),
            "watchlist": watchlist_results,
            "market": market_results,
            "no_data_count": len(no_data),
            "chips_fetched": chips_fetched,
            "fund_fetched": fund_fetched,
        }, f, ensure_ascii=False, indent=2, default=str)

    by_combo: dict[str, list[dict]] = {}
    for r in market_results:
        for c in r["combos"]:
            by_combo.setdefault(c, []).append(r)

    single_hit_count = sum(1 for r in market_results if not r["combos"])

    ctx = {
        "date_str": today.strftime("%Y-%m-%d (%a)"),
        "watchlist": watchlist_results,
        "by_combo": by_combo,
        "combo_hit_count": sum(len(v) for v in by_combo.values()),
        "single_hit_count": single_hit_count,
        "no_data_count": len(no_data),
        "label": STRATEGY_LABEL,
        "test_mode": test_mode,
    }

    html = render_email("daily_email.html", ctx)
    subject_prefix = "[測試] " if test_mode else ""
    subject = (
        f"{subject_prefix}[台股選股] {today.strftime('%Y/%m/%d')} "
        f"自選池 {len(watchlist_results)} 檔 / 多訊號交集 {ctx['combo_hit_count']} 檔"
    )

    send_email(subject, html)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="Bypass trading-day check; subject prefixed [測試]")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    daily_run(test_mode=args.test)
