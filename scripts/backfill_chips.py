"""一次性 backfill:把既有 data/chips/*.parquet 的缺洞補起來(Bug 3 的收尾步驟)。

背景:舊版增量抓取從 last+1 起、且合併用 concat+keep=last,導致三大法人/融資券/外資持股
出表時間差造成的當日缺值永遠補不回來(參見 storage.upsert_chips 註解)。程式面已改成
「last-4d 重疊回補 + combine_first」,之後每日跑會逐步自癒近幾天的洞;但更早的歷史缺洞
不會被日常增量觸及。本腳本對每檔重抓近 N 天籌碼,用新的 combine_first 語意合併回去補洞。

用法(需設好 FINMIND_TOKEN):
    python -m scripts.backfill_chips           # 重抓近 60 天,補所有既有 data/chips 檔
    python -m scripts.backfill_chips --days 90  # 自訂重抓天數
    python -m scripts.backfill_chips 2330 2317  # 只補指定股號

安全性:純補資料、不刪不覆蓋既有非 NaN 值(combine_first 保證新 NaN 不蓋舊值);
任一檔抓取失敗只記錄、跳過,不中斷整批。
"""
from __future__ import annotations
import argparse
import sys
from datetime import date, timedelta

from .config import assert_env
from .fetchers import fetch_chips_history
from .storage import CHIPS_DIR, upsert_chips
from .utils import log


def backfill(stock_ids: list[str] | None = None, days: int = 60) -> None:
    assert_env(require_mail=False, require_finmind=True)
    if stock_ids:
        ids = list(dict.fromkeys(stock_ids))
    else:
        ids = sorted(p.stem for p in CHIPS_DIR.glob("*.parquet"))
    if not ids:
        log.info("data/chips 沒有既有快取,無需 backfill。")
        return
    today = date.today()
    start = today - timedelta(days=days)
    log.info(f"Backfill chips:{len(ids)} 檔,重抓 {start} ~ {today}(近 {days} 天)")
    ok = 0
    for i, sid in enumerate(ids, 1):
        try:
            new = fetch_chips_history(sid, start, today)
            if not new.empty:
                upsert_chips(sid, new)   # combine_first:補洞、不蓋既有值
                ok += 1
        except Exception as e:
            log.warning(f"backfill {sid} 失敗(跳過):{e}")
        if i % 25 == 0:
            log.info(f"backfill 進度 {i}/{len(ids)}")
    log.info(f"Backfill 完成:{ok}/{len(ids)} 檔有新資料合併。")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Backfill data/chips 缺洞")
    p.add_argument("stock_ids", nargs="*", help="指定股號(不給則補全部既有快取)")
    p.add_argument("--days", type=int, default=60, help="重抓近幾天(預設 60)")
    args = p.parse_args(argv)
    backfill(args.stock_ids or None, days=args.days)


if __name__ == "__main__":
    main(sys.argv[1:])
