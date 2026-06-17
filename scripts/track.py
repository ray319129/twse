from __future__ import annotations
import json
import glob
from datetime import date

import pandas as pd

from .config import SIGNALS_DIR
from .storage import load_prices

"""歷史追蹤與績效分析系統。

每天執行時自動更新「過去所有核心選股」的後續績效,純讀 data/signals/*.json + data/prices/*.parquet,
不需任何 API。產出:
  1. 逐檔追蹤台帳(選股日/股票/成本價/最新價/天數/報酬率/期間最高漲幅),依報酬率排序
  2. 勝率統計(總選股數 / 獲利 / 虧損 / 勝率)
  3. 各天期平均報酬(隔日/3/5/10/20/30 日)
  4. 短線最高漲幅模組(各天期區間內以最高價計的最大漲幅 — 最佳出場參考)
"""

HORIZONS = [1, 3, 5, 10, 20, 30]   # 交易日
ACTIVE_TRADING_DAYS = 30           # 台帳只列「仍在追蹤(<=30 交易日)」的選股
LEDGER_EMAIL_CAP = 50              # 郵件台帳最多列幾列(完整見 performance.json)


def _load_core_picks() -> list[dict]:
    """從所有歷史 signals JSON 抽出核心選股(舊格式無 'core' 鍵,自動略過)。"""
    picks: list[dict] = []
    for f in sorted(glob.glob(str(SIGNALS_DIR / "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        sig_date = d.get("date")
        if not sig_date:
            continue
        for p in d.get("core", []):
            sid = p.get("stock_id"); entry = p.get("close")
            if not sid or entry in (None, 0):
                continue
            picks.append({
                "date": sig_date, "stock_id": sid, "name": p.get("name", ""),
                "entry": float(entry), "score": p.get("score"), "profile": p.get("profile"),
            })
    # 同一檔同一天可能重複(理論上不會),去重
    seen = set(); uniq = []
    for p in picks:
        key = (p["date"], p["stock_id"])
        if key not in seen:
            seen.add(key); uniq.append(p)
    return uniq


def _pick_perf(df: pd.DataFrame, sig_date: str, entry: float, as_of: date) -> dict | None:
    """單檔:各天期收盤報酬 + 各天期區間最高漲幅 + 截至今日(最新)報酬。"""
    if df is None or df.empty or "close" not in df.columns:
        return None
    pos = df.index.get_indexer([pd.Timestamp(sig_date)])
    if len(pos) == 0 or pos[0] == -1:
        return None
    p0 = int(pos[0]); n = len(df)
    high = df["high"] if "high" in df.columns else df["close"]
    out = {"rets": {}, "maxgain": {}}
    for h in HORIZONS:
        tp = p0 + h
        if tp < n:
            c = df["close"].iloc[tp]
            out["rets"][h] = float(c / entry - 1) if pd.notna(c) and entry else None
            hi = high.iloc[p0 + 1:tp + 1].max()
            out["maxgain"][h] = float(hi / entry - 1) if pd.notna(hi) and entry else None
        else:
            out["rets"][h] = None; out["maxgain"][h] = None
    # 最新(截至今日)
    last = n - 1
    if last > p0:
        lc = df["close"].iloc[last]
        out["latest_ret"] = float(lc / entry - 1) if pd.notna(lc) and entry else None
        out["latest_close"] = float(lc) if pd.notna(lc) else None
        hi_all = high.iloc[p0 + 1:last + 1].max()
        out["peak_price"] = float(hi_all) if pd.notna(hi_all) else None
        out["peak_gain"] = float(hi_all / entry - 1) if pd.notna(hi_all) and entry else None
        out["trading_elapsed"] = last - p0
    else:
        out["latest_ret"] = 0.0; out["latest_close"] = entry
        out["peak_price"] = entry; out["peak_gain"] = 0.0; out["trading_elapsed"] = 0
    out["days_elapsed"] = (as_of - date.fromisoformat(sig_date)).days
    return out


def build_report(index_close: pd.Series | None = None, as_of: date | None = None) -> dict:
    picks = _load_core_picks()
    if as_of is None:
        as_of = date.today()

    rows: list[dict] = []
    for p in picks:
        df = load_prices(p["stock_id"])
        perf = _pick_perf(df, p["date"], p["entry"], as_of)
        if perf is None:
            continue
        rows.append({**p, **perf})

    # ---------- 2. 勝率統計(以最新報酬正負;含所有已有 >=1 交易日的選股) ----------
    matured_any = [r for r in rows if r["trading_elapsed"] >= 1 and r["latest_ret"] is not None]
    win = sum(1 for r in matured_any if r["latest_ret"] > 0)
    loss = sum(1 for r in matured_any if r["latest_ret"] < 0)
    flat = len(matured_any) - win - loss
    overall = {
        "total": len(matured_any), "win": win, "loss": loss, "flat": flat,
        "win_rate": round(win / len(matured_any) * 100, 1) if matured_any else None,
    }

    # ---------- 3+4. 各天期:平均收盤報酬 + 平均最高漲幅 + 勝率 ----------
    by_horizon = {}
    for h in HORIZONS:
        rs = [r["rets"][h] for r in rows if r["rets"].get(h) is not None]
        mg = [r["maxgain"][h] for r in rows if r["maxgain"].get(h) is not None]
        if not rs:
            continue
        by_horizon[h] = {
            "n": len(rs),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
            "avg_ret": round(sum(rs) / len(rs) * 100, 2),
            "avg_maxgain": round(sum(mg) / len(mg) * 100, 2) if mg else None,
        }

    # ---------- 1. 逐檔追蹤台帳(仍在追蹤的,依最新報酬排序) ----------
    active = [r for r in rows if r["trading_elapsed"] <= ACTIVE_TRADING_DAYS]
    active.sort(key=lambda r: -(r["latest_ret"] if r["latest_ret"] is not None else -9))
    ledger = [{
        "date": r["date"], "stock_id": r["stock_id"], "name": r["name"],
        "entry": round(r["entry"], 2),
        "latest_close": round(r["latest_close"], 2) if r["latest_close"] is not None else None,
        "days": r["days_elapsed"], "trading_elapsed": r["trading_elapsed"],
        "ret_pct": round(r["latest_ret"] * 100, 2) if r["latest_ret"] is not None else None,
        "peak_price": round(r["peak_price"], 2) if r["peak_price"] is not None else None,
        "peak_gain_pct": round(r["peak_gain"] * 100, 2) if r["peak_gain"] is not None else None,
        "profile": r.get("profile"), "score": r.get("score"),
    } for r in active]

    return {
        "as_of": as_of.isoformat(),
        "overall": overall,
        "by_horizon": by_horizon,
        "ledger": ledger,
        "ledger_total": len(active),
        "ledger_cap": LEDGER_EMAIL_CAP,
        "horizons": HORIZONS,
        "total_tracked": len(rows),
    }


def _print_report(rep: dict) -> None:
    o = rep["overall"]
    print(f"=== 歷史追蹤(as of {rep['as_of']}) 共 {rep['total_tracked']} 檔選股 ===")
    if o["win_rate"] is not None:
        print(f"勝率:{o['win_rate']}%  (總 {o['total']} / 獲利 {o['win']} / 虧損 {o['loss']} / 持平 {o['flat']})")
    print(f"\n{'天期':>5} {'樣本':>5} {'勝率':>6} {'平均報酬':>9} {'平均最高漲幅':>12}")
    names = {1: "隔日", 3: "3日", 5: "5日", 10: "10日", 20: "20日", 30: "30日"}
    for h in rep["horizons"]:
        s = rep["by_horizon"].get(h)
        if not s:
            continue
        mg = f"{s['avg_maxgain']:+.2f}%" if s["avg_maxgain"] is not None else "-"
        print(f"{names[h]:>5} {s['n']:>5} {s['win_rate']:>5.0f}% {s['avg_ret']:>+8.2f}% {mg:>12}")
    print(f"\n台帳(仍追蹤 {rep['ledger_total']} 檔,依報酬排序):")
    print(f"{'選股日':>10} {'代號':>6} {'成本':>8} {'最新':>8} {'天':>3} {'報酬':>8} {'最高漲幅':>9}")
    for r in rep["ledger"][:20]:
        pg = f"{r['peak_gain_pct']:+.1f}%" if r["peak_gain_pct"] is not None else "-"
        rt = f"{r['ret_pct']:+.2f}%" if r["ret_pct"] is not None else "-"
        print(f"{r['date']:>10} {r['stock_id']:>6} {r['entry']:>8} {str(r['latest_close']):>8} {r['days']:>3} {rt:>8} {pg:>9}")


if __name__ == "__main__":
    rep = build_report()
    _print_report(rep)
