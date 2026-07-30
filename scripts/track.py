from __future__ import annotations
import json
import glob
from datetime import date

import pandas as pd

from .config import SIGNALS_DIR
from .storage import load_prices, load_index_cache
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


def _pick_perf(df: pd.DataFrame, sig_date: str, entry: float, as_of: date,
               index_close: pd.Series | None = None) -> dict | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    pos = df.index.get_indexer([pd.Timestamp(sig_date)])
    if len(pos) == 0 or pos[0] == -1:
        return None
    p0 = int(pos[0]); n = len(df)
    high = df["high"] if "high" in df.columns else df["close"]
    # bench/excess:同一段期間的大盤報酬與超額。用個股自己的日期切點對齊,
    # 每檔各自從選股日起算(事件時間),沒有共同終點偏誤。
    out = {"rets": {}, "maxgain": {}, "bench": {}, "excess": {}}
    for h in HORIZONS:
        tp = p0 + h
        if tp < n:
            c = df["close"].iloc[tp]
            out["rets"][h] = float(c / entry - 1) if pd.notna(c) and entry else None
            hi = high.iloc[p0 + 1:tp + 1].max()
            out["maxgain"][h] = float(hi / entry - 1) if pd.notna(hi) and entry else None
            b = _bench_between(index_close, df.index[p0], df.index[tp])
            out["bench"][h] = b
            out["excess"][h] = (out["rets"][h] - b) if (b is not None and out["rets"][h] is not None) else None
        else:
            out["rets"][h] = None; out["maxgain"][h] = None
            out["bench"][h] = None; out["excess"][h] = None
    last = n - 1
    if last > p0:
        lc = df["close"].iloc[last]
        out["latest_ret"] = float(lc / entry - 1) if pd.notna(lc) and entry else None
        out["latest_close"] = float(lc) if pd.notna(lc) else None
        hi_all = high.iloc[p0 + 1:last + 1].max()
        out["peak_price"] = float(hi_all) if pd.notna(hi_all) else None
        out["peak_gain"] = float(hi_all / entry - 1) if pd.notna(hi_all) and entry else None
        out["trading_elapsed"] = last - p0
        out["latest_bench"] = _bench_between(index_close, df.index[p0], df.index[last])
    else:
        out["latest_ret"] = 0.0; out["latest_close"] = entry
        out["peak_price"] = entry; out["peak_gain"] = 0.0; out["trading_elapsed"] = 0
        out["latest_bench"] = 0.0 if index_close is not None else None
    out["latest_excess"] = (out["latest_ret"] - out["latest_bench"]
                            if (out["latest_bench"] is not None and out["latest_ret"] is not None) else None)
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
    # 均線停損寬限:進場後前 N 個交易日只用初始停損(結構低),第 N+1 日起才啟用均線停損。
    # 動能突破股觸發日收盤常在 5MA 上方 5~10%,隔日進場後正常回測一天就跌破 5MA → 被單日洗盤掃出,
    # 完全沒吃到後續噴出(實測:均線停損佔出場 53.7%、平均持有 1.2 天)。給前幾天洗盤空間。
    ma_grace = int(sc.get("ma_stop_grace_days", 0))
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
            # 前 ma_grace 個交易日(i < ma_grace)略過均線停損,只靠初始停損 → 給洗盤空間
            if i >= ma_grace and pd.notna(cl) and pd.notna(ma_stop.iloc[d]) and cl < ma_stop.iloc[d]:
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


def _exit_stats(exits: list[dict]) -> dict | None:
    """對一組 closed 出場結果算勝率/平均淨報酬/平均持有天數/出場原因分佈。
    供 exit_sim 的 by_trigger(breakout vs pullback_turn)與 by_style(動能 vs 波段)拆分共用 ——
    看得出「哪種進場型態配哪種出場規則真有 edge」,是調參的依據。"""
    exits = [e for e in exits if e and e.get("status") == "closed" and e.get("exit_ret") is not None]
    if not exits:
        return None
    n = len(exits)
    reasons = {}
    for rn in EXIT_REASONS:
        c = sum(1 for e in exits if e["reason"] == rn)
        if c:
            reasons[rn] = round(c / n * 100, 1)
    return {
        "n": n,
        "win_rate": round(sum(1 for e in exits if e["exit_ret"] > 0) / n * 100, 1),
        "avg_ret": round(sum(e["exit_ret"] for e in exits) / n * 100, 2),
        "avg_hold_days": round(sum(e["hold_days"] for e in exits) / n, 1),
        "reasons": reasons,
    }


def _trigger_of(r: dict) -> str:
    """進場型態分類(擇一):突破 > 回測轉強 > 其他。供 by_trigger 拆分。"""
    if r.get("breakout"):
        return "breakout"
    if r.get("pullback_turn"):
        return "pullback_turn"
    return "other"


def _avg_pct(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals) * 100, 2) if vals else None


def _win_rate(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None


def _bench_between(index_close: pd.Series | None, d0, d1) -> float | None:
    """大盤在兩個日期之間的報酬。用 `asof`(取「該日或之前最近一筆」)對齊 ——
    個股與指數的交易日序列可能有洞(停牌/資料缺),用位置差會飄掉,用日期才對得準。
    任一端缺值或起點為 0 回 None(缺基準時寧可不顯示,別給假超額)。

    ⚠️ **必須擋區間外**:`asof` 對「晚於序列最後一筆」的日期會回傳最後一筆值,
    等於把大盤當成從此不再變動 → 基準恆 0 → 超額 = 原始報酬,而且看起來完全正常。
    大盤快取只要落後一天,所有新選股都會拿到假超額。故超出兩端一律回 None。"""
    if index_close is None or d0 is None or d1 is None or index_close.empty:
        return None
    t0, t1 = pd.Timestamp(d0), pd.Timestamp(d1)
    lo, hi = index_close.index[0], index_close.index[-1]
    if t0 < lo or t1 > hi:
        return None
    try:
        a = index_close.asof(t0)
        b = index_close.asof(t1)
    except Exception:
        return None
    if pd.isna(a) or pd.isna(b) or not a:
        return None
    return float(b / a - 1)


def _beat_rate(excess: list[float]) -> float | None:
    """勝過大盤的比例(超額 > 0)。與 _win_rate 分開命名,避免「絕對正報酬」被誤讀成「贏大盤」。"""
    vals = [v for v in excess if v is not None]
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

    if index_close is not None and not index_close.empty:
        index_close = index_close.dropna().sort_index()   # asof 要求已排序
        if index_close.empty:
            index_close = None

    rows: list[dict] = []
    for p in picks:
        df = load_prices(p["stock_id"])
        perf = _pick_perf(df, p["date"], p["entry"], as_of, index_close)
        if perf is None:
            continue
        sim = _simulate_exit(df, p["date"], p["entry"], _style_of(p), exit_cfg, max_chase, cost_cfg,
                             catalyst_signal=p.get("catalyst_signal"), catalyst_chase_cfg=catalyst_chase_cfg)
        row = {**p, **perf, "exit": sim}
        # 已出場的單:算「實際持有期間」(隔日開盤進場 → 出場當日)的大盤報酬,
        # 才能問「這筆是靠選股贏,還是整個大盤都在漲」。
        if sim and sim.get("status") == "closed" and sim.get("hold_days") is not None:
            _pos = df.index.get_indexer([pd.Timestamp(p["date"])])
            if len(_pos) and _pos[0] != -1:
                e = int(_pos[0]) + 1
                xp = e + int(sim["hold_days"])
                if e < len(df) and xp < len(df):
                    eb = _bench_between(index_close, df.index[e], df.index[xp])
                    if eb is not None:
                        sim["bench_ret"] = eb
                        if sim.get("exit_ret") is not None:
                            sim["excess"] = sim["exit_ret"] - eb
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
    # ★ 超額才是選股能力的尺:絕對報酬會把大盤漲跌算到自己頭上。
    _ex_all = [r.get("latest_excess") for r in matured]
    overall.update({
        "avg_ret": _avg_pct([r["latest_ret"] for r in matured]),
        "avg_bench": _avg_pct([r.get("latest_bench") for r in matured]),
        "avg_excess": _avg_pct(_ex_all),
        "beat_rate": _beat_rate(_ex_all),
        "n_excess": sum(1 for v in _ex_all if v is not None),
    })

    # ---------- 各天期平均收盤報酬 + 平均最高漲幅 ----------
    by_horizon = {}
    for h in HORIZONS:
        rs = [r["rets"][h] for r in rows if r["rets"].get(h) is not None]
        mg = [r["maxgain"][h] for r in rows if r["maxgain"].get(h) is not None]
        ex = [r["excess"][h] for r in rows if r.get("excess", {}).get(h) is not None]
        bn = [r["bench"][h] for r in rows if r.get("bench", {}).get(h) is not None]
        if not rs:
            continue
        by_horizon[h] = {
            "n": len(rs),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
            "avg_ret": round(sum(rs) / len(rs) * 100, 2),
            "avg_maxgain": round(sum(mg) / len(mg) * 100, 2) if mg else None,
            "n_excess": len(ex),
            "avg_bench": round(sum(bn) / len(bn) * 100, 2) if bn else None,
            "avg_excess": round(sum(ex) / len(ex) * 100, 2) if ex else None,
            "beat_rate": _beat_rate(ex),
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
        _cex = [e.get("excess") for e in closed if e.get("excess") is not None]
        _cbn = [e.get("bench_ret") for e in closed if e.get("bench_ret") is not None]
        exit_sim.update({
            "win_rate": round(sum(1 for e in closed if e["exit_ret"] > 0) / len(closed) * 100, 1),
            "avg_ret": round(sum(e["exit_ret"] for e in closed) / len(closed) * 100, 2),
            "avg_ret_gross": round(sum(e["exit_ret_gross"] for e in closed) / len(closed) * 100, 2),
            "avg_cost_pct": round(sum(e["cost_pct"] for e in closed) / len(closed), 2),
            "avg_hold_days": round(sum(e["hold_days"] for e in closed) / len(closed), 1),
            "reasons": reasons,
            # 已實現超額:扣完成本的淨報酬 vs 同一段持有期間的大盤
            "n_excess": len(_cex),
            "avg_bench": round(sum(_cbn) / len(_cbn) * 100, 2) if _cbn else None,
            "avg_excess": round(sum(_cex) / len(_cex) * 100, 2) if _cex else None,
            "beat_rate": _beat_rate(_cex),
        })
        # by_trigger / by_style 拆分:看「哪種進場型態配哪種出場規則」有沒有 edge(調參依據)。
        closed_rows = [r for r in rows if r.get("exit") and r["exit"].get("status") == "closed"
                       and r["exit"].get("exit_ret") is not None]
        by_trigger = {}
        for key in ("breakout", "pullback_turn", "other"):
            st = _exit_stats([r["exit"] for r in closed_rows if _trigger_of(r) == key])
            if st:
                by_trigger[key] = st
        by_style = {}
        for key in ("momentum", "swing"):
            st = _exit_stats([r["exit"] for r in closed_rows if _style_of(r) == key])
            if st:
                by_style[key] = st
        if by_trigger:
            exit_sim["by_trigger"] = by_trigger
        if by_style:
            exit_sim["by_style"] = by_style

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
            "bench_ret_pct": round(r["latest_bench"] * 100, 2) if r.get("latest_bench") is not None else None,
            "excess_pct": round(r["latest_excess"] * 100, 2) if r.get("latest_excess") is not None else None,
            "exit_excess_pct": round(ex["excess"] * 100, 2) if ex.get("excess") is not None else None,
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
        "avg_bench": _avg_pct([r.get("latest_bench") for r in matured]),
        "avg_excess": _avg_pct([r.get("latest_excess") for r in matured]),
        "beat_rate": _beat_rate([r.get("latest_excess") for r in matured]),
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
        "benchmark": {
            "available": index_close is not None,
            "name": "加權指數 (^TWII)",
            "note": ("超額 = 個股報酬 − 同期大盤報酬,每檔各自從自己的選股日起算(事件時間,無共同終點偏誤)。"
                     "絕對報酬會把大盤漲跌算到選股頭上,超額才是選股能力的尺。"
                     if index_close is not None else "本次無大盤資料,超額欄位從缺(不以 0 充數)。"),
        },
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
    bm = rep.get("benchmark", {})
    print(f"=== 歷史追蹤(as of {rep['as_of']}) 共 {rep['total_tracked']} 檔選股 ===")
    if o["win_rate"] is not None:
        print(f"勝率:{o['win_rate']}%  (總 {o['total']} / 獲利 {o['win']} / 虧損 {o['loss']})")
    if bm.get("available") and o.get("avg_excess") is not None:
        print(f"★ 超額 vs {bm.get('name')}:平均 {o['avg_excess']:+.2f}pp"
              f"(選股 {o.get('avg_ret'):+.2f}% vs 大盤 {o.get('avg_bench'):+.2f}%)"
              f" · 勝過大盤 {o.get('beat_rate')}%  [n={o.get('n_excess')}]")
    elif not bm.get("available"):
        print("⚠ 無大盤基準,只能看絕對報酬 —— 大盤自己的漲跌會被算到選股頭上,別據此下結論。")
    print(f"\n{'天期':>5} {'樣本':>5} {'勝率':>6} {'平均報酬':>9} {'大盤同期':>9} {'超額':>9} {'勝過大盤':>8} {'平均最高漲幅':>12}")
    names = {1: "隔日", 3: "3日", 5: "5日", 10: "10日", 20: "20日", 30: "30日"}
    for h in rep["horizons"]:
        s = rep["by_horizon"].get(h)
        if not s:
            continue
        mg = f"{s['avg_maxgain']:+.2f}%" if s["avg_maxgain"] is not None else "-"
        bn = f"{s['avg_bench']:+.2f}%" if s.get("avg_bench") is not None else "-"
        ex = f"{s['avg_excess']:+.2f}pp" if s.get("avg_excess") is not None else "-"
        br = f"{s['beat_rate']:.0f}%" if s.get("beat_rate") is not None else "-"
        print(f"{names[h]:>5} {s['n']:>5} {s['win_rate']:>5.0f}% {s['avg_ret']:>+8.2f}% {bn:>9} {ex:>9} {br:>8} {mg:>12}")
    es = rep.get("exit_sim", {})
    if es.get("closed"):
        rs = " / ".join(f"{k}{v}%" for k, v in es.get("reasons", {}).items())
        print(f"\n出場模擬({es['method']};隔日開盤進場,開高>{es.get('max_chase')}% 棄單,硬停損-{es['hard_stop']}% / TP1={es['r_multiple']}R):")
        print(f"  已實現勝率 {es['win_rate']}% · 平均報酬(已扣成本) {es['avg_ret']:+.2f}%"
              f"(扣前 {es.get('avg_ret_gross', 0):+.2f}%,成本 {es.get('avg_cost_pct', 0):.2f}%) · 平均持有 {es['avg_hold_days']} 日"
              f"  ({rs} · 持有中 {es['open']} · 跳空棄單 {es.get('skipped_gap',0)} · 待進場 {es.get('pending',0)})")
        if es.get("avg_excess") is not None:
            print(f"  ★ 已實現超額 {es['avg_excess']:+.2f}pp(同期大盤 {es.get('avg_bench'):+.2f}%)"
                  f" · 勝過大盤 {es.get('beat_rate')}%  [n={es.get('n_excess')}]")
        _TRIG_LABEL = {"breakout": "突破", "pullback_turn": "回測轉強", "other": "其他"}
        _STYLE_LABEL = {"momentum": "動能", "swing": "波段"}
        for title, grp, labels in (("依進場型態", es.get("by_trigger", {}), _TRIG_LABEL),
                                   ("依風格", es.get("by_style", {}), _STYLE_LABEL)):
            if grp:
                print(f"  {title}:")
                for key, s in grp.items():
                    rs2 = " / ".join(f"{k}{v}%" for k, v in s.get("reasons", {}).items())
                    print(f"    {labels.get(key, key):>5}  n={s['n']:>3} 勝率{s['win_rate']:>5.1f}%"
                          f" 平均{s['avg_ret']:>+7.2f}% 持有{s['avg_hold_days']:>4.1f}日  ({rs2})")
    print(f"\n台帳(仍追蹤 {rep['ledger_total']} 檔,依報酬排序):")
    for r in rep["ledger"][:20]:
        ex = f"{r['exit_reason']} {r['exit_ret_pct']:+.1f}%" if r.get("exit_ret_pct") is not None else (r.get("exit_reason") or "-")
        rt = f"{r['ret_pct']:+.2f}%" if r["ret_pct"] is not None else "-"
        pk = f"{r['peak_gain_pct']:+.1f}%" if r.get("peak_gain_pct") is not None else "-"
        xs = f"{r['excess_pct']:+.2f}pp" if r.get("excess_pct") is not None else "-"
        print(f"  {r['date']} {r['stock_id']:>5} 成本{r['entry']:>8} 最新{str(r['latest_close']):>8} "
              f"{r['days']:>2}天 報酬{rt:>8} 超額{xs:>9} 最高{pk:>7} 出場[{ex}]")


if __name__ == "__main__":
    from .config import load_screeners
    try:
        cfg = load_screeners()
        ecfg = cfg.get("exit", {}); encfg = cfg.get("entry", {}); ccfg = cfg.get("cost", {})
    except Exception:
        ecfg = {}; encfg = {}; ccfg = {}
    # 單獨跑時用落地的大盤快取(每日流程會更新它);缺檔就退化成無基準模式並明講。
    _idx = load_index_cache()
    _ic = _idx["close"] if (_idx is not None and not _idx.empty and "close" in _idx.columns) else None
    _print_report(build_report(index_close=_ic, exit_cfg=ecfg, entry_cfg=encfg, cost_cfg=ccfg))
