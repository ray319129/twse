from __future__ import annotations
import argparse
import time

from .config import assert_env
from .fetchers import fetch_stock_info, filter_tradable_stocks, fetch_price_history
from .storage import load_prices, upsert_prices, save_prices, flush_prices
from .utils import log


def backfill(days: int = 400, only_missing: bool = True, sleep: float = 0.4) -> None:
    assert_env(require_mail=False)
    info = fetch_stock_info(force=True)
    universe = filter_tradable_stocks(info)
    log.info(f"Backfilling {len(universe)} stocks, {days} days each")

    fetched = 0
    skipped = 0
    failed = 0

    for i, row in universe.iterrows():
        sid = row["stock_id"]
        market = row.get("type", "twse")

        existing = load_prices(sid)
        if only_missing and not existing.empty and len(existing) >= days * 0.6:
            skipped += 1
            continue

        df = fetch_price_history(sid, market, days=days)
        if df.empty:
            failed += 1
        else:
            # 補史是「整段抓」,直接寫 base 才對:走 upsert 會把幾百根 K 棒全塞進 tail 月檔,
            # 反而把當月檔撐大好幾十倍(tail 的設計前提是每天只加一根)。
            if existing.empty:
                save_prices(sid, df)
            else:
                upsert_prices(sid, df)
            fetched += 1

        if (i + 1) % 50 == 0:
            flush_prices()
            log.info(f"progress {i+1}/{len(universe)} fetched={fetched} skipped={skipped} failed={failed}")

        time.sleep(sleep)

    flush_prices()
    log.info(f"Backfill done: fetched={fetched} skipped={skipped} failed={failed}")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--all", action="store_true", help="Refetch even if data exists")
    p.add_argument("--sleep", type=float, default=0.4)
    return p.parse_args()


if __name__ == "__main__":
    a = parse_args()
    backfill(days=a.days, only_missing=not a.all, sleep=a.sleep)
