from __future__ import annotations
import json
import glob
from datetime import date

import pandas as pd

from .config import SIGNALS_DIR, DATA_DIR
from .storage import load_prices
from .utils import log

"""核心精選的前進式績效追蹤(forward test)。

每天回頭看過去每一批「核心精選」,用 repo 已存的每日收盤價,算進場後 N 個交易日的:
  - 實際報酬
  - 同期大盤(^TWII)報酬
  - 超額報酬 alpha = 個股 - 大盤(這才是「有沒有贏 ETF」的關鍵)
並彙總勝率 / 平均報酬 / 平均 alpha / 贏大盤比例,證明這個評分到底有沒有 edge。

純讀 data/signals/*.json + data/prices/*.parquet,不需任何 API。
"""

HORIZONS = [1, 3, 5, 10, 20]   # 交易日


def _load_core_picks() -> list[dict]:
    """從所有歷史 signals JSON 抽出核心精選(只有新版有 'core' 鍵,舊檔自動略過)。"""
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
            sid = p.get("stock_id")
            entry = p.get("close")
            if not sid or entry in (None, 0):
                continue
            picks.append({
                "date": sig_date,
                "stock_id": sid,
                "name": p.get("name", ""),
                "entry": float(entry),
                "score": p.get("score"),
                "profile": p.get("profile"),
                "trigger": "突破" if p.get("breakout") else ("回測" if p.get("pullback_turn") else "觸發"),
            })
    return picks


def _fwd_return(df: pd.DataFrame, sig_date: str, horizon: int) -> float | None:
    """進場日(sig_date 收盤)後 horizon 個交易日的報酬;資料不足回 None。"""
    if df is None or df.empty or "close" not in df.columns:
        return None
    ts = pd.Timestamp(sig_date)
    pos = df.index.get_indexer([ts])
    if len(pos) == 0 or pos[0] == -1:
        return None
    p0 = int(pos[0])
    if p0 + horizon >= len(df):
        return None   # 還沒累積到那麼多天
    c0 = df["close"].iloc[p0]
    cN = df["close"].iloc[p0 + horizon]
    if pd.notna(c0) and pd.notna(cN) and c0 > 0:
        return float(cN / c0 - 1)
    return None


def build_report(index_close: pd.Series | None = None, as_of: date | None = None) -> dict:
    """彙總核心精選的前進式績效。index_close = ^TWII 收盤(算 alpha);None 則只看絕對報酬。"""
    picks = _load_core_picks()
    idx = None
    if index_close is not None and len(index_close) > 0:
        idx = pd.DataFrame({"close": pd.to_numeric(index_close, errors="coerce")})

    rows: list[dict] = []
    for p in picks:
        df = load_prices(p["stock_id"])
        rec = dict(p)
        rec["rets"] = {}
        rec["alpha"] = {}
        any_ret = False
        for h in HORIZONS:
            r = _fwd_return(df, p["date"], h)
            rec["rets"][h] = r
            if r is not None:
                any_ret = True
                if idx is not None:
                    b = _fwd_return(idx, p["date"], h)
                    rec["alpha"][h] = (r - b) if b is not None else None
                else:
                    rec["alpha"][h] = None
        if any_ret:
            rows.append(rec)

    # 各時間窗彙總
    summary = {}
    for h in HORIZONS:
        rs = [r["rets"][h] for r in rows if r["rets"].get(h) is not None]
        al = [r["alpha"][h] for r in rows if r["alpha"].get(h) is not None]
        if not rs:
            continue
        summary[h] = {
            "n": len(rs),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
            "avg_ret": round(sum(rs) / len(rs) * 100, 2),
            "avg_alpha": round(sum(al) / len(al) * 100, 2) if al else None,
            "beat_rate": round(sum(1 for x in al if x > 0) / len(al) * 100, 1) if al else None,
        }

    # 依型態(動能/品質/均衡)拆 5 日表現
    by_profile = {}
    for prof in ("動能", "品質", "均衡"):
        rs = [r["rets"][5] for r in rows if r.get("profile") == prof and r["rets"].get(5) is not None]
        if rs:
            by_profile[prof] = {
                "n": len(rs),
                "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
                "avg_ret": round(sum(rs) / len(rs) * 100, 2),
            }

    # 最近一批已有 5 日結果的核心(讓使用者看到具體個股的實際走勢)
    dated = sorted({r["date"] for r in rows if r["rets"].get(5) is not None})
    recent = []
    recent_date = None
    if dated:
        recent_date = dated[-1]
        for r in rows:
            if r["date"] == recent_date and r["rets"].get(5) is not None:
                recent.append({
                    "stock_id": r["stock_id"], "name": r["name"],
                    "score": r["score"], "profile": r["profile"], "trigger": r["trigger"],
                    "ret5": round(r["rets"][5] * 100, 2),
                    "alpha5": round(r["alpha"][5] * 100, 2) if r["alpha"].get(5) is not None else None,
                })
        recent.sort(key=lambda x: -(x["ret5"] if x["ret5"] is not None else -999))

    return {
        "summary": summary,
        "by_profile": by_profile,
        "recent": recent,
        "recent_date": recent_date,
        "total_picks_tracked": len(rows),
        "horizons": HORIZONS,
    }


def _print_report(rep: dict) -> None:
    print(f"追蹤核心精選共 {rep['total_picks_tracked']} 檔(已有前進資料者)")
    print(f"{'天期':>4} {'樣本':>5} {'勝率':>7} {'均報酬':>8} {'均alpha':>8} {'贏大盤':>7}")
    for h in rep["horizons"]:
        s = rep["summary"].get(h)
        if not s:
            continue
        a = f"{s['avg_alpha']:+.2f}%" if s["avg_alpha"] is not None else "  -  "
        b = f"{s['beat_rate']:.0f}%" if s["beat_rate"] is not None else "  -  "
        print(f"{h:>3}日 {s['n']:>5} {s['win_rate']:>6.0f}% {s['avg_ret']:>+7.2f}% {a:>8} {b:>7}")
    if rep["by_profile"]:
        print("\n依型態(5 日):")
        for prof, s in rep["by_profile"].items():
            print(f"  {prof}: n={s['n']} 勝率{s['win_rate']:.0f}% 均報酬{s['avg_ret']:+.2f}%")
    if rep["recent"]:
        print(f"\n最近一批({rep['recent_date']})核心 5 日實際走勢:")
        for r in rep["recent"]:
            a = f" (贏大盤{r['alpha5']:+.2f}%)" if r["alpha5"] is not None else ""
            print(f"  {r['stock_id']} {r['name'][:8]} 分{r['score']} {r['profile']} {r['trigger']}: {r['ret5']:+.2f}%{a}")


if __name__ == "__main__":
    from .fetchers import fetch_index_history
    idx = fetch_index_history(days=400)
    rep = build_report(idx["close"] if not idx.empty else None)
    _print_report(rep)
