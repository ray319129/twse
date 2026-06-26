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
                "catalyst_signal": p.get("catalyst_signal"),
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


def compute_entry_plan(df: pd.DataFrame, ref_idx: int, ref_price: float, style: str,
                       exit_cfg: dict, max_chase: float = 0.03) -> dict:
    """規劃初始停損 / 風險R / TP1 / 建議進場上限。供盤後給「隔日進場」參考,也供出場模擬共用。
    初始停損 = max(近 N 根結構低, ref×(1-hard));R = ref - 停損;TP1 = ref + r_multiple×R。"""
    hard = float(exit_cfg.get("hard_stop", 0.07)); rmult = float(exit_cfg.get("r_multiple", 2.0))
    sc = exit_cfg.get(style, {}) or {}
    struct_lb = int(sc.get("struct_lookback", 2 if style == "momentum" else 10))
    low = df["low"] if "low" in df.columns else df["close"]
    high = df["high"] if "high" in df.columns else df["close"]
    floor = ref_price * (1 - hard)
    lo0 = low.iloc[max(0, ref_idx - struct_lb + 1):ref_idx + 1].min()
    init_stop = floor if (pd.isna(lo0) or float(lo0) >= ref_price) else max(float(lo0), floor)
    R = ref_price - init_stop
    if R <= ref_price * 0.005:
        init_stop = floor; R = ref_price - init_stop
    tp1 = ref_price + rmult * R
    ph = high.iloc[max(0, ref_idx - 60):ref_idx].max()
    ma5 = float(df["close"].iloc[max(0, ref_idx - 4):ref_idx + 1].mean())
    return {"ref": round(ref_price, 2), "max_entry": round(ref_price * (1 + max_chase), 2),
            "init_stop": round(init_stop, 2), "tp1": round(tp1, 2), "r_abs": R,
            "risk_pct": round(R / ref_price * 100, 2) if ref_price else None, "style": style,
            "tp1_resistance": bool(pd.notna(ph) and tp1 <= ph <= tp1 * 1.05),
            # 隔日開盤分流(A/B/C)的價位線:平盤帶 ±1%、開高 +1.5%、開低 -1%
            "flat_lo": round(ref_price * 0.99, 2), "flat_hi": round(ref_price * 1.01, 2),
            "gap_up_line": round(ref_price * 1.015, 2), "gap_dn_line": round(ref_price * 0.99, 2),
            "trail_ma": int((exit_cfg.get(style, {}) or {}).get("trail_ma", 5 if style == "momentum" else 10))}


def _net_return(entry: float, exit_price: float, cost_cfg: dict | None, hold_days: int | None = None) -> float:
    """扣交易成本後的真實報酬:買賣各收手續費(×折數)+滑價,賣出再收證交稅。
    證交稅預設一般稅率 0.3%;只有「進場與出場同一天」(hold_days==0,理論上才符合當沖定義)
    且 config 設了 tax_rate_daytrade 時,才套用當沖減半稅率 0.15%(2027年底前)——
    本系統設計是隔日開盤才進場,正常出場(hold_days≥1)本來就不是當沖,不能套用減半稅率。
    """
    cost_cfg = cost_cfg or {}
    fee = float(cost_cfg.get("fee_rate", 0.001425)) * float(cost_cfg.get("fee_discount", 1.0))
    slip = float(cost_cfg.get("slippage_pct", 0.0))
    if hold_days == 0 and "tax_rate_daytrade" in cost_cfg:
        tax = float(cost_cfg["tax_rate_daytrade"])
    else:
        tax = float(cost_cfg.get("tax_rate", 0.003))
    buy_cost = entry * (1 + slip) * (1 + fee)
    sell_proceeds = exit_price * (1 - slip) * (1 - fee - tax)
    return sell_proceeds / buy_cost - 1 if buy_cost else 0.0


def compute_position_size(plan: dict | None, account_cfg: dict | None) -> dict | None:
    """依帳戶資金 × 單筆風險% 反推建議張數(1張=1000股)— 把 R 倍數框架用完整。
    research:倉位規模對績效變異的影響遠大於進場訊號本身(Van Tharp),但這系統原本只給價位線、沒給張數。
    預設關閉(account.enabled=false);使用者填自己資金與風險%才會顯示,不填就不出現(不臆測)。
    """
    account_cfg = account_cfg or {}
    if not account_cfg.get("enabled") or not plan or not plan.get("r_abs"):
        return None
    capital = float(account_cfg.get("capital", 0) or 0)
    risk_pct = float(account_cfg.get("risk_pct", 0.01))
    r_abs = float(plan["r_abs"])
    if capital <= 0 or risk_pct <= 0 or r_abs <= 0:
        return None
    risk_budget = capital * risk_pct
    lots = int(risk_budget // (r_abs * 1000))
    ref = float(plan.get("ref") or 0)
    return {
        "capital": round(capital), "risk_pct": round(risk_pct * 100, 2),
        "risk_budget_ntd": round(risk_budget),
        "suggested_lots": lots,
        "position_cost_ntd": round(lots * 1000 * ref) if (lots and ref) else 0,
        "actual_risk_ntd": round(lots * 1000 * r_abs) if lots else 0,
    }


def _simulate_exit(df: pd.DataFrame, sig_date: str, sig_close: float, style: str,
                   cfg: dict, max_chase: float = 0.03, cost_cfg: dict | None = None,
                   catalyst_signal: float | None = None, catalyst_chase_cfg: dict | None = None) -> dict | None:
    """真實出場模擬:以「隔日開盤」進場(貼近實際,不是盤後收盤),並做跳空保護:
      - 隔日開盤較選股收盤高 > max_chase → 跳空棄單(不追高)
        例外:有強催化劑(catalyst_signal ≥ min_catalyst_score)+ 進場當日帶量(量比 ≥ min_vol_ratio)時,
        棄單門檻放寬到 max_chase+extra_chase(`entry.catalyst_chase`,預設關閉)——
        研究顯示「有強催化劑的突破缺口」回補率遠低於無催化劑的缺口,值得放寬而非一律棄單。
      - 隔日開盤已跌破初始停損 → 跳空棄單(開低破停損)
    進場後:未達 TP1 前盤中破初始停損 / 收盤破均線出場;觸 TP1 啟動移動停利。
    exit_ret 已扣交易成本(手續費×2+證交稅+滑價,見 cost_cfg);exit_ret_gross 為扣成本前報酬,供對照。
    """
    if df is None or df.empty or "close" not in df.columns:
        return None
    pos = df.index.get_indexer([pd.Timestamp(sig_date)])
    if len(pos) == 0 or pos[0] == -1:
        return None
    p0 = int(pos[0]); n = len(df)
    e = p0 + 1
    if e >= n:                       # 今天才選,隔日尚未到 → 待進場
        return {"status": "pending", "reason": "待隔日進場", "exit_ret": None,
                "hold_days": 0, "entry_price": None, "gap": None}
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]
    open_ = df["open"] if "open" in df.columns else close
    rmult = float(cfg.get("r_multiple", 2.0))
    max_hold = int(cfg.get("max_hold_days", 30))
    sc = cfg.get(style, {}) or {}
    ma_stop_p = int(sc.get("ma_stop", 5 if style == "momentum" else 20))
    trail_ma_p = int(sc.get("trail_ma", 5 if style == "momentum" else 10))
    tcfg = cfg.get("trail", {}) or {}
    atr_mult = float(tcfg.get("atr_mult", 1.5))
    rmin = float(tcfg.get("min_pct", 0.03)); rmax = float(tcfg.get("max_pct", 0.07))
    ma_stop = sma(close, ma_stop_p); ma_trail = sma(close, trail_ma_p)
    atr = atr_ind(high, low, close, 14)

    oe = open_.iloc[e]
    if pd.isna(oe):
        oe = close.iloc[e] if pd.notna(close.iloc[e]) else sig_close
    entry = float(oe)
    gap = (entry / sig_close - 1) if sig_close else 0.0
    plan = compute_entry_plan(df, p0, entry, style, cfg, max_chase)
    init_stop = plan["init_stop"]; tp1 = plan["tp1"]; R = plan["r_abs"]
    base = {"entry_price": round(entry, 2), "gap": round(gap * 100, 2),
            "init_stop": round(init_stop, 2), "tp1": round(tp1, 2),
            "r_pct": round(R / entry * 100, 2) if entry else None, "style": style}

    if gap > max_chase:              # 跳空開高 → 預設不追(R:R 已破壞)
        chased = False
        cc = catalyst_chase_cfg or {}
        if cc.get("enabled") and catalyst_signal is not None \
                and catalyst_signal >= float(cc.get("min_catalyst_score", 0.5)) \
                and gap <= max_chase + float(cc.get("extra_chase", 0.02)):
            vol = df["volume"] if "volume" in df.columns else None
            if vol is not None:
                vol_ma5 = sma(vol, 5)
                vma = vol_ma5.iloc[e]
                vr = float(vol.iloc[e] / vma) if (pd.notna(vma) and vma) else None
                if vr is not None and vr >= float(cc.get("min_vol_ratio", 1.5)):
                    chased = True
                    base["chased_catalyst"] = True
        if not chased:
            return {**base, "status": "skip", "reason": "跳空開高棄單", "exit_ret": None, "hold_days": 0}
    if gap < -max_chase:             # 跳空開低過大 → 隔夜條件已變,棄單(無論催化劑都別接刀)
        return {**base, "status": "skip", "reason": "跳空開低棄單", "exit_ret": None, "hold_days": 0}
    if entry <= init_stop:           # 開盤已在停損價下 → 棄單
        return {**base, "status": "skip", "reason": "跳空開低破停損", "exit_ret": None, "hold_days": 0}

    def res(reason, price, hold, status="closed"):
        gross = float(price / entry - 1)
        net = _net_return(entry, float(price), cost_cfg, hold_days=hold)
        return {**base, "status": status, "reason": reason,
                "exit_ret": net, "exit_ret_gross": gross,
                "cost_pct": round((gross - net) * 100, 2),
                "exit_price": round(float(price), 2), "hold_days": hold}

    trailing = False; swing = entry
    for i in range(0, max_hold):     # i=0 進場當日(已用開盤進場,當日高低可觸發)
        d = e + i
        if d >= n:
            last = n - 1; lc = close.iloc[last]
            return res("持有中", float(lc) if pd.notna(lc) else entry, max(last - e, 0), status="open")
        hi = high.iloc[d]; lo = low.iloc[d]; cl = close.iloc[d]
        if not trailing:
            if pd.notna(lo) and lo <= init_stop:
                return res("止損", init_stop, i)
            if pd.notna(cl) and pd.notna(ma_stop.iloc[d]) and cl < ma_stop.iloc[d]:
                return res("均線停損", cl, i)
            if pd.notna(hi) and hi >= tp1:
                trailing = True; swing = max(entry, float(hi)); continue
        else:
            atr_pct = float(atr.iloc[d] / cl) if (pd.notna(atr.iloc[d]) and pd.notna(cl) and cl) else rmin
            retr = min(max(atr_mult * atr_pct, rmin), rmax)
            trail_level = swing * (1 - retr)
            if pd.notna(lo) and lo <= trail_level:
                return res("移動停利", trail_level, i)
            if pd.notna(cl) and pd.notna(ma_trail.iloc[d]) and cl < ma_trail.iloc[d]:
                return res("移動停利", cl, i)
            if pd.notna(hi):
                swing = max(swing, float(hi))
    d = e + max_hold - 1
    if d < n and pd.notna(close.iloc[d]):
        return res("到期", float(close.iloc[d]), max_hold - 1)
    last = n - 1; lc = close.iloc[last]
    return res("持有中", float(lc) if pd.notna(lc) else entry, max(last - e, 0), status="open")


def _style_of(pick: dict) -> str:
    return "momentum" if (pick.get("profile") == "動能" or pick.get("breakout")) else "swing"


def _forward_from_entry(df: pd.DataFrame, e: int, entry_price: float) -> dict | None:
    """從『進場日 e(隔日開盤進場)』起算的前向收盤報酬與最高漲幅。
    用於『被跳空開高 skip 的票,若當初照開盤買進』的機會成本分析(純測量,不影響出場模擬)。"""
    if df is None or df.empty or not entry_price or e < 0 or e >= len(df):
        return None
    n = len(df)
    high = df["high"] if "high" in df.columns else df["close"]
    close = df["close"]
    out: dict = {"rets": {}, "maxgain": {}}
    for h in HORIZONS:
        tp = e + h
        if tp < n:
            c = close.iloc[tp]
            out["rets"][h] = float(c / entry_price - 1) if pd.notna(c) else None
            hi = high.iloc[e:tp + 1].max()
            out["maxgain"][h] = float(hi / entry_price - 1) if pd.notna(hi) else None
        else:
            out["rets"][h] = None
            out["maxgain"][h] = None
    last = n - 1
    lc = close.iloc[last]
    out["latest_ret"] = float(lc / entry_price - 1) if pd.notna(lc) else None
    hi_all = high.iloc[e:last + 1].max()
    out["peak_gain"] = float(hi_all / entry_price - 1) if pd.notna(hi_all) else None
    return out


def _avg_pct(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals) * 100, 2) if vals else None


def _win_rate(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None


def build_report(index_close: pd.Series | None = None, as_of: date | None = None,
                 exit_cfg: dict | None = None, entry_cfg: dict | None = None,
                 cost_cfg: dict | None = None) -> dict:
    picks = _load_core_picks()
    if as_of is None:
        as_of = date.today()
    exit_cfg = exit_cfg or {}
    max_chase = float((entry_cfg or {}).get("max_chase", 0.03))
    catalyst_chase_cfg = (entry_cfg or {}).get("catalyst_chase", {})

    rows: list[dict] = []
    for p in picks:
        df = load_prices(p["stock_id"])
        perf = _pick_perf(df, p["date"], p["entry"], as_of)
        if perf is None:
            continue
        sim = _simulate_exit(df, p["date"], p["entry"], _style_of(p), exit_cfg, max_chase, cost_cfg,
                             catalyst_signal=p.get("catalyst_signal"), catalyst_chase_cfg=catalyst_chase_cfg)
        row = {**p, **perf, "exit": sim}
        # 機會成本測量:被「跳空開高棄單」的票,若當初照隔日開盤買進的前向表現(純量化,不改交易規則)
        if sim and sim.get("status") == "skip" and sim.get("gap") is not None and sim["gap"] > 0:
            pos = df.index.get_indexer([pd.Timestamp(p["date"])])
            if len(pos) and pos[0] != -1:
                row["skip_fwd"] = _forward_from_entry(df, int(pos[0]) + 1, sim.get("entry_price"))
                row["skip_gap"] = sim["gap"]   # 百分比
        rows.append(row)

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

    # ---------- 出場模擬(隔日開盤進場 + 跳空保護 + R 倍數 + 移動停利)→ 真實已實現勝率 ----------
    closed = [r["exit"] for r in rows if r.get("exit") and r["exit"]["status"] == "closed"]
    open_n = sum(1 for r in rows if r.get("exit") and r["exit"]["status"] == "open")
    skip_n = sum(1 for r in rows if r.get("exit") and r["exit"]["status"] == "skip")
    pending_n = sum(1 for r in rows if r.get("exit") and r["exit"]["status"] == "pending")
    chased_n = sum(1 for r in rows if r.get("exit") and r["exit"].get("chased_catalyst"))
    cost_cfg = cost_cfg or {}
    exit_sim = {
        "method": "隔日開盤進場 + 跳空保護 + R 倍數 + 移動停利",
        "hard_stop": round(float(exit_cfg.get("hard_stop", 0.07)) * 100, 1),
        "r_multiple": float(exit_cfg.get("r_multiple", 2.0)),
        "max_hold_days": int(exit_cfg.get("max_hold_days", 30)),
        "max_chase": round(max_chase * 100, 1),
        "closed": len(closed), "open": open_n, "skipped_gap": skip_n, "pending": pending_n,
        "chased_catalyst": chased_n,
        "cost_included": bool(cost_cfg),
        "fee_rate": round(float(cost_cfg.get("fee_rate", 0.001425)) * float(cost_cfg.get("fee_discount", 1.0)) * 100, 4),
        "tax_rate": round(float(cost_cfg.get("tax_rate", 0.003)) * 100, 2),
        "tax_rate_daytrade": (round(float(cost_cfg["tax_rate_daytrade"]) * 100, 2)
                               if "tax_rate_daytrade" in cost_cfg else None),
        "slippage_pct": round(float(cost_cfg.get("slippage_pct", 0.0)) * 100, 2),
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
            "avg_ret_gross": round(sum(e["exit_ret_gross"] for e in closed) / len(closed) * 100, 2),
            "avg_cost_pct": round(sum(e["cost_pct"] for e in closed) / len(closed), 2),
            "avg_hold_days": round(sum(e["hold_days"] for e in closed) / len(closed), 1),
            "reasons": reasons,
        })

    # ---------- 逐檔追蹤台帳(仍追蹤的,依最新報酬排序) ----------
    active = [r for r in rows if r["trading_elapsed"] <= ACTIVE_TRADING_DAYS]
    active.sort(key=lambda r: -(r["latest_ret"] if r["latest_ret"] is not None else -9))
    def _led(r):
        ex = r.get("exit") or {}
        ex_ret = ex.get("exit_ret")
        return {
            "date": r["date"], "stock_id": r["stock_id"], "name": r["name"],
            "entry": round(r["entry"], 2),
            "latest_close": round(r["latest_close"], 2) if r["latest_close"] is not None else None,
            "days": r["days_elapsed"], "trading_elapsed": r["trading_elapsed"],
            "ret_pct": round(r["latest_ret"] * 100, 2) if r["latest_ret"] is not None else None,
            "peak_price": round(r["peak_price"], 2) if r["peak_price"] is not None else None,
            "peak_gain_pct": round(r["peak_gain"] * 100, 2) if r["peak_gain"] is not None else None,
            "profile": r.get("profile"), "score": r.get("score"),
            "entry_open": ex.get("entry_price"), "gap_pct": ex.get("gap"),
            "exit_status": ex.get("status"),
            "exit_reason": ex.get("reason"),
            "exit_ret_pct": round(ex_ret * 100, 2) if ex_ret is not None else None,
            "exit_ret_gross_pct": round(ex["exit_ret_gross"] * 100, 2) if ex.get("exit_ret_gross") is not None else None,
            "cost_pct": ex.get("cost_pct"),
            "hold_days": ex.get("hold_days"),
            "tp1": ex.get("tp1"), "init_stop": ex.get("init_stop"),
        }
    ledger = [_led(r) for r in active]

    # ---------- 選股 vs 執行 拆分 + 跳空棄單機會成本(純測量,不影響上面的交易規則) ----------
    # signal_return:選股能力 = 從「選股日收盤」持有到最新(全部已成熟 picks,不含任何執行規則)
    signal = {
        "n": len(matured),
        "win_rate": _win_rate([r["latest_ret"] for r in matured]),
        "avg_ret": _avg_pct([r["latest_ret"] for r in matured]),
        "avg_maxgain": _avg_pct([r["peak_gain"] for r in matured]),
    }
    # execution_return:實際規則(隔日開盤進場 + 跳空保護 + 停損/移動停利),只有 closed 真正實現
    execution = {
        "n_closed": len(closed), "n_skip": skip_n, "n_open": open_n, "n_pending": pending_n,
        "win_rate": exit_sim.get("win_rate"), "avg_ret": exit_sim.get("avg_ret"),
        "avg_hold_days": exit_sim.get("avg_hold_days"),
    }
    # 選股 vs 執行 差距:同一組「已決策」(closed ∪ skip)上比較。
    # signal=照選股收盤持有到最新;exec=照規則(skip 視為未進場、報酬 0)。delta<0 代表執行層在扣分。
    decided = [r for r in rows if r.get("exit") and r["exit"]["status"] in ("closed", "skip")
               and r["latest_ret"] is not None]
    sig_avg = _avg_pct([r["latest_ret"] for r in decided])
    exec_vals = [(r["exit"]["exit_ret"] if r["exit"]["status"] == "closed" else 0.0) for r in decided
                 if not (r["exit"]["status"] == "closed" and r["exit"].get("exit_ret") is None)]
    exec_avg = _avg_pct(exec_vals)
    signal_vs_exec = {
        "n_decided": len(decided),
        "signal_avg_ret": sig_avg, "exec_avg_ret": exec_avg,
        "delta": (round(exec_avg - sig_avg, 2) if (sig_avg is not None and exec_avg is not None) else None),
        "note": "signal=照選股收盤持有到最新;exec=照規則(跳空棄單視為未進場,報酬0)。delta<0 表示執行層在扣分。",
    }
    # 跳空開高棄單的機會成本:這些票若當初照隔日開盤買進,後續表現如何?
    skipped = [r for r in rows if r.get("skip_fwd")]
    skip_cost = {"n": len(skipped)}
    if skipped:
        skip_cost.update({
            "win_rate": _win_rate([r["skip_fwd"]["latest_ret"] for r in skipped]),
            "avg_ret": _avg_pct([r["skip_fwd"]["latest_ret"] for r in skipped]),
            "avg_maxgain": _avg_pct([r["skip_fwd"]["peak_gain"] for r in skipped]),
        })
        buckets = [("3-6%", 3, 6), ("6-8%", 6, 8), (">8%", 8, 1e9)]
        by_gap = {}
        for lab, lo, hi in buckets:
            grp = [r for r in skipped if lo <= r["skip_gap"] < hi]
            if grp:
                by_gap[lab] = {
                    "n": len(grp),
                    "win_rate": _win_rate([r["skip_fwd"]["latest_ret"] for r in grp]),
                    "avg_ret": _avg_pct([r["skip_fwd"]["latest_ret"] for r in grp]),
                    "avg_maxgain": _avg_pct([r["skip_fwd"]["peak_gain"] for r in grp]),
                }
        skip_cost["by_gap"] = by_gap
        by_h = {}
        for h in HORIZONS:
            mg = [r["skip_fwd"]["maxgain"].get(h) for r in skipped if r["skip_fwd"]["maxgain"].get(h) is not None]
            rt = [r["skip_fwd"]["rets"].get(h) for r in skipped if r["skip_fwd"]["rets"].get(h) is not None]
            if rt:
                by_h[h] = {"n": len(rt), "win_rate": _win_rate(rt),
                           "avg_ret": _avg_pct(rt), "avg_maxgain": _avg_pct(mg)}
        skip_cost["by_horizon"] = by_h

    return {
        "as_of": as_of.isoformat(),
        "overall": overall,
        "by_horizon": by_horizon,
        "exit_sim": exit_sim,
        "signal": signal,
        "execution": execution,
        "signal_vs_exec": signal_vs_exec,
        "skip_cost": skip_cost,
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
        print(f"\n出場模擬({es['method']};隔日開盤進場,開高>{es.get('max_chase')}% 棄單,硬停損-{es['hard_stop']}% / TP1={es['r_multiple']}R):")
        print(f"  已實現勝率 {es['win_rate']}% · 平均報酬(已扣成本) {es['avg_ret']:+.2f}%"
              f"(扣前 {es.get('avg_ret_gross', 0):+.2f}%,成本 {es.get('avg_cost_pct', 0):.2f}%) · 平均持有 {es['avg_hold_days']} 日"
              f"  ({rs} · 持有中 {es['open']} · 跳空棄單 {es.get('skipped_gap',0)} · 待進場 {es.get('pending',0)})")
    print(f"\n台帳(仍追蹤 {rep['ledger_total']} 檔,依報酬排序):")
    for r in rep["ledger"][:20]:
        ex = f"{r['exit_reason']} {r['exit_ret_pct']:+.1f}%" if r.get("exit_ret_pct") is not None else (r.get("exit_reason") or "-")
        rt = f"{r['ret_pct']:+.2f}%" if r["ret_pct"] is not None else "-"
        pk = f"{r['peak_gain_pct']:+.1f}%" if r.get("peak_gain_pct") is not None else "-"
        print(f"  {r['date']} {r['stock_id']:>5} 成本{r['entry']:>8} 最新{str(r['latest_close']):>8} "
              f"{r['days']:>2}天 報酬{rt:>8} 最高{pk:>7} 出場[{ex}]")


if __name__ == "__main__":
    from .config import load_screeners
    try:
        cfg = load_screeners()
        ecfg = cfg.get("exit", {}); encfg = cfg.get("entry", {}); ccfg = cfg.get("cost", {})
    except Exception:
        ecfg = {}; encfg = {}; ccfg = {}
    _print_report(build_report(exit_cfg=ecfg, entry_cfg=encfg, cost_cfg=ccfg))
