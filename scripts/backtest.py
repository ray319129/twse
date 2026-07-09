from __future__ import annotations
import argparse
import json
import glob
import math
import sys
import time
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

from .config import load_screeners, PRICES_DIR, META_DIR, DATA_DIR
from .storage import load_prices
from .indicators import compute_all, compute_relative_strength
from .scoring import compute_conviction
from .market import compute_market_regime
from .track import _simulate_exit, _style_of, _net_return, HORIZONS

"""純技術回測(第一版)— 誠實回答「純技術選股訊號有沒有 edge vs 大盤」。

架構(見 memory/twse-backtest-plan.md):
  A 撮合引擎 = 直接複用 track._simulate_exit(隔日開盤進場 / 跳空棄單 / R 倍數 / 移動停利 / 扣交易成本)
  B 訊號重放 = 本檔核心工作:對每個歷史交易日 d,只用「當日可知」資訊重算指標→評分→選股

關鍵正確性保證(為何可以「每檔只算一次指標」而非每天重算):
  compute_all / compute_relative_strength 內所有指標(sma/ema/kd/macd/rsi/atr/bbands/rs_line/rs_ratio)
  皆為『因果』——只用 <= 當日的資料(rolling/ewm+min_periods/shift,無置中、無未來洩漏)。
  故某日 d 的指標值,不論算在「完整序列」或「截到 d 的序列」上都相同。因此先對完整歷史算一次指標,
  再用 df.loc[:d] 切片餵 compute_conviction,結果與「每天重切重算」逐位元一致,但快 ~400 倍。
  (若日後在 indicators 加入任何非因果轉換,這個假設就失效,回測必須改回逐日重算。)

第一版刻意的邊界(不做 = 誠實,不是偷懶):
  1. 純技術面:不含 stage-2 的籌碼/基本面/新聞/產業/combo 加成 —— 那些是前視或需 API 的資料,
     無 point-in-time 歷史快照。故本回測衡量的是「技術選股層」,不是線上完整 core(技術層 + stage-2 重排)。
  2. valuation=None:估值快照(PE/殖利率/PB)只有「今天」的值,無歷史時點對齊 → 品質面給中性 0.5(對全檔一致,
     不影響相對排序太多)。線上 quality 權重僅 0.05,影響小。
  3. 倖存者偏誤:universe = 今天還在的 1976 檔 parquet,歷史下市/暫停交易的股已消失(坑#2,無法補,標記)。
  4. 除權息跳空污染:parquet adj_close 覆蓋率僅 ~3% → compute_all 的還原價分支不啟動,全程用原始價;
     _simulate_exit 本就吃原始價(與線上一致)。跨除息日的報酬會被自然下跳污染(坑#3,現逢除權息旺季尤甚,標記)。
  5. 時間段偏誤:資料僅 ~22 個月單一多頭段;「回測賺」可能只是 beta。故一切以「超額報酬 vs TWII」與
     「弱盤(指數跌破月線)分組」為主軸,絕不看絕對報酬(坑#4)。
"""

WARMUP_BARS = 60          # compute_conviction 的 gate(= min_history_new);同時當暖身:前 60 根不重放
LEDGER_SAMPLE_CAP = 300   # 寫入 JSON 的逐筆樣本上限(控檔案大小;統計數字用全部交易)


# ---------------------------------------------------------------------------
# 資料準備
# ---------------------------------------------------------------------------

def _load_index_close() -> tuple[pd.Series, str]:
    """大盤基準:優先用快取的 TWII(data/meta/twii.parquet);缺則回 (None, 'none')。
    TWII 同時供 (a) compute_relative_strength 的相對強度(與線上一致)與 (b) 超額報酬 benchmark。"""
    p = META_DIR / "twii.parquet"
    if p.exists():
        df = pd.read_parquet(p)
        if "close" in df.columns and not df.empty:
            s = pd.to_numeric(df["close"], errors="coerce").dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s.sort_index(), "TWII(^TWII 加權指數)"
    return None, "none"


def _prepare(universe_limit: int | None, index_close: pd.Series):
    """對每檔載入原始價(供撮合/報酬)+ 算一次指標(供評分)。回傳:
    raws[sid], inds[sid], 以及 master 交易日(所有股票日期的聯集,已排序)。"""
    files = sorted(glob.glob(str(PRICES_DIR / "*.parquet")))
    if universe_limit:
        files = files[:universe_limit]
    raws: dict[str, pd.DataFrame] = {}
    inds: dict[str, pd.DataFrame] = {}
    all_dates: set = set()
    t0 = time.time()
    for i, f in enumerate(files):
        sid = f.replace("\\", "/").split("/")[-1].replace(".parquet", "")
        raw = load_prices(sid)
        if raw.empty or len(raw) < WARMUP_BARS or "close" not in raw.columns:
            continue
        ind = compute_all(raw)
        if index_close is not None:
            ind = compute_relative_strength(ind, index_close, n=60)
        raws[sid] = raw
        inds[sid] = ind
        all_dates.update(raw.index)
        if (i + 1) % 400 == 0:
            print(f"  precompute {i+1}/{len(files)} … ({time.time()-t0:.0f}s)")
    master = pd.DatetimeIndex(sorted(all_dates))
    print(f"  precompute done: {len(inds)} 檔可用, {len(master)} 交易日, {time.time()-t0:.0f}s")
    return raws, inds, master


# ---------------------------------------------------------------------------
# 訊號重放
# ---------------------------------------------------------------------------

def _signal_returns(raw: pd.DataFrame, pos_d: int, sig_close: float) -> dict:
    """選股層報酬:買在選股日『收盤』,持有到各 horizon 的收盤報酬(不含任何進出場規則)。
    與 track._pick_perf 同義,衡量『純選股能力』。用原始價(與線上一致)。"""
    n = len(raw); close = raw["close"]
    out: dict[int, float | None] = {}
    for h in HORIZONS:
        tp = pos_d + h
        if tp < n and sig_close:
            c = close.iloc[tp]
            out[h] = float(c / sig_close - 1) if pd.notna(c) else None
        else:
            out[h] = None
    return out


import copy


def _replay(cfg: dict, universe_limit, start, end, use_regime: bool):
    """訊號重放(慢,~7 分鐘)—— 只做選股,不含出場。回傳 (selections, shared)。
    出場參數不影響選股 → 重放一次即可對多組出場參數做敏感度掃描(見 sweep_exits)。
    selections 每筆含:選股層報酬 sig_rets / benchmark bench_rets / style / _pos_master,皆與出場無關。"""
    score_cfg = dict(cfg.get("scoring", {}) or {})
    rank_cfg = cfg.get("ranking", {}) or {}
    score_cfg["min_dollar_volume"] = float(rank_cfg.get("min_dollar_volume", 30_000_000))
    market_cfg = cfg.get("market", {}) or {}
    fixed_core = int(rank_cfg.get("core_count", 10))
    fixed_min_score = float(rank_cfg.get("min_score", 45))
    lu_thr = float(market_cfg.get("limit_up_pct", 0.095)) * 100
    bo_pen = float(market_cfg.get("breakout_penalty_weak", 8.0))

    index_close, bench_name = _load_index_close()
    print(f"Benchmark: {bench_name}")
    raws, inds, master = _prepare(universe_limit, index_close)
    if not inds:
        raise RuntimeError("無可用股票資料")

    master_vals = master.values
    master_pos = {ts: i for i, ts in enumerate(master)}
    if index_close is not None:
        twii_m = index_close.reindex(master).ffill()
        twii_ma20 = twii_m.rolling(20).mean()
    else:
        twii_m = pd.Series(index=master, dtype=float)
        twii_ma20 = twii_m
    ind_dates = {sid: ind.index.values for sid, ind in inds.items()}

    lo = WARMUP_BARS
    hi = len(master) - 1
    if start:
        lo = max(lo, int(np.searchsorted(master_vals, np.datetime64(start), side="left")))
    if end:
        hi = min(hi, int(np.searchsorted(master_vals, np.datetime64(end), side="right")))
    replay_days = master[lo:hi]
    print(f"重放交易日:{len(replay_days)} 天 ({replay_days[0].date()} -> {replay_days[-1].date()})")

    selections: list[dict] = []
    day_min_score: dict = {}
    t0 = time.time()
    for di, d in enumerate(replay_days):
        d64 = np.datetime64(d)
        pos_master = master_pos[d]
        index_below_ma20 = False
        if index_close is not None and pd.notna(twii_ma20.iloc[pos_master]):
            index_below_ma20 = bool(twii_m.iloc[pos_master] < twii_ma20.iloc[pos_master])

        day_scored: list[dict] = []
        b_n = b_above = b_adv = b_dec = b_lu = b_ld = 0
        for sid, ind in inds.items():
            cut = int(np.searchsorted(ind_dates[sid], d64, side="right"))
            if cut < WARMUP_BARS:
                continue
            sl = ind.iloc[:cut]
            last = sl.iloc[-1]
            close_v = last.get("close"); ma20_v = last.get("ma20")
            if pd.notna(close_v):
                b_n += 1
                if pd.notna(ma20_v) and close_v > ma20_v:
                    b_above += 1
                if cut >= 2:
                    pc = sl["close"].iloc[-2]
                    if pd.notna(pc) and pc > 0:
                        chg = (close_v / pc - 1) * 100
                        if chg > 0:
                            b_adv += 1
                        elif chg < 0:
                            b_dec += 1
                        if chg >= lu_thr:
                            b_lu += 1
                        elif chg <= -lu_thr:
                            b_ld += 1
            conv = compute_conviction(sl, None, cfg=score_cfg)
            if conv and conv.get("trigger"):
                sig_close = float(close_v) if pd.notna(close_v) else None
                if sig_close:
                    conv["stock_id"] = sid; conv["sig_close"] = sig_close; conv["_pos_d"] = cut - 1
                    day_scored.append(conv)

        core_count, min_score, prefer_pb = fixed_core, fixed_min_score, False
        if use_regime:
            breadth = {"n": b_n, "above_ma20": b_above, "adv": b_adv, "dec": b_dec,
                       "limit_up": b_lu, "limit_down": b_ld}
            regime = compute_market_regime(index_close.loc[:d] if index_close is not None else None,
                                           breadth, market_cfg)
            if regime:
                if regime.get("core_count") is not None:
                    core_count = regime["core_count"]
                if regime.get("min_score") is not None:
                    min_score = regime["min_score"]
                prefer_pb = bool(regime.get("prefer_pullback"))

        day_min_score[d.isoformat()] = min_score

        def _key(s):
            base = float(s["score"])
            if prefer_pb and s.get("breakout") and not s.get("pullback_turn"):
                base -= bo_pen
            return -base
        selected = sorted([s for s in day_scored if s["score"] >= min_score], key=_key)[:core_count]

        for s in selected:
            sid = s["stock_id"]; raw = raws[sid]; pos_d = s["_pos_d"]; sig_close = s["sig_close"]
            sig_rets = _signal_returns(raw, pos_d, sig_close)
            bench_rets: dict = {}
            for h in HORIZONS:
                tp = pos_master + h
                if index_close is not None and tp < len(master) \
                        and pd.notna(twii_m.iloc[pos_master]) and pd.notna(twii_m.iloc[tp]) and twii_m.iloc[pos_master]:
                    bench_rets[h] = float(twii_m.iloc[tp] / twii_m.iloc[pos_master] - 1)
                else:
                    bench_rets[h] = None
            selections.append({
                "date": d.isoformat(), "stock_id": sid, "score": s["score"], "profile": s.get("profile"),
                "breakout": bool(s.get("breakout")), "pullback_turn": bool(s.get("pullback_turn")),
                "new_stock": bool(s.get("new_stock")), "index_below_ma20": index_below_ma20,
                "sig_close": round(sig_close, 2), "sig_rets": sig_rets, "bench_rets": bench_rets,
                "style": _style_of(s), "_pos_master": pos_master, "_pos_d": pos_d,
            })

        if (di + 1) % 20 == 0:
            print(f"  replay {di+1}/{len(replay_days)}  {d.date()}  累計選股 {len(selections)} 筆  ({time.time()-t0:.0f}s)")

    print(f"重放完成:{len(selections)} 筆選股, {time.time()-t0:.0f}s")
    shared = {"raws": raws, "master": master,
              "twii_m": twii_m if index_close is not None else None,
              "bench_name": bench_name, "replay_days": replay_days,
              # 供投資組合模擬器逐日重評持股 / 判斷失去訊號用:
              "inds": inds, "ind_dates": ind_dates, "score_cfg": score_cfg,
              "master_pos": master_pos, "day_min_score": day_min_score}
    return selections, shared


def _simulate(selections: list[dict], shared: dict, exit_cfg: dict,
              entry_cfg: dict, cost_cfg: dict) -> list[dict]:
    """對已重放好的 selections 套一組出場參數(快,幾秒)。回傳 picks(含 exit + bench_exec)。"""
    raws = shared["raws"]; master = shared["master"]; twii_m = shared["twii_m"]
    max_chase = float((entry_cfg or {}).get("max_chase", 0.03))
    picks: list[dict] = []
    for sel in selections:
        raw = raws[sel["stock_id"]]
        sim = _simulate_exit(raw, sel["date"], sel["sig_close"], sel["style"],
                             exit_cfg, max_chase, cost_cfg)
        bench_exec = None
        if sim and sim.get("status") == "closed" and sim.get("hold_days") is not None and twii_m is not None:
            ep = sel["_pos_master"] + 1
            xp = ep + int(sim["hold_days"])
            if 0 <= ep < len(master) and 0 <= xp < len(master) \
                    and pd.notna(twii_m.iloc[ep]) and pd.notna(twii_m.iloc[xp]) and twii_m.iloc[ep]:
                bench_exec = float(twii_m.iloc[xp] / twii_m.iloc[ep] - 1)
        pick = {k: v for k, v in sel.items() if not k.startswith("_") and k != "style"}
        pick["exit"] = sim; pick["bench_exec"] = bench_exec
        picks.append(pick)
    return picks


def run_backtest(universe_limit=None, start=None, end=None, use_regime: bool = True) -> dict:
    cfg = load_screeners()
    selections, shared = _replay(cfg, universe_limit, start, end, use_regime)
    picks = _simulate(selections, shared, cfg.get("exit", {}) or {},
                      cfg.get("entry", {}) or {}, cfg.get("cost", {}) or {})
    return _aggregate(picks, shared["bench_name"], shared["replay_days"])


def _exit_variants(base_exit: dict) -> list:
    """出場參數敏感度網格。主軸:出場太早(均線停損佔多數、平均持有 3 天)
    -> 給洗盤空間(拉長均線停損寬限)、或 TP1 前完全不用均線停損、或放寬移動停利。"""
    out = []
    def mk(name, **mods):
        e = copy.deepcopy(base_exit)
        for pathk, val in mods.items():
            cur = e; parts = pathk.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = val
        out.append((name, e))
    mh = int(base_exit.get("max_hold_days", 30))
    mk("baseline(現行)")
    mk("grace 動能3/波段2", **{"momentum.ma_stop_grace_days": 3, "swing.ma_stop_grace_days": 2})
    mk("grace 動能5/波段3", **{"momentum.ma_stop_grace_days": 5, "swing.ma_stop_grace_days": 3})
    mk("grace 動能8/波段5", **{"momentum.ma_stop_grace_days": 8, "swing.ma_stop_grace_days": 5})
    mk("TP1前關均線停損", **{"momentum.ma_stop_grace_days": mh, "swing.ma_stop_grace_days": mh})
    mk("移動停利放宽(ATRx2.5)", **{"trail.atr_mult": 2.5, "trail.min_pct": 0.04, "trail.max_pct": 0.10})
    mk("grace5/3+移動放宽", **{"momentum.ma_stop_grace_days": 5, "swing.ma_stop_grace_days": 3,
                                        "trail.atr_mult": 2.5, "trail.min_pct": 0.04, "trail.max_pct": 0.10})
    mk("grace5/3+max_hold45", **{"momentum.ma_stop_grace_days": 5, "swing.ma_stop_grace_days": 3,
                                 "max_hold_days": 45})
    mk("TP1前關均線+移動放宽", **{"momentum.ma_stop_grace_days": mh, "swing.ma_stop_grace_days": mh,
                                            "trail.atr_mult": 2.5, "trail.min_pct": 0.04, "trail.max_pct": 0.10})
    return out


def sweep_exits(universe_limit=None, start=None, end=None, use_regime: bool = True) -> dict:
    """重放一次 -> 對同一批選股套多組出場參數,比執行層統計。"""
    cfg = load_screeners()
    selections, shared = _replay(cfg, universe_limit, start, end, use_regime)
    entry_cfg = cfg.get("entry", {}) or {}; cost_cfg = cfg.get("cost", {}) or {}
    rows = []
    print("\n掃描出場參數中…")
    for name, exit_cfg in _exit_variants(cfg.get("exit", {}) or {}):
        picks = _simulate(selections, shared, exit_cfg, entry_cfg, cost_cfg)
        st = _exec_stats(picks) or {}
        rows.append({"variant": name, **st})
        print(f"  done {name}")
    return {"benchmark": shared["bench_name"], "n_selections": len(selections),
            "replay_from": shared["replay_days"][0].date().isoformat(),
            "replay_to": shared["replay_days"][-1].date().isoformat(),
            "variants": rows}


# ---------------------------------------------------------------------------
# 彙總與拆分(一切以超額報酬 vs 大盤為主軸,絕不看絕對報酬)
# ---------------------------------------------------------------------------

def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _pct(xs: list, pred) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(1 for x in xs if pred(x)) / len(xs) * 100, 1) if xs else None


def _exec_stats(rows: list[dict]) -> dict | None:
    """對一組 picks 的『執行層(closed 交易)』算勝率/淨報酬/超額/持有天數。"""
    closed = [r for r in rows if r.get("exit") and r["exit"].get("status") == "closed"
              and r["exit"].get("exit_ret") is not None]
    if not closed:
        return None
    nets = [r["exit"]["exit_ret"] for r in closed]
    excess = [r["exit"]["exit_ret"] - r["bench_exec"] for r in closed if r.get("bench_exec") is not None]
    reasons: dict[str, float] = {}
    for rn in ("止損", "均線停損", "移動停利", "到期"):
        c = sum(1 for r in closed if r["exit"].get("reason") == rn)
        if c:
            reasons[rn] = round(c / len(closed) * 100, 1)
    return {
        "n": len(closed),
        "win_rate": round(_pct(nets, lambda x: x > 0), 1) if nets else None,
        "avg_net_ret_pct": round((_mean(nets) or 0) * 100, 2),
        "avg_gross_ret_pct": round((_mean([r["exit"].get("exit_ret_gross") for r in closed]) or 0) * 100, 2),
        "avg_excess_vs_bench_pct": round(_mean(excess) * 100, 2) if excess else None,
        "pct_beat_bench": _pct(excess, lambda x: x > 0),
        "avg_hold_days": _mean([r["exit"].get("hold_days") for r in closed]),
        "exit_reasons": reasons,
    }


def _signal_by_horizon(rows: list[dict]) -> dict:
    """選股層:各 horizon 的平均報酬 / 勝率 / 平均超額 vs TWII / 贏大盤比率。"""
    out = {}
    for h in HORIZONS:
        rs = [r["sig_rets"].get(h) for r in rows if r["sig_rets"].get(h) is not None]
        ex = [r["sig_rets"][h] - r["bench_rets"][h] for r in rows
              if r["sig_rets"].get(h) is not None and r["bench_rets"].get(h) is not None]
        if not rs:
            continue
        out[h] = {
            "n": len(rs),
            "avg_ret_pct": round(_mean(rs) * 100, 2),
            "win_rate": _pct(rs, lambda x: x > 0),
            "avg_excess_pct": round(_mean(ex) * 100, 2) if ex else None,
            "pct_beat_bench": _pct(ex, lambda x: x > 0),
        }
    return out


def _aggregate(picks: list[dict], bench_name: str, replay_days) -> dict:
    n_total = len(picks)
    statuses = defaultdict(int)
    for r in picks:
        st = (r.get("exit") or {}).get("status", "none")
        statuses[st] += 1

    report: dict = {
        "meta": {
            "generated": date.today().isoformat(),
            "benchmark": bench_name,
            "n_signals": n_total,
            "replay_from": replay_days[0].date().isoformat() if len(replay_days) else None,
            "replay_to": replay_days[-1].date().isoformat() if len(replay_days) else None,
            "status_counts": dict(statuses),
            "horizons": HORIZONS,
        },
        # 選股層(全部 picks,不含進出場規則):各天期報酬 + 超額 vs 大盤
        "signal_by_horizon": _signal_by_horizon(picks),
        # 執行層(_simulate_exit closed 交易):真實已實現勝率/淨報酬/超額
        "execution_overall": _exec_stats(picks),
    }

    # --- 拆分 1:依進場型態 ---
    def _trig(r):
        return "breakout" if r["breakout"] else ("pullback_turn" if r["pullback_turn"] else "other")
    report["execution_by_trigger"] = {
        k: st for k in ("breakout", "pullback_turn", "other")
        if (st := _exec_stats([r for r in picks if _trig(r) == k]))
    }
    report["signal_by_trigger_h5"] = {
        k: _signal_by_horizon([r for r in picks if _trig(r) == k]).get(5)
        for k in ("breakout", "pullback_turn", "other")
        if any(_trig(r) == k for r in picks)
    }

    # --- 拆分 2:依大盤狀態(選股當日指數站上/跌破月線)= 弱盤存活測試(近似空頭)---
    report["execution_by_market"] = {
        ("index_above_ma20" if not weak else "index_below_ma20"): st
        for weak in (False, True)
        if (st := _exec_stats([r for r in picks if r["index_below_ma20"] == weak]))
    }
    report["signal_by_market_h5"] = {
        ("index_above_ma20" if not weak else "index_below_ma20"):
            _signal_by_horizon([r for r in picks if r["index_below_ma20"] == weak]).get(5)
        for weak in (False, True)
        if any(r["index_below_ma20"] == weak for r in picks)
    }

    # --- 拆分 3:依信心分四分位(edge 是否隨分數遞增?)---
    scores = sorted(r["score"] for r in picks)
    if len(scores) >= 8:
        qs = [scores[int(len(scores) * q)] for q in (0.25, 0.5, 0.75)]
        def _bucket(sc):
            if sc < qs[0]: return "Q1_low"
            if sc < qs[1]: return "Q2"
            if sc < qs[2]: return "Q3"
            return "Q4_high"
        report["execution_by_score_quartile"] = {
            b: st for b in ("Q1_low", "Q2", "Q3", "Q4_high")
            if (st := _exec_stats([r for r in picks if _bucket(r["score"]) == b]))
        }
        report["_score_quartile_thresholds"] = {"q25": qs[0], "q50": qs[1], "q75": qs[2]}

    # --- 拆分 4:依選股月份(檢查是否只有某幾個月在賺 = 時間段偏誤)---
    by_month: dict[str, dict] = {}
    for m in sorted({r["date"][:7] for r in picks}):
        st = _exec_stats([r for r in picks if r["date"][:7] == m])
        if st:
            by_month[m] = st
    report["execution_by_month"] = by_month

    # --- 逐筆樣本(控大小)---
    report["sample_trades"] = [{
        "date": r["date"], "stock_id": r["stock_id"], "score": r["score"],
        "trigger": _trig(r), "index_below_ma20": r["index_below_ma20"],
        "exit_status": (r.get("exit") or {}).get("status"),
        "exit_reason": (r.get("exit") or {}).get("reason"),
        "net_ret_pct": round((r["exit"]["exit_ret"]) * 100, 2)
            if (r.get("exit") and r["exit"].get("exit_ret") is not None) else None,
        "bench_exec_pct": round(r["bench_exec"] * 100, 2) if r.get("bench_exec") is not None else None,
        "hold_days": (r.get("exit") or {}).get("hold_days"),
    } for r in picks[:LEDGER_SAMPLE_CAP]]

    return report


# ---------------------------------------------------------------------------
# 報表輸出
# ---------------------------------------------------------------------------

def _fmt(v, suffix="%", nd=2):
    return f"{v:+.{nd}f}{suffix}" if v is not None else "  n/a"


def print_report(rep: dict) -> None:
    m = rep["meta"]
    print("\n" + "=" * 78)
    print(f"純技術回測  |  benchmark = {m['benchmark']}")
    print(f"重放 {m['replay_from']} → {m['replay_to']}  |  訊號 {m['n_signals']} 筆  |  "
          f"撮合狀態 {m['status_counts']}")
    print("=" * 78)

    print("\n【選股層】買在選股日收盤,持有到各天期(不含進出場規則);超額 = 個股 - TWII 同期")
    print(f"  {'天期':>4} {'樣本':>5} {'平均報酬':>9} {'勝率':>6} {'平均超額':>9} {'贏大盤':>6}")
    names = {1: "隔日", 3: "3日", 5: "5日", 10: "10日", 20: "20日", 30: "30日"}
    for h in m["horizons"]:
        s = rep["signal_by_horizon"].get(h)
        if not s:
            continue
        print(f"  {names[h]:>4} {s['n']:>5} {_fmt(s['avg_ret_pct']):>9} "
              f"{s['win_rate'] if s['win_rate'] is not None else 'n/a':>5}% "
              f"{_fmt(s['avg_excess_pct']):>9} {str(s['pct_beat_bench'])+'%':>6}")

    eo = rep.get("execution_overall")
    if eo:
        print("\n【執行層】隔日開盤進場 + 跳空棄單 + R倍數/移動停利 + 扣交易成本(真實已實現)")
        print(f"  已實現 {eo['n']} 筆  勝率 {eo['win_rate']}%  平均淨報酬 {_fmt(eo['avg_net_ret_pct'])}"
              f"(扣前 {_fmt(eo['avg_gross_ret_pct'])})  平均持有 {eo['avg_hold_days']} 日")
        print(f"  超額 vs 大盤 {_fmt(eo['avg_excess_vs_bench_pct'])}  贏大盤 {eo['pct_beat_bench']}%"
              f"  出場原因 {eo['exit_reasons']}")

    def _dump_exec(title, grp, label_map=None):
        if not grp:
            return
        print(f"\n【{title}】")
        print(f"  {'分組':>16} {'n':>4} {'勝率':>6} {'淨報酬':>8} {'超額':>8} {'贏大盤':>6} {'持有':>5}")
        for k, s in grp.items():
            if not s:
                continue
            lab = (label_map or {}).get(k, k)
            print(f"  {lab:>16} {s['n']:>4} {str(s['win_rate'])+'%':>6} {_fmt(s['avg_net_ret_pct']):>8} "
                  f"{_fmt(s['avg_excess_vs_bench_pct']):>8} {str(s['pct_beat_bench'])+'%':>6} "
                  f"{s['avg_hold_days']:>5}")

    _dump_exec("依進場型態", rep.get("execution_by_trigger"),
               {"breakout": "突破", "pullback_turn": "回測轉強", "other": "其他"})
    _dump_exec("依大盤狀態(弱盤存活測試)", rep.get("execution_by_market"),
               {"index_above_ma20": "指數站上月線", "index_below_ma20": "指數跌破月線"})
    _dump_exec("依信心分四分位", rep.get("execution_by_score_quartile"),
               {"Q1_low": "Q1最低", "Q2": "Q2", "Q3": "Q3", "Q4_high": "Q4最高"})
    _dump_exec("依選股月份", rep.get("execution_by_month"))

    print("\n" + "-" * 78)
    print("誠實邊界:①倖存者偏誤(下市股已消失)②除權息跳空污染(adj 覆蓋率<3%,全程原始價)")
    print("        ③僅 ~22 個月單一多頭段,證明不了空頭 ④純技術層,不含線上 stage-2 籌碼/基本面/新聞加成")
    print("        ⑤估值快照無歷史時點對齊,品質面一律中性。→ 看『超額』與『弱盤分組』,別信絕對報酬。")
    print("-" * 78)


def print_sweep(rep: dict) -> None:
    """出場參數敏感度掃描報表:同一批選股,各出場參數的執行層勝率/淨報酬/超額。"""
    print("\n" + "=" * 92)
    print(f"出場參數敏感度掃描  |  benchmark = {rep['benchmark']}  |  同一批 {rep['n_selections']} 筆選股  "
          f"|  {rep.get('replay_from')} → {rep.get('replay_to')}")
    print("=" * 92)
    print(f"  {'出場參數':<24} {'n':>5} {'勝率':>6} {'淨報酬':>8} {'扣前':>8} {'超額':>8} {'贏大盤':>6} "
          f"{'持有':>5} {'均線停損%':>8}")
    print("  " + "-" * 88)
    base_net = None
    for r in rep["variants"]:
        if not r.get("n"):
            continue
        if base_net is None:
            base_net = r["avg_net_ret_pct"]
        ma = r.get("exit_reasons", {}).get("均線停損", 0)
        print(f"  {r['variant']:<24} {r['n']:>5} {str(r['win_rate'])+'%':>6} "
              f"{_fmt(r['avg_net_ret_pct']):>8} {_fmt(r['avg_gross_ret_pct']):>8} "
              f"{_fmt(r['avg_excess_vs_bench_pct']):>8} {str(r['pct_beat_bench'])+'%':>6} "
              f"{r['avg_hold_days']:>5} {str(ma)+'%':>8}")
    print("  " + "-" * 88)
    print("  淨報酬/超額為執行層(隔日開盤進場+扣成本);均線停損% = 該出場參數下由均線停損出場的比例。")
    print("  誠實邊界同單次回測(倖存者/除權息/單一多頭段/純技術層);此掃描僅比『相對高下』,別當絕對保證。")


# ---------------------------------------------------------------------------
# 投資組合回測(資金有限 → 最多同時 N 檔 + 滿倉換股規則)
# ---------------------------------------------------------------------------
# 使用者情境:現金有限,不可能每天每檔都買。規則(2026-07-10 與使用者逐項確認):
#   - 同時最多持有 N 檔(等權分成 N 本帳);湊不滿就擺現金(不硬湊爛票,現金報酬 0)。
#   - 每天:①手上部位照常跑停損/移動停利出場(空出名額)②當日觸發選股照分數排序
#           ③先填空名額 ④滿倉才考慮換股。
#   - 換股:只有「新訊號明顯強過最弱持股」才一賣一買。
#       最弱持股 = 三因子等權綜合排名(今天重評分數↓ / 帳面損益↓ / 持有天數↑)。
#       明顯強過 = 最弱持股今天『失去訊號』(重評 None/過熱/分數<當日門檻) 或 分差≥M,任一即換。
#       最短持有閘:進場未滿 min_hold 個交易日的部位不可被換掉(防當沖式來回燒手續費)。
#   - 汰出/新進都走隔日開盤成交,換股吃一賣一買兩趟成本。
# 撮合:每檔部位的「自然出場」直接複用 _simulate_exit(與逐筆回測一致);換股只是提早平倉的疊加層。


def _nat_exit(sel: dict, shared: dict, exit_cfg: dict, max_chase: float, cost_cfg: dict):
    """用 _simulate_exit 算某選股的自然出場(隔日開盤進場)。跳空棄單/待進場/資料不足 → None(不佔名額)。"""
    raw = shared["raws"][sel["stock_id"]]
    sim = _simulate_exit(raw, sel["date"], sel["sig_close"], sel["style"], exit_cfg, max_chase, cost_cfg)
    if not sim or sim.get("status") in ("skip", "pending") or sim.get("entry_price") is None:
        return None
    e = sel["_pos_d"] + 1                       # 進場 bar(隔日開盤)在該股索引的位置
    if e >= len(raw):
        return None
    hold = int(sim.get("hold_days") or 0)
    xbar = min(e + hold, len(raw) - 1)
    return {
        "sid": sel["stock_id"], "style": sel["style"], "entry_score": float(sel["score"]),
        "entry_bar": e, "entry_date": raw.index[e], "entry_price": float(sim["entry_price"]),
        "nat_exit_date": raw.index[xbar], "nat_exit_ret": sim.get("exit_ret"),
        "nat_reason": sim.get("reason"), "nat_status": sim.get("status"),
    }


def _rescore(shared: dict, sid: str, d64) -> dict | None:
    ind = shared["inds"].get(sid)
    if ind is None:
        return None
    cut = int(np.searchsorted(shared["ind_dates"][sid], d64, side="right"))
    if cut < WARMUP_BARS:
        return None
    return compute_conviction(ind.iloc[:cut], None, cfg=shared["score_cfg"])


def _close_at(raw, d64):
    dates = raw.index.values
    pos = int(np.searchsorted(dates, d64, side="right")) - 1
    if pos < 0:
        return None
    v = raw["close"].iloc[pos]
    return float(v) if pd.notna(v) else None


def _next_open_after(raw, d64):
    dates = raw.index.values
    pos = int(np.searchsorted(dates, d64, side="right"))
    if pos >= len(raw):
        return None
    o = raw["open"].iloc[pos] if "open" in raw.columns else raw["close"].iloc[pos]
    if pd.isna(o):
        o = raw["close"].iloc[pos]
    return float(o) if pd.notna(o) else None


LOST_SIGNAL_FLOOR = 30.0   # 「失去訊號」= 重評 None / 過熱 / 分數 < 此(真弱,非只是掉到入榜門檻 45 以下)


def _pick_weakest(occ: list, books: list, d64, shared: dict, min_hold: int):
    """在佔用中的帳裡,依三因子等權綜合排名選最弱、且已過最短持有閘者。回傳 (best_i, weak_info) 或 (None, None)。
    weak_info 帶 entry_score(進場當時分數,供公平比較,避免『新訊號因今天剛觸發而虛高』的換股偏誤)。"""
    master_pos = shared["master_pos"]
    d_pos = int(np.searchsorted(shared["master"].values, d64, side="right")) - 1
    infos = []
    for i in occ:
        p = books[i]["pos"]
        held = d_pos - master_pos.get(p["entry_date"], d_pos)
        if held < min_hold:
            continue
        conv = _rescore(shared, p["sid"], d64)
        score = float(conv["score"]) if conv else -1e9
        lost = (conv is None) or bool(conv.get("exhausted")) or (float(conv["score"]) < LOST_SIGNAL_FLOOR)
        cur = _close_at(shared["raws"][p["sid"]], d64)
        pnl = (cur / p["entry_price"] - 1) if (cur and p["entry_price"]) else 0.0
        infos.append({"i": i, "score": score, "lost": lost, "pnl": pnl, "held": held, "rk": 0,
                      "entry_score": float(p.get("entry_score", score))})
    if not infos:
        return None, None
    for key, reverse in (("score", False), ("pnl", False), ("held", True)):
        for rank, info in enumerate(sorted(infos, key=lambda x: x[key], reverse=reverse)):
            info["rk"] += rank          # 每維最弱者 rank 0;總和越小越弱
    best = min(infos, key=lambda x: (x["rk"], x["score"]))
    return best["i"], best


def simulate_portfolio(selections, shared, naturals, cost_cfg, N, M, min_hold):
    """單組 (N, M, min_hold) 的投資組合逐日模擬。回傳權益曲線 + 指標 + 交易明細。"""
    master = shared["master"]; master_vals = master.values
    picks_by_day = defaultdict(list)
    for si, sel in enumerate(selections):
        if naturals[si] is not None:
            picks_by_day[sel["date"]].append((sel, naturals[si]))
    for dd in picks_by_day:
        picks_by_day[dd].sort(key=lambda t: -t[0]["score"])   # 每天內分數高→低

    books = [{"val": 1.0 / N, "pos": None} for _ in range(N)]   # N 本等權帳,合計 1.0
    trades = []; n_rot = 0; equity_curve = []
    start_pos = int(np.searchsorted(master_vals, np.datetime64(shared["replay_days"][0]), side="left"))
    for dpos in range(start_pos, len(master)):     # 走到最後,讓尾端部位跑到自然出場
        d = master[dpos]; d64 = np.datetime64(d); d_iso = d.isoformat()

        # 1. 自然出場(出場日 <= d 的佔用帳結算)
        for bk in books:
            p = bk["pos"]
            if p is not None and p["nat_exit_date"] <= d:
                ret = p["nat_exit_ret"] if p["nat_exit_ret"] is not None else 0.0
                bk["val"] *= (1 + ret)
                trades.append({"sid": p["sid"], "entry": p["entry_date"].isoformat(),
                               "exit": p["nat_exit_date"].isoformat(), "ret": ret,
                               "reason": p["nat_reason"], "rotated": False})
                bk["pos"] = None

        cands = list(picks_by_day.get(d_iso, []))
        held_ids = {bk["pos"]["sid"] for bk in books if bk["pos"]}
        cands = [(s, nat) for (s, nat) in cands if s["stock_id"] not in held_ids]

        # 3. 填空名額
        ci = 0
        for bk in books:
            if bk["pos"] is None and ci < len(cands):
                sel, nat = cands[ci]; ci += 1
                bk["pos"] = {**nat, "basis": bk["val"]}
        cands = cands[ci:]

        # 4. 滿倉換股(對剩餘候選分數高→低,換掉最弱持股;明顯強過才換)
        while cands and all(bk["pos"] is not None for bk in books):
            sel, nat = cands[0]
            occ = [i for i in range(N) if books[i]["pos"] is not None]
            wi, winfo = _pick_weakest(occ, books, d64, shared, min_hold)
            if wi is None:
                break
            # 明顯強過:最弱持股『失去訊號』(真弱) 或 新訊號分數 高過該持股『進場當時分數』≥ M
            #   (比進場分數而非今天重評分數 → 公平,不會因新訊號今天剛觸發虛高而每天亂換)
            if not (winfo["lost"] or (float(sel["score"]) - winfo["entry_score"] >= M)):
                break                    # 最強候選都打不過最弱持股 → 停(候選已排序)
            w = books[wi]["pos"]
            wopen = _next_open_after(shared["raws"][w["sid"]], d64)
            if wopen is None:
                break
            rot_ret = _net_return(w["entry_price"], wopen, cost_cfg, hold_days=max(1, winfo["held"]))
            books[wi]["val"] *= (1 + rot_ret)
            trades.append({"sid": w["sid"], "entry": w["entry_date"].isoformat(), "exit": d_iso,
                           "ret": rot_ret, "reason": "換股汰出", "rotated": True})
            n_rot += 1
            books[wi]["pos"] = {**nat, "basis": books[wi]["val"]}   # 換入(承接該帳現值)
            cands = cands[1:]

        # 5. 每日 mark-to-market 權益(尚未進場的部位仍以現金計)
        eq = 0.0
        for bk in books:
            p = bk["pos"]
            if p is None or d < p["entry_date"]:
                eq += bk["val"]
            else:
                cur = _close_at(shared["raws"][p["sid"]], d64)
                eq += p["basis"] * (cur / p["entry_price"]) if (cur and p["entry_price"]) else bk["val"]
        equity_curve.append((d, eq))

    return _portfolio_metrics(equity_curve, trades, n_rot, shared, N, M, min_hold)


def _max_drawdown(vals: list) -> float:
    peak = -1e18; mdd = 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def _portfolio_metrics(equity_curve, trades, n_rot, shared, N, M, min_hold) -> dict:
    dates = [d for d, _ in equity_curve]; eq = [v for _, v in equity_curve]
    twii = shared["twii_m"]
    d0, d1 = dates[0], dates[-1]
    years = max((d1 - d0).days / 365.25, 1e-9)
    total = eq[-1] - 1
    cagr = eq[-1] ** (1 / years) - 1 if eq[-1] > 0 else -1
    mdd = _max_drawdown(eq)
    b_tot = b_cagr = b_mdd = None
    if twii is not None:
        tw = twii.reindex(pd.DatetimeIndex(dates)).ffill()
        if pd.notna(tw.iloc[0]) and tw.iloc[0] and pd.notna(tw.iloc[-1]):
            b_tot = float(tw.iloc[-1] / tw.iloc[0] - 1)
            b_cagr = (1 + b_tot) ** (1 / years) - 1
            b_mdd = _max_drawdown(list(tw.values))
    closed = [t for t in trades if t["ret"] is not None]
    wins = sum(1 for t in closed if t["ret"] > 0)
    nat = [t for t in closed if not t["rotated"]]
    rot = [t for t in closed if t["rotated"]]
    return {
        "N": N, "M": M, "min_hold": min_hold,
        "avg_natural_ret_pct": round(sum(t["ret"] for t in nat) / len(nat) * 100, 2) if nat else None,
        "avg_rotated_ret_pct": round(sum(t["ret"] for t in rot) / len(rot) * 100, 2) if rot else None,
        "n_natural": len(nat), "n_rotated_out": len(rot),
        "from": d0.date().isoformat(), "to": d1.date().isoformat(), "years": round(years, 2),
        "total_return_pct": round(total * 100, 1), "cagr_pct": round(cagr * 100, 1),
        "max_drawdown_pct": round(mdd * 100, 1),
        "bench_total_pct": round(b_tot * 100, 1) if b_tot is not None else None,
        "bench_cagr_pct": round(b_cagr * 100, 1) if b_cagr is not None else None,
        "bench_mdd_pct": round(b_mdd * 100, 1) if b_mdd is not None else None,
        "excess_cagr_pct": round((cagr - b_cagr) * 100, 1) if b_cagr is not None else None,
        "n_trades": len(closed), "n_rotations": n_rot, "rotations_per_year": round(n_rot / years, 1),
        "trade_win_rate": round(wins / len(closed) * 100, 1) if closed else None,
        "avg_trade_ret_pct": round(sum(t["ret"] for t in closed) / len(closed) * 100, 2) if closed else None,
        "equity_curve": [(d.date().isoformat(), round(v, 4)) for d, v in equity_curve[::5]],
    }


def sweep_portfolio(universe_limit=None, start=None, end=None, use_regime: bool = True,
                    n_grid=(3, 4, 5), m_grid=(10, 15, 20), hold_grid=(0, 2, 3)) -> dict:
    """組合回測掃描:重放一次 → 對 (N, M, min_hold) 網格各跑一次逐日組合模擬。"""
    cfg = load_screeners()
    selections, shared = _replay(cfg, universe_limit, start, end, use_regime)
    exit_cfg = cfg.get("exit", {}) or {}; cost_cfg = cfg.get("cost", {}) or {}
    max_chase = float((cfg.get("entry", {}) or {}).get("max_chase", 0.03))
    print("預算各選股自然出場(複用 _simulate_exit)…")
    naturals = [_nat_exit(sel, shared, exit_cfg, max_chase, cost_cfg) for sel in selections]
    n_enter = sum(1 for x in naturals if x is not None)
    print(f"  可進場選股 {n_enter}/{len(selections)}(其餘跳空棄單/待進場)")
    rows = []
    print("組合模擬掃描中…")
    for N in n_grid:
        for M in m_grid:
            for mh in hold_grid:
                r = simulate_portfolio(selections, shared, naturals, cost_cfg, N, M, mh)
                rows.append(r)
                print(f"  N={N} M={M} 最短持有={mh} → CAGR {r['cagr_pct']}% "
                      f"(大盤 {r['bench_cagr_pct']}%, 超額 {r['excess_cagr_pct']}%) "
                      f"MDD {r['max_drawdown_pct']}% 換股/年 {r['rotations_per_year']}")
    return {"benchmark": shared["bench_name"], "n_selections": len(selections),
            "n_enterable": n_enter, "results": rows}


def print_portfolio(rep: dict) -> None:
    print("\n" + "=" * 104)
    print(f"投資組合回測(資金有限 + 滿倉換股)  |  benchmark = {rep['benchmark']}  |  "
          f"可進場選股 {rep['n_enterable']}/{rep['n_selections']}")
    print("=" * 104)
    print(f"  {'N':>2} {'M':>3} {'持有閘':>5} {'總報酬':>8} {'CAGR':>7} {'大盤CAGR':>8} {'超額':>7} "
          f"{'最大回撤':>8} {'大盤回撤':>8} {'交易':>5} {'換股/年':>7} {'勝率':>6}")
    print("  " + "-" * 100)
    best = max(rep["results"], key=lambda r: (r["excess_cagr_pct"] if r["excess_cagr_pct"] is not None else -1e9))
    for r in rep["results"]:
        mark = "  <<最佳超額" if r is best else ""
        print(f"  {r['N']:>2} {r['M']:>3} {r['min_hold']:>5} {_fmt(r['total_return_pct']):>8} "
              f"{_fmt(r['cagr_pct']):>7} {_fmt(r['bench_cagr_pct']):>8} {_fmt(r['excess_cagr_pct']):>7} "
              f"{_fmt(r['max_drawdown_pct']):>8} {_fmt(r['bench_mdd_pct']):>8} {r['n_trades']:>5} "
              f"{r['rotations_per_year']:>7} {str(r['trade_win_rate'])+'%':>6}{mark}")
    print("  " + "-" * 100)
    print("  CAGR/回撤為『固定資金、最多同時N檔、湊不滿擺現金』的權益曲線;超額 = 策略CAGR - 買TWII抱著CAGR。")
    print("  ⚠️ 誠實邊界同前:單一多頭段 + 倖存者偏誤 + 純技術層。超額為正也只代表『這段多頭贏過大盤』,非未來保證。")


def _json_safe(o):
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, np.floating):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def parse_args():
    p = argparse.ArgumentParser(description="純技術回測(第一版)")
    p.add_argument("--limit", type=int, default=None, help="只用前 N 檔(冒煙測試用)")
    p.add_argument("--start", help="重放起始日 YYYY-MM-DD")
    p.add_argument("--end", help="重放結束日 YYYY-MM-DD")
    p.add_argument("--no-regime", action="store_true", help="不套用大盤閘門,用固定 core_count/min_score")
    p.add_argument("--sweep", action="store_true", help="出場參數敏感度掃描(重放一次,套多組出場參數)")
    p.add_argument("--portfolio", action="store_true",
                   help="投資組合回測(資金有限:最多同時 N 檔 + 滿倉換股規則;掃 N×M×最短持有)")
    p.add_argument("--out", default=str(DATA_DIR / "backtest.json"), help="輸出 JSON 路徑")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 主控台預設 cp950,中文/符號會亂碼或崩潰
    except Exception:
        pass
    args = parse_args()
    if args.portfolio:
        rep = sweep_portfolio(universe_limit=args.limit, start=args.start, end=args.end,
                              use_regime=not args.no_regime)
        print_portfolio(rep)
        out = args.out if args.out != str(DATA_DIR / "backtest.json") else str(DATA_DIR / "backtest_portfolio.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(_json_safe(rep), f, ensure_ascii=False, indent=2, default=str)
        print(f"\n組合回測結果已寫入 {out}")
    elif args.sweep:
        rep = sweep_exits(universe_limit=args.limit, start=args.start, end=args.end,
                          use_regime=not args.no_regime)
        print_sweep(rep)
        out = args.out if args.out != str(DATA_DIR / "backtest.json") else str(DATA_DIR / "backtest_sweep.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(_json_safe(rep), f, ensure_ascii=False, indent=2, default=str)
        print(f"\n掃描結果已寫入 {out}")
    else:
        rep = run_backtest(universe_limit=args.limit, start=args.start, end=args.end,
                           use_regime=not args.no_regime)
        print_report(rep)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(_json_safe(rep), f, ensure_ascii=False, indent=2, default=str)
        print(f"\n完整結果已寫入 {args.out}")
