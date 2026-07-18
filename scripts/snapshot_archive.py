"""全市場快照存檔(鐵則三)—— 訂閱斷掉後,存下來的資料仍然是我們的。

## 為什麼

FinMind Sponsor 是按月訂閱(實測 2026-07-17 ~ 08-17,隨時可能不續)。快照裡的
**均價(VWAP)、量比、委買賣**是我們原本完全沒有、事後也補不回來的欄位 ——
歷史快照沒有 API 可以回補,今天沒存就永遠沒有。

一天 7 個檢查點 × 171 KB = **1.2 MB/日、23 MB/月**,這是公開 repo 吃得下的量。
(相對地每分鐘存一次 = 900 MB/月,repo 會爆 —— 額度撐得住不代表儲存撐得住。)

## 檢查點的選法

不是均勻取樣,是挑「盤中型態研究會問到的時點」:

    0900 開盤           跳空/開盤缺口 —— 台帳裡 27 筆跳空棄單,被棄的平均 -18.94%
    0930 開盤半小時     開盤 30 分強弱 → 收盤,經典當沖命題
    1000 / 1100 / 1200  盤中走勢(是否守住開盤方向)
    1300 尾盤前
    1330 收盤           當日 VWAP / 量比定案 —— 因子研究主要用這一份

## 檔案格式

`data/snapshots/YYYY-MM/YYYY-MM-DD.parquet`,一天一檔、含當天所有檢查點,
多一個 `snap_tag` 欄位(0900/0930/…)。按月分目錄免得單一目錄檔案數爆掉。

同一個 tag 重跑會覆蓋該 tag 的列(補跑安全,不會產生重複)。
"""
from __future__ import annotations
import argparse
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, now_tpe
from .quotes import fetch_snapshot_all, sponsor_status
from .utils import log

SNAP_DIR = DATA_DIR / "snapshots"

# 檢查點標籤 → 大約的台北時間。實際由外部 cron 在對應時間觸發;
# 這裡只用來當檔案內的欄位值與人工補跑的參數。
CHECKPOINTS = ["0900", "0930", "1000", "1100", "1200", "1300", "1330"]

# 存檔只留研究會用到的欄位。change_price/amount 可由其他欄位推回,不重複存。
KEEP = ["stock_id", "name", "open", "high", "low", "close", "change_rate",
        "average_price", "total_volume", "total_amount", "yesterday_volume",
        "volume_ratio", "buy_price", "buy_volume", "sell_price", "sell_volume",
        "date"]


def _path_for(day: str) -> Path:
    return SNAP_DIR / day[:7] / f"{day}.parquet"


def archive_snapshot(tag: str | None = None, day: str | None = None) -> dict:
    """抓一次全市場快照並併進當天的檔案。回傳 {ok, tag, day, rows, path, reason}。
    **任何失敗都只回 ok=False,不 raise** —— 這是背景存檔,不該擋住任何主流程。"""
    now = now_tpe()
    tag = tag or now.strftime("%H%M")
    day = day or now.strftime("%Y-%m-%d")

    st = sponsor_status()
    if not st.get("active"):
        log.info(f"非 Sponsor 或訂閱已到期({st.get('level_title') or 'n/a'}),略過快照存檔。")
        return {"ok": False, "reason": "no_sponsor", "tag": tag, "day": day, "rows": 0}

    df = fetch_snapshot_all(force=True)
    if df.empty:
        return {"ok": False, "reason": "empty", "tag": tag, "day": day, "rows": 0}

    # 快照的 date 欄是資料本身的時間戳。若它的日期不是今天,代表市場沒開(假日/颱風假)
    # 或資料還沒更新 —— 存進去只會污染歷史,直接跳過。
    stamp = str(df["date"].iloc[0])[:10] if "date" in df.columns else ""
    if stamp and stamp != day:
        log.info(f"快照時間戳為 {stamp} 而非 {day}(非交易日或尚未更新),略過存檔。")
        return {"ok": False, "reason": f"stale:{stamp}", "tag": tag, "day": day, "rows": 0}

    df = df[[c for c in KEEP if c in df.columns]].copy()
    df.insert(0, "snap_tag", tag)
    df.insert(0, "snap_date", day)

    path = _path_for(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old = pd.read_parquet(path)
            old = old[old["snap_tag"] != tag]          # 同 tag 重跑 → 覆蓋,不重複
            df = pd.concat([old, df], ignore_index=True)
        except Exception as e:
            log.warning(f"既有快照檔讀取失敗,將直接覆寫:{e}")
    df.to_parquet(path, compression="zstd", index=False)

    size_kb = path.stat().st_size / 1024
    log.info(f"快照已存檔 {day} {tag}:{len(df)} 列(含當日全部檢查點),{size_kb:.0f} KB → {path}")
    return {"ok": True, "tag": tag, "day": day, "rows": len(df), "path": str(path)}


def load_snapshots(day: str) -> pd.DataFrame:
    """讀某日的全部檢查點。沒有就回空 DataFrame。"""
    p = _path_for(day)
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(p)
    except Exception as e:
        log.warning(f"快照 {day} 讀取失敗:{e}")
        return pd.DataFrame()


def archive_stats() -> dict:
    """存檔總覽(給網頁顯示「我們累積了多少即時歷史」)。"""
    files = sorted(SNAP_DIR.glob("*/*.parquet"))
    if not files:
        return {"days": 0, "mb": 0.0, "first": "", "last": ""}
    total = sum(f.stat().st_size for f in files)
    return {"days": len(files), "mb": round(total / 1024 / 1024, 1),
            "first": files[0].stem, "last": files[-1].stem}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="全市場即時快照存檔")
    ap.add_argument("--tag", help=f"檢查點標籤,預設用現在時間。常用:{'/'.join(CHECKPOINTS)}")
    ap.add_argument("--day", help="覆寫日期(YYYY-MM-DD),預設今天")
    ap.add_argument("--stats", action="store_true", help="只顯示存檔總覽")
    a = ap.parse_args()
    if a.stats:
        print(archive_stats())
    else:
        print(archive_snapshot(tag=a.tag, day=a.day))
