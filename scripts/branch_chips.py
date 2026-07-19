"""券商分點籌碼(對標「籌碼K線」的分點調查局 / 秘密券商,NT$4,680/年)。

## 範圍:只掃 核心 + 觀察 + 自選 + 持倉(使用者 2026-07-19 決定)

全市場掃分點要 2852 次呼叫 ≈ 71 分鐘,而且**存不下**:2330 單日就 16,067 列
(顆粒度 = 分點 × 成交價),全市場推估每天數百萬列,公開 repo 直接爆。

而且沒必要 —— 對一檔今天沒量沒波動的股票,查主力進出沒有任何資訊量。
只掃你真的在看的那幾十檔,1 分鐘跑完。

## 只存聚合指標,不存原始列

每檔壓成一列:主力買超集中度、前 5 大買/賣分點、隔日沖比例。
16,067 列 → 1 列。這樣才存得下,也才是你真正會看的東西。

## 指標定義(全部看得見,不是黑盒子)

- **net_top5 / 買超集中度**:前 5 大買超分點的淨買超合計 ÷ 當日總成交量。
  愈高代表「少數分點在吃貨」,愈低代表散戶對敲。
- **concentration(集中度)**:(前5大買超 − 前5大賣超) ÷ 總成交量。正=籌碼集中。
- **day_trade_ratio(隔日沖嫌疑)**:同一分點當日 `min(buy, sell)` 合計 ÷ 總成交量。
  高代表當沖/隔日沖客多,隔天賣壓重 —— 突破訊號配上高隔日沖率要打折。
- **n_traders**:有交易的分點家數。太少代表流動性差。

⚠️ FinMind 分點資料**當晚 21:00 才發布**,所以這支只能盤後跑(建議 21:30)。
"""
from __future__ import annotations
import argparse
import json
import time
from collections import defaultdict

import pandas as pd

from .config import DATA_DIR, now_tpe
from .branch import _fetch_one_day          # 沿用既有抓取路徑,不要有兩套
from .utils import log

OUT_DIR = DATA_DIR / "chips_branch"
DOCS_DIR = DATA_DIR.parent / "docs"

# FinMind 對逐檔查詢的安全節奏。實測 IP-ban 門檻約 40 次/分,取 30 留餘裕。
SAFE_PER_MIN = 30
_SLEEP = 60.0 / SAFE_PER_MIN


def target_stocks() -> dict[str, str]:
    """核心 + 觀察 + 自選 + 持倉。持倉在 localStorage(不上傳,見隱私設計),
    所以這裡只能取得前三者 —— 持倉要查請自己加進 watchlist。"""
    out: dict[str, str] = {}
    try:
        data = json.loads((DOCS_DIR / "data.json").read_text(encoding="utf-8"))
        for key in ("core", "watch", "watchlist"):
            for s in (data.get(key) or []):
                sid = str(s.get("stock_id") or "")
                if sid:
                    out.setdefault(sid, s.get("name") or "")
    except Exception as e:
        log.warning(f"讀 data.json 失敗:{e}")
    return out


def _aggregate(rows: list[dict], stock_id: str, name: str, day: str) -> dict | None:
    """原始分點列 → 一列聚合指標。"""
    if not rows:
        return None
    buy: dict[str, float] = defaultdict(float)
    sell: dict[str, float] = defaultdict(float)
    for r in rows:
        t = r.get("securities_trader") or r.get("securities_trader_id") or "?"
        buy[t] += float(r.get("buy") or 0)
        sell[t] += float(r.get("sell") or 0)
    traders = set(buy) | set(sell)
    total = sum(buy.values()) + sum(sell.values())
    if total <= 0:
        return None
    net = {t: buy[t] - sell[t] for t in traders}
    top_buy = sorted(net.items(), key=lambda kv: -kv[1])[:5]
    top_sell = sorted(net.items(), key=lambda kv: kv[1])[:5]
    # 隔日沖嫌疑:同一分點當天既買又賣的重疊部分
    churn = sum(min(buy[t], sell[t]) for t in traders)
    vol = total / 2                                   # buy+sell 是雙邊計,成交量取一半
    return {
        "date": day, "stock_id": stock_id, "name": name,
        "n_traders": len(traders),
        "net_top5_buy": round(sum(v for _, v in top_buy), 0),
        "net_top5_sell": round(sum(v for _, v in top_sell), 0),
        "concentration": round((sum(v for _, v in top_buy) + sum(v for _, v in top_sell)) / vol, 4),
        "buy_concentration": round(sum(v for _, v in top_buy) / vol, 4),
        "day_trade_ratio": round(churn / vol, 4),
        "top_buy": json.dumps([{"t": t, "net": round(v)} for t, v in top_buy if v > 0], ensure_ascii=False),
        "top_sell": json.dumps([{"t": t, "net": round(v)} for t, v in top_sell if v < 0], ensure_ascii=False),
    }


def run(day: str | None = None, limit: int | None = None) -> dict:
    """掃目標股的分點並存聚合結果。回傳 {ok, day, n, path}。"""
    day = day or now_tpe().strftime("%Y-%m-%d")
    targets = target_stocks()
    if limit:
        targets = dict(list(targets.items())[:limit])
    if not targets:
        log.warning("沒有目標股(data.json 沒有 core/watch/watchlist),跳過。")
        return {"ok": False, "n": 0, "day": day, "reason": "no_targets"}

    log.info(f"分點掃描 {day}:{len(targets)} 檔,安全節奏 {SAFE_PER_MIN}/分 "
             f"→ 預估 {len(targets)/SAFE_PER_MIN:.1f} 分鐘")
    out = []
    for i, (sid, name) in enumerate(targets.items(), 1):
        try:
            rows = _fetch_one_day(sid, day)
            agg = _aggregate(rows, sid, name, day)
            if agg:
                out.append(agg)
        except Exception as e:
            log.warning(f"分點 {sid} 失敗(略過):{e}")
        if i < len(targets):
            time.sleep(_SLEEP)

    if not out:
        log.info(f"分點 {day} 無資料(可能未到 21:00 發布時間或非交易日)。")
        return {"ok": False, "n": 0, "day": day, "reason": "no_data"}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{day}.parquet"
    df = pd.DataFrame(out)
    df.to_parquet(path, compression="zstd", index=False)
    log.info(f"分點聚合已存檔:{len(df)} 檔 → {path}({path.stat().st_size/1024:.0f} KB)")
    return {"ok": True, "n": len(df), "day": day, "path": str(path)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="券商分點籌碼(核心+觀察+自選)")
    ap.add_argument("--day", help="日期 YYYY-MM-DD,預設今天")
    ap.add_argument("--limit", type=int, help="只跑前 N 檔(測試用)")
    a = ap.parse_args()
    print(json.dumps(run(day=a.day, limit=a.limit), ensure_ascii=False))
