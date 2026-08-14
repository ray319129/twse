"""重算 Swing Score 的橫斷面校準表(scripts/health/scoring.py 的 _DOLLAR_VOL_PCTL / _ATR_PCT_PCTL)。

為什麼是「印出來手動貼」而不是寫成資料檔:
  .vercelignore 排除整個 data/,即時健檢(api/health.py)在 Vercel 上讀不到任何資料檔。
  如果校準表存成 data/meta/*.json,批次路徑有、即時路徑沒有 → 同一檔股票在兩條路徑會
  拿到不同的當沖分數。寫死在程式碼裡是唯一能讓兩條路徑一致的做法。

用法(建議每半年跑一次,或發現分數分布明顯偏移時):
    python tools/refresh_swing_calibration.py
然後把輸出的兩行常數貼回 scripts/health/scoring.py,並更新註解裡的日期與樣本數。
"""
from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from scripts import storage
from scripts.indicators import compute_all

PCTLS = [10, 25, 50, 75, 90, 95, 99]


def main() -> None:
    dollar_vol: list[float] = []
    atr_pct: list[float] = []
    skipped = 0

    paths = sorted(glob.glob(os.path.join("data", "prices", "*.parquet")))
    for p in paths:
        sid = os.path.basename(p)[:-8]
        try:
            df = storage.load_prices(sid)
            if df is None or len(df) < 60:
                skipped += 1
                continue
            last = compute_all(df.copy()).iloc[-1]
            close, vma20, atr14 = last.get("close"), last.get("vol_ma20"), last.get("atr14")
            if pd.isna(close) or pd.isna(vma20) or pd.isna(atr14) or not close:
                skipped += 1
                continue
            dollar_vol.append(float(close * vma20 / 1e6))
            atr_pct.append(float(atr14 / close * 100))
        except Exception:
            skipped += 1

    if not dollar_vol:
        print("沒有可用樣本,請先確認 data/prices/ 有資料。")
        return

    dv = np.array(dollar_vol)
    at = np.array(atr_pct)
    dv_tbl = [(round(float(np.percentile(dv, q)), 1), q) for q in PCTLS]
    at_tbl = [(round(float(np.percentile(at, q)), 2), q) for q in PCTLS]

    print(f"樣本 {len(dv)} 檔(略過 {skipped} 檔:K棒不足或指標算不出來)")
    print()
    print("貼回 scripts/health/scoring.py:")
    print(f"_DOLLAR_VOL_PCTL = {dv_tbl}")
    print(f"_ATR_PCT_PCTL = {at_tbl}")
    print()
    print("順帶檢查(舊版固定上限的飽和率,若仍偏高代表校準表確實該更新):")
    print(f"  日均成交額 ≥ 300 百萬:{(dv >= 300).mean() * 100:.1f}%")
    print(f"  ATR% ≥ 6:{(at >= 6).mean() * 100:.1f}%")


if __name__ == "__main__":
    main()
