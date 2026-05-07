from __future__ import annotations
import argparse
import json
from datetime import date

import pandas as pd

from .config import (
    assert_env, load_screeners, load_watchlist,
    SIGNALS_DIR, now_tpe,
)
from .fetchers import fetch_stock_info, filter_tradable_stocks, fetch_news, fetch_price_history
from .storage import load_prices, upsert_prices, save_prices
from .indicators import compute_all
from .screener import screen_stock, stock_summary
from .notify import render_email, send_email
from .utils import log


# Friendly Chinese names per strategy key (for email display)
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
}


def daily_run(test_mode: bool = False) -> None:
    assert_env()
    cfg = load_screeners()
    watchlist = load_watchlist()

    log.info("Loading stock universe...")
    info = fetch_stock_info()
    universe = filter_tradable_stocks(info)
    log.info(f"Universe: {len(universe)} tradable stocks")

    market_map = dict(zip(universe["stock_id"], universe.get("type", pd.Series(["twse"] * len(universe)))))
    name_map = dict(zip(universe["stock_id"], universe["stock_name"]))

    today = now_tpe().date()

    market_results: list[dict] = []
    watchlist_results: list[dict] = []
    no_data: list[str] = []

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
                if not inc.empty:
                    df = upsert_prices(sid, inc)
                else:
                    df = existing
            else:
                df = existing

        if len(df) < 60:
            continue

        df_ind = compute_all(df)
        screen = screen_stock(df_ind, cfg)
        summary = stock_summary(sid, sname, df_ind, screen)

        if summary["combos"] or summary["hits"]:
            market_results.append(summary)

        if sid in watchlist:
            summary["note"] = watchlist[sid]
            summary["news"] = fetch_news(sid, sname, limit=5)
            watchlist_results.append(summary)

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_DIR / f"{today.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump({
            "date": today.isoformat(),
            "watchlist": watchlist_results,
            "market": market_results,
            "no_data_count": len(no_data),
        }, f, ensure_ascii=False, indent=2)

    log.info(f"Watchlist hits: {len(watchlist_results)}, market hits: {len(market_results)}")

    by_combo: dict[str, list[dict]] = {}
    for r in market_results:
        for c in r["combos"]:
            by_combo.setdefault(c, []).append(r)

    ctx = {
        "date_str": today.strftime("%Y-%m-%d (%a)"),
        "watchlist": watchlist_results,
        "by_combo": by_combo,
        "market_total": len(market_results),
        "no_data_count": len(no_data),
        "label": STRATEGY_LABEL,
        "test_mode": test_mode,
    }

    html = render_email("daily_email.html", ctx)
    subject_prefix = "[測試] " if test_mode else ""
    subject = (
        f"{subject_prefix}[台股選股] {today.strftime('%Y/%m/%d')} "
        f"自選池 {len(watchlist_results)} 檔 / 全市場符合 {len(market_results)} 檔"
    )

    send_email(subject, html)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="Run pipeline + send email with [測試] prefix")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    daily_run(test_mode=args.test)
