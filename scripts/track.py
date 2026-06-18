from __future__ import annotations
import json
import glob
from datetime import date

import pandas as pd

from .config import SIGNALS_DIR
from .storage import load_prices
from .indicators import sma, atr as atr_ind

"""歷史追蹤與績效分析系統。

每天回看過去所有核心選股,用 repo 既有每日收盤/最高/最低價(零 API):
  1. 逐檔追蹤台帳(選股日/成本/最新/天數/報酬率/期間最高漲幅 + 模擬出場結果)
  2. 勝率統計(以最新報酬正負)
  3. 各天期平均報酬(隔日/3/5/10/20/30 日)+ 各天期平均最高漲幅
  4. 出場模擬(R 倍數初始停損 + 2R 第一目標 + 突破後移動停利),算真實已實現勝率
"""

HORIZONS = [1, 3, 5, 10, 20, 30]
ACTIVE_TRADING_DAYS = 30
LEDGER_EMAIL_CAP = 50
EXIT_REASONS = ["止損", "均線停損", "移動停利", "到期"]


def _load_core_picks() -> list[dict]:
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
                "breakout": bool(p.get("breakout")), "pullback_turn": bool(p.get("pullback_turn")),
            })
    seen = set(); uniq = []
    for p in picks:
        key = (p["date"], p["stock_id"])
        if key not in seen:
            seen.add(key); uniq.append(p)
    return uniq


def _pick_perf(df: pd.DataFrame, sig_date: str, entry: float, as_of: date) -> dict | None:
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


def _simulate_exit(df: pd.DataFrame, sig_date: str, entry: float, style: str, cfg: dict) -> dict | None:
    """R 倍數初始停損 + 2R 第一目標(TP1)+ 突破 TP1 後啟動移動停利。

    動能流(5MA)/ 波段流(20MA)依風格分流;初始停損取近 N 根結構低但不深於 -hard_stop;
    移動停利回檔 = clamp(atr_mult × ATR%, min_pct, max_pct);同日雙觸保守假設先觸停損。
    """
    if df is None or df.empty or "close" not in df.columns:
        return None
    pos = df.index.get_indexer([pd.Timestamp(sig_date)])
    if len(pos) == 0 or pos[0] == -1:
        return None
    p0 = int(pos[0]); n = len(df)
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    hard = float(cfg.get("hard_stop", 0.07)); rmult = float(cfg.get("r_multiple", 2.0))
    max_hold = int(cfg.get("max_hold_days", 30))
    sc = cfg.get(style, {}) or {}
    struct_lb = int(sc.get("struct_lookback", 2 if style == "momentum" else 10))
    ma_stop_p = int(sc.get("ma_stop", 5 if style == "momentum" else 20))
    trail_ma_p = int(sc.get("trail_ma", 5 if style == "momentum" else 10))
    tcfg = cfg.get("trail", {}) or {}
    atr_mult = float(tcfg.get("atr_mult", 1.5))
    rmin = float(tcfg.get("min_pct", 0.03)); rmax = float(tcfg.get("max_pct", 0.07))

    ma_stop = sma(close, ma_stop_p)
    ma_trail = sma(close, trail_ma_p)
    atr = atr_ind(high, low, close, 14)

    # 初始停損:近 struct_lb 根結構低,但虧損不超過 hard_stop
    floor = entry * (1 - hard)
    lo0 = low.iloc[max(0, p0 - struct_lb + 1):p0 + 1].min()
    init_stop = floor if (pd.isna(lo0) or lo0 >= entry) else max(float(lo0), floor)
    R = entry - init_stop
    if R <= entry * 0.005:           # 風險過小 → 退用硬停損當風險基準
        init_stop = floor; R = entry - init_stop
    tp1 = entry + rmult * R
    ph = high.iloc[max(0, p0 - 60):p0].max()
    tp1_resistance = bool(pd.notna(ph) and tp1 <= ph <= tp1 * 1.05)

    def res(reason, price, i, status="closed"):
        return {"status": status, "reason": reason, "exit_ret": float(price / entry - 1),
                "exit_price": round(float(price), 2), "hold_days": i,
                "style": style, "init_stop": round(init_stop, 2), "tp1": round(tp1, 2),
                "r_pct": round(R / entry * 100, 2), "tp1_resistance": tp1_resistance}

    trailing = False; swing_high = entry
    for i in range(1, max_hold + 1):
        d = p0 + i
        if d >= n:
            last = n - 1
            price = float(close.iloc[last]) if last > p0 else entry
            return res("持有中", price, max(last - p0, 0), status="open")
        hi = high.iloc[d]; lo = low.iloc[d]; cl = close.iloc[d]
        if not trailing:
            if pd.notna(lo) and lo <= init_stop:                       # 盤中破初始/硬停損
                return res("止損", init_stop, i)
            if pd.notna(cl) and pd.notna(ma_stop.iloc[d]) and cl < ma_stop.iloc[d]:  # 收盤破均線
                return res("均線停損", cl, i)
            if pd.notna(hi) and hi >= tp1:                             # 觸 TP1 → 啟動移動停利
                trailing = True; swing_high = max(entry, float(hi))
                continue
        else:
            atr_pct = float(atr.iloc[d] / cl) if (pd.notna(atr.iloc[d]) and pd.notna(cl) and cl) else rmin
            retr = min(max(atr_mult * atr_pct, rmin), rmax)
            trail_level = swing_high * (1 - retr)                      # 用今天之前的波段高(保守)
            if pd.notna(lo) and lo <= trail_level:
                return res("移動停利", trail_level, i)
            if pd.notna(cl) and pd.notna(ma_trail.iloc[d]) and cl < ma_trail.iloc[d]:
                return res("移動停利", cl, i)
            if pd.notna(hi):
                swing_high = max(swing_high, float(hi))
    d = p0 + max_hold
    if d < n:
        return res("到期", float(close.iloc[d]), max_hold)
    last = n - 1
    price = float(close.iloc[last]) if last > p0 else entry
    return res("持有中", price, max(last - p0, 0), status="open")


def _style_of(pick: dict) -> str:
    return "momentum" if (pick.get("profile") == "動能" or pick.get("breakout")) else "swing"


def build_report(index_close: pd.Series | None = None, as_of: date | None = None,
                 exit_cfg: dict | None = None) -> dict:
    picks = _load_core_picks()
    if as_of is None:
        as_of = date.today()
    exit_cfg = exit_cfg or {}

    rows: list[dict] = []
    for p in picks:
        df = load_prices(p["stock_id"])
        perf = _pick_perf(df, p["date"], p["entry"], as_of)
        if perf is None:
            continue
        sim = _simulate_exit(df, p["date"], p["entry"], _style_of(p), exit_cfg)
        rows.append({**p, **perf, "exit": sim})

    # ---------- 勝率統計(以最新報酬正負) ----------
    matured = [r for r in rows if r["trading_elapsed"] >= 1 and r["latest_ret"] is not None]
    win = sum(1 for r in matured if r["latest_ret"] > 0)
    loss = sum(1 for r in matured if r["latest_ret"] < 0)
    overall = {"total": len(matured), "win": win, "loss": loss, "flat": len(matured) - win - loss,
               "win_rate": round(win / len(matured) * 100, 1) if matured else None}

    # ---------- 各天期平均收盤報酬 + 平均最高漲幅 ----------
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

    # ---------- 出場模擬(R 倍數 + 移動停利)→ 已實現勝率 ----------
    closed = [r["exit"] for r in rows if r.get("exit") and r["exit"]["status"] == "closed"]
    open_n = sum(1 for r in rows if r.get("exit") and r["exit"]["status"] == "open")
    exit_sim = {
        "method": "R 倍數初始停損 + 2R 目標 + 移動停利",
        "hard_stop": round(float(exit_cfg.get("hard_stop", 0.07)) * 100, 1),
        "r_multiple": float(exit_cfg.get("r_multiple", 2.0)),
        "max_hold_days": int(exit_cfg.get("max_hold_days", 30)),
        "closed": len(closed), "open": open_n,
    }
    if closed:
        reasons = {}
        for rn in EXIT_REASONS:
            c = sum(1 for e in closed if e["reason"] == rn)
            if c:
                reasons[rn] = round(c / len(closed) * 100, 1)
        exit_sim.update({
            "win_rate": round(sum(1 for e in closed if e["exit_ret"] > 0) / len(closed) * 100, 1),
            "avg_ret": round(sum(e["exit_ret"] for e in closed) / len(closed) * 100, 2),
            "avg_hold_days": round(sum(e["hold_days"] for e in closed) / len(closed), 1),
            "reasons": reasons,
        })

    # ---------- 逐檔追蹤台帳(仍追蹤的,依最新報酬排序) ----------
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
        "exit_reason": r["exit"]["reason"] if r.get("exit") else None,
        "exit_ret_pct": round(r["exit"]["exit_ret"] * 100, 2) if r.get("exit") else None,
        "hold_days": r["exit"]["hold_days"] if r.get("exit") else None,
        "tp1": r["exit"]["tp1"] if r.get("exit") else None,
        "init_stop": r["exit"]["init_stop"] if r.get("exit") else None,
        "tp1_resistance": r["exit"].get("tp1_resistance") if r.get("exit") else None,
    } for r in active]

    return {
        "as_of": as_of.isoformat(),
        "overall": overall,
        "by_horizon": by_horizon,
        "exit_sim": exit_sim,
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
        print(f"勝率:{o['win_rate']}%  (總 {o['total']} / 獲利 {o['win']} / 虧損 {o['loss']})")
    print(f"\n{'天期':>5} {'樣本':>5} {'勝率':>6} {'平均報酬':>9} {'平均最高漲幅':>12}")
    names = {1: "隔日", 3: "3日", 5: "5日", 10: "10日", 20: "20日", 30: "30日"}
    for h in rep["horizons"]:
        s = rep["by_horizon"].get(h)
        if not s:
            continue
        mg = f"{s['avg_maxgain']:+.2f}%" if s["avg_maxgain"] is not None else "-"
        print(f"{names[h]:>5} {s['n']:>5} {s['win_rate']:>5.0f}% {s['avg_ret']:>+8.2f}% {mg:>12}")
    es = rep.get("exit_sim", {})
    if es.get("closed"):
        rs = " / ".join(f"{k}{v}%" for k, v in es.get("reasons", {}).items())
        print(f"\n出場模擬({es['method']};硬停損-{es['hard_stop']}% / TP1={es['r_multiple']}R / 最多{es['max_hold_days']}日):")
        print(f"  已實現勝率 {es['win_rate']}% · 平均報酬 {es['avg_ret']:+.2f}% · 平均持有 {es['avg_hold_days']} 日"
              f"  ({rs} · 持有中 {es['open']})")
    print(f"\n台帳(仍追蹤 {rep['ledger_total']} 檔,依報酬排序):")
    for r in rep["ledger"][:20]:
        ex = f"{r['exit_reason']} {r['exit_ret_pct']:+.1f}%" if r["exit_reason"] else "-"
        rt = f"{r['ret_pct']:+.2f}%" if r["ret_pct"] is not None else "-"
        print(f"  {r['date']} {r['stock_id']:>5} 成本{r['entry']:>8} 最新{str(r['latest_close']):>8} "
              f"{r['days']:>2}天 報酬{rt:>8} 最高{r['peak_gain_pct']:+.1f}% 出場[{ex}]")


if __name__ == "__main__":
    from .config import load_screeners
    try:
        ecfg = load_screeners().get("exit", {})
    except Exception:
        ecfg = {}
    _print_report(build_report(exit_cfg=ecfg))
