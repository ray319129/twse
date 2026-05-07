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
    fetch_price_history, fetch_institutional_history,
)
from .storage import (
    load_prices, upsert_prices,
    load_chips, upsert_chips,
)
from .indicators import compute_all
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
}

CHIP_STRATEGIES = {
    "inst_consecutive_buy",
    "foreign_holding_increase",
    "short_cover_with_buy",
}


def _is_trading_day(today: date) -> bool:
    """Heuristic: 2330 has data for today after market close."""
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


def _could_form_chip_combo(price_hits: dict, combos_cfg: list) -> bool:
    for combo in combos_cfg or []:
        reqs = combo.get("requires", [])
        chip_reqs = [r for r in reqs if r in CHIP_STRATEGIES]
        non_chip_reqs = [r for r in reqs if r not in CHIP_STRATEGIES]
        if not chip_reqs:
            continue
        if all(price_hits.get(r, False) for r in non_chip_reqs):
            return True
    return False


def _update_chips(stock_id: str, today: date, history_days: int = 30) -> pd.DataFrame:
    """Incremental chips fetch + cache merge."""
    existing = load_chips(stock_id)
    if not existing.empty:
        last = existing.index.max().date()
        if last >= today:
            return existing
        start = last + timedelta(days=1)
    else:
        start = today - timedelta(days=history_days * 2)

    new = fetch_institutional_history(stock_id, start, today)
    if new.empty:
        return existing
    return upsert_chips(stock_id, new)


def _last_chip_summary(chips_df: pd.DataFrame) -> dict:
    if chips_df is None or chips_df.empty:
        return {}
    last = chips_df.iloc[-1]
    streak = 0
    for v in reversed(chips_df["inst_total"].values):
        if v > 0:
            streak += 1
        else:
            break
    return {
        "inst_total_today": int(last.get("inst_total", 0) or 0),
        "inst_buy_streak": int(streak),
    }


def daily_run(test_mode: bool = False) -> None:
    assert_env()
    cfg = load_screeners()
    watchlist = load_watchlist()
    today = now_tpe().date()

    if not _is_trading_day(today):
        log.info(f"{today} appears to be non-trading day; skipping email")
        return

    log.info("Loading stock universe...")
    info = fetch_stock_info()
    universe = filter_tradable_stocks(info)
    log.info(f"Universe: {len(universe)} tradable stocks")

    market_map = dict(zip(universe["stock_id"], universe.get("type", pd.Series(["twse"] * len(universe)))))
    name_map = dict(zip(universe["stock_id"], universe["stock_name"]))

    market_results: list[dict] = []
    watchlist_results: list[dict] = []
    no_data: list[str] = []
    chips_fetched = 0

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
        needs_chips = is_watch or _could_form_chip_combo(price_screen["hits"], cfg.get("combos", []))

        chips_df = None
        if needs_chips:
            chips_df = _update_chips(sid, today)
            chips_fetched += 1

        full_screen = screen_stock(df_ind, cfg, chips_df=chips_df) if needs_chips else price_screen
        summary = stock_summary(sid, sname, df_ind, full_screen)

        if needs_chips:
            summary["chips"] = _last_chip_summary(chips_df) if chips_df is not None else {}

        if summary["combos"] or summary["hits"]:
            market_results.append(summary)

        if is_watch:
            summary = dict(summary)
            summary["note"] = watchlist[sid]
            summary["news"] = fetch_news(sid, sname, limit=5)
            watchlist_results.append(summary)

    log.info(
        f"Watchlist hits: {len(watchlist_results)}, market hits: {len(market_results)}, "
        f"chips fetched: {chips_fetched}"
    )

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_DIR / f"{today.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump({
            "date": today.isoformat(),
            "watchlist": watchlist_results,
            "market": market_results,
            "no_data_count": len(no_data),
            "chips_fetched": chips_fetched,
        }, f, ensure_ascii=False, indent=2)

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
    p.add_argument("--test", action="store_true", help="Run pipeline + send email with [測試] prefix")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    daily_run(test_mode=args.test)
