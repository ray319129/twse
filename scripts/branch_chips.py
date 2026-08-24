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

# 每晚跑完往回檢查幾個交易日的缺口。取 10 是配合 net_series 的長度,
# 也讓「某天補不到」的情況最多重試 10 個交易日就自然停手,不會永遠重試。
CATCHUP_DAYS = 10


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
        # 分點是 Sponsor 級資料集。訂閱到期後 API 一律回空,但舊訊息把它講成
        # 「可能未到 21:00 或非交易日」—— 2026-08-24 那次跑在台北 22:23(早就過 21:00)
        # 還是印這句,結果是資料從 08-17 起靜靜停更一週都沒人看得出原因。
        # 先問訂閱狀態再決定怎麼講。
        try:
            from .quotes import sponsor_status
            st = sponsor_status()
        except Exception:
            st = {}
        if not st.get("active"):
            exp = st.get("expires") or "未知"
            log.warning(f"分點 {day} 無資料:**FinMind 訂閱未生效/已到期**(到期日 {exp}"
                        f",目前等級 {st.get('level_title') or 'Free'})。"
                        f"分點是 Sponsor 級資料集,沒有訂閱就抓不到 —— 這不是時間或交易日的問題。")
            return {"ok": False, "n": 0, "day": day, "reason": "no_sponsor"}
        log.info(f"分點 {day} 無資料(可能未到 21:00 發布時間或非交易日)。")
        return {"ok": False, "n": 0, "day": day, "reason": "no_data"}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{day}.parquet"
    df = pd.DataFrame(out)
    df.to_parquet(path, compression="zstd", index=False)
    log.info(f"分點聚合已存檔:{len(df)} 檔 → {path}({path.stat().st_size/1024:.0f} KB)")
    return {"ok": True, "n": len(df), "day": day, "path": str(path)}


# ---------- 分點連買/連賣天數 ----------
#
# 「主力連買 N 日」是籌碼K線那類軟體的招牌欄位。定義得講清楚,不然只是個看起來厲害的數字:
#
#   主力淨買超(當日) = 前 5 大買超分點淨買超 + 前 5 大賣超分點淨買超
#                      (後者是負值,所以是相減的意思)
#   連買 N 日 = 從最近一個交易日往回數,主力淨買超連續為正的天數
#   連賣 N 日 = 同理,連續為負
#
# ⚠️ 用「前 5 大」而不是全部分點,是因為全部分點加總恆等於 0(有買必有賣),算出來沒有意義。
# ⚠️ 需要歷史。`run()` 從執行當天起才有,所以另外提供 `backfill()` 回補。

def _trading_days(n: int, end: str | None = None) -> list[str]:
    """最近 n 個交易日(用本機價格檔推,不打 API)。"""
    from .storage import load_prices
    try:
        df = load_prices("2330")
        days = [str(d)[:10] for d in df.index]
        if end:
            days = [d for d in days if d <= end]
        return days[-n:]
    except Exception:
        return []


def backfill(days: int = 15, limit: int | None = None) -> dict:
    """回補最近 N 個交易日的分點聚合(連買連賣要有歷史才算得出來)。
    30 檔 × 15 日 ≈ 450 次呼叫,依安全節奏約 15 分鐘。已存在的日期會跳過。"""
    from .branch import fetch_branch_daily
    targets = target_stocks()
    if limit:
        targets = dict(list(targets.items())[:limit])
    dates = _trading_days(days)
    if not targets or not dates:
        return {"ok": False, "reason": "no_targets_or_dates", "n": 0}
    have = {p.stem for p in OUT_DIR.glob("*.parquet")} if OUT_DIR.exists() else set()
    todo = [d for d in dates if d not in have]
    log.info(f"分點回補:{len(targets)} 檔 × {len(todo)} 日(已有 {len(dates)-len(todo)} 日)")
    per_day: dict[str, list] = {d: [] for d in todo}
    for sid, name in targets.items():
        if not todo:
            break
        try:
            g = fetch_branch_daily(sid, todo, max_workers=4)   # 節制併發,避免 IP ban
        except Exception as e:
            log.warning(f"分點回補 {sid} 失敗:{e}")
            continue
        if g is None or g.empty:
            continue
        for d, sub in g.groupby("date"):
            d = str(d)[:10]
            if d not in per_day:
                continue
            agg = _agg_from_long(sub, sid, name, d)
            if agg:
                per_day[d].append(agg)
        time.sleep(_SLEEP)
    written = 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for d, rows in per_day.items():
        if not rows:
            continue
        pd.DataFrame(rows).to_parquet(OUT_DIR / f"{d}.parquet", compression="zstd", index=False)
        written += 1
    log.info(f"分點回補完成:寫入 {written} 個交易日")
    return {"ok": True, "days": written, "n": len(targets)}


def _agg_from_long(sub: pd.DataFrame, sid: str, name: str, day: str) -> dict | None:
    """`branch.fetch_branch_daily` 的 long 表 → 與 `_aggregate` 相同欄位的一列。"""
    if sub is None or sub.empty:
        return None
    total = float(sub["buy"].sum() + sub["sell"].sum())
    if total <= 0:
        return None
    vol = total / 2
    s = sub.sort_values("net", ascending=False)
    top_buy = s.head(5)[["trader", "net"]].values.tolist()
    top_sell = s.tail(5)[["trader", "net"]].values.tolist()
    nb = float(s.head(5)["net"].sum())
    ns = float(s.tail(5)["net"].sum())
    return {
        "date": day, "stock_id": sid, "name": name,
        "n_traders": int(sub["trader_id"].nunique()),
        "net_top5_buy": round(nb), "net_top5_sell": round(ns),
        "concentration": round((nb + ns) / vol, 4),
        "buy_concentration": round(nb / vol, 4),
        "day_trade_ratio": round(float(sub["churn"].sum()) / vol, 4),
        "top_buy": json.dumps([{"t": t, "net": round(float(n))} for t, n in top_buy if n > 0], ensure_ascii=False),
        "top_sell": json.dumps([{"t": t, "net": round(float(n))} for t, n in top_sell if n < 0], ensure_ascii=False),
    }


def compute_streaks() -> dict:
    """讀所有已存的分點聚合,算每檔的連買/連賣天數,寫 docs/branch_streak.json。
    回傳 {stock_id: {streak, dir, days_used, last_date, net_series}}。
    `streak` 恆為正整數,方向看 `dir`('buy'/'sell'/'flat')。"""
    files = sorted(OUT_DIR.glob("*.parquet")) if OUT_DIR.exists() else []
    if not files:
        log.info("沒有分點歷史,連買連賣跳過(先跑 --backfill)。")
        return {}
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            continue
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df["main_net"] = df["net_top5_buy"] + df["net_top5_sell"]
    out = {}
    for sid, sub in df.groupby("stock_id"):
        sub = sub.sort_values("date")
        nets = sub["main_net"].tolist()
        if not nets:
            continue
        sign = 1 if nets[-1] > 0 else (-1 if nets[-1] < 0 else 0)
        streak = 0
        if sign:
            for v in reversed(nets):
                if (v > 0 and sign > 0) or (v < 0 and sign < 0):
                    streak += 1
                else:
                    break
        out[str(sid)] = {
            "streak": streak,
            "dir": "buy" if sign > 0 else ("sell" if sign < 0 else "flat"),
            "days_used": len(nets),
            "last_date": str(sub["date"].iloc[-1])[:10],
            "last_net": round(float(nets[-1])),
            # 最近 10 日的主力淨買超,給前端畫小柱狀(正負一眼看得出來)
            "net_series": [round(float(v)) for v in nets[-10:]],
        }
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "branch_streak.json").write_text(
        json.dumps({"updated": now_tpe().strftime("%Y-%m-%d %H:%M"), "streaks": out},
                   ensure_ascii=False), encoding="utf-8")
    log.info(f"分點連買連賣已更新:{len(out)} 檔(歷史 {len(files)} 個交易日)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="券商分點籌碼(核心+觀察+自選)")
    ap.add_argument("--day", help="日期 YYYY-MM-DD,預設今天")
    ap.add_argument("--limit", type=int, help="只跑前 N 檔(測試用)")
    ap.add_argument("--backfill", type=int, metavar="DAYS", help="回補最近 N 個交易日")
    ap.add_argument("--streaks", action="store_true", help="只重算連買連賣")
    ap.add_argument("--no-catchup", action="store_true",
                    help="跑完不補前幾日的缺口(預設會補)")
    a = ap.parse_args()
    if a.backfill:
        print(json.dumps(backfill(a.backfill, limit=a.limit), ensure_ascii=False))
        compute_streaks()
    elif a.streaks:
        s = compute_streaks()
        for sid, v in list(s.items())[:10]:
            print(f"  {sid} 連{'買' if v['dir']=='buy' else '賣' if v['dir']=='sell' else '平'} "
                  f"{v['streak']} 日(歷史 {v['days_used']} 日)")
    else:
        print(json.dumps(run(day=a.day, limit=a.limit), ensure_ascii=False))
        # 當晚 job 掛掉(2026-08-06 就是 GitHub Actions 大規模故障)那天的資料就永久缺一格,
        # 沒有任何地方會發現 —— 近 20 個交易日實際缺了 07-27 / 08-03 / 08-06 三天。
        # 缺口對 compute_streaks() 是隱形的:它只按日期排序數連續同號,跨過缺口的
        # 「連買 5 日」其實不連續。所以跑完順手補。
        # backfill() 會跳過已存在的日期,沒缺口時 todo 為空、一次 API 都不打。
        if not a.no_catchup:
            print(json.dumps(backfill(CATCHUP_DAYS, limit=a.limit), ensure_ascii=False))
        compute_streaks()
