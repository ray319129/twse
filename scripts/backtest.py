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
from .track import _simulate_exit, _style_of, HORIZONS

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


def run_backtest(universe_limit: int | None = None, start: str | None = None,
                 end: str | None = None, use_regime: bool = True) -> dict:
    cfg = load_screeners()
    score_cfg = dict(cfg.get("scoring", {}) or {})
    rank_cfg = cfg.get("ranking", {}) or {}
    score_cfg["min_dollar_volume"] = float(rank_cfg.get("min_dollar_volume", 30_000_000))
    exit_cfg = cfg.get("exit", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    cost_cfg = cfg.get("cost", {}) or {}
    market_cfg = cfg.get("market", {}) or {}
    max_chase = float(entry_cfg.get("max_chase", 0.03))
    fixed_core = int(rank_cfg.get("core_count", 10))
    fixed_min_score = float(rank_cfg.get("min_score", 45))
    lu_thr = float(market_cfg.get("limit_up_pct", 0.095)) * 100
    bo_pen = float(market_cfg.get("breakout_penalty_weak", 8.0))

    index_close, bench_name = _load_index_close()
    print(f"Benchmark: {bench_name}")
    raws, inds, master = _prepare(universe_limit, index_close)
    if not inds:
        raise RuntimeError("無可用股票資料")

    # master 交易日的位置索引 + 對齊到 master 的 TWII(供超額報酬 benchmark;缺日 ffill)
    master_vals = master.values
    master_pos = {ts: i for i, ts in enumerate(master)}
    if index_close is not None:
        twii_m = index_close.reindex(master).ffill()
        twii_ma20 = twii_m.rolling(20).mean()
    else:
        twii_m = pd.Series(index=master, dtype=float)
        twii_ma20 = twii_m

    # 每檔的日期 numpy 陣列(searchsorted 用)
    ind_dates = {sid: ind.index.values for sid, ind in inds.items()}

    # 重放區間:留 WARMUP_BARS 暖身;最後一天不重放(需隔日開盤才能進場)
    lo = WARMUP_BARS
    hi = len(master) - 1
    if start:
        s = np.datetime64(start)
        lo = max(lo, int(np.searchsorted(master_vals, s, side="left")))
    if end:
        e = np.datetime64(end)
        hi = min(hi, int(np.searchsorted(master_vals, e, side="right")))
    replay_days = master[lo:hi]
    print(f"重放交易日:{len(replay_days)} 天 ({replay_days[0].date()} → {replay_days[-1].date()})")

    picks: list[dict] = []      # 每一筆選股(含訊號報酬 + 撮合結果 + benchmark)
    t0 = time.time()
    for di, d in enumerate(replay_days):
        d64 = np.datetime64(d)
        pos_master = master_pos[d]
        index_below_ma20 = False
        if index_close is not None and pd.notna(twii_ma20.iloc[pos_master]):
            index_below_ma20 = bool(twii_m.iloc[pos_master] < twii_ma20.iloc[pos_master])

        day_scored: list[dict] = []
        # 市場廣度(regime 用):對所有 >=60 根的股票統計,近似線上 industry_rows 的廣度
        b_n = b_above = b_adv = b_dec = b_lu = b_ld = 0
        for sid, ind in inds.items():
            cut = int(np.searchsorted(ind_dates[sid], d64, side="right"))
            if cut < WARMUP_BARS:
                continue
            sl = ind.iloc[:cut]
            last = sl.iloc[-1]
            close_v = last.get("close")
            ma20_v = last.get("ma20")
            # --- 廣度統計 ---
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
            # --- 評分 ---
            conv = compute_conviction(sl, None, cfg=score_cfg)
            if conv and conv.get("trigger"):
                sig_close = float(close_v) if pd.notna(close_v) else None
                if sig_close:
                    conv["stock_id"] = sid
                    conv["sig_close"] = sig_close
                    conv["_pos_d"] = cut - 1
                    day_scored.append(conv)

        # --- 大盤閘門:決定當日 core_count / min_score / prefer_pullback ---
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

        # --- 選核心:達門檻的觸發股,依 score 排序(弱盤對純追突破扣分),取前 core_count ---
        def _key(s):
            base = float(s["score"])
            if prefer_pb and s.get("breakout") and not s.get("pullback_turn"):
                base -= bo_pen
            return -base
        selected = sorted([s for s in day_scored if s["score"] >= min_score], key=_key)[:core_count]

        for s in selected:
            sid = s["stock_id"]; raw = raws[sid]; pos_d = s["_pos_d"]; sig_close = s["sig_close"]
            style = _style_of(s)
            sim = _simulate_exit(raw, d.isoformat(), sig_close, style, exit_cfg, max_chase, cost_cfg)
            sig_rets = _signal_returns(raw, pos_d, sig_close)
            # benchmark(選股層):TWII 同 horizon 報酬 → 超額
            bench_rets: dict[int, float | None] = {}
            for h in HORIZONS:
                tp = pos_master + h
                if index_close is not None and tp < len(master) \
                        and pd.notna(twii_m.iloc[pos_master]) and pd.notna(twii_m.iloc[tp]) and twii_m.iloc[pos_master]:
                    bench_rets[h] = float(twii_m.iloc[tp] / twii_m.iloc[pos_master] - 1)
                else:
                    bench_rets[h] = None
            # benchmark(執行層):進場日→出場日 TWII 報酬(對齊 master 日位置)
            bench_exec = None
            if sim and sim.get("status") == "closed" and sim.get("hold_days") is not None \
                    and index_close is not None:
                ep = pos_master + 1
                xp = ep + int(sim["hold_days"])
                if 0 <= ep < len(master) and 0 <= xp < len(master) \
                        and pd.notna(twii_m.iloc[ep]) and pd.notna(twii_m.iloc[xp]) and twii_m.iloc[ep]:
                    bench_exec = float(twii_m.iloc[xp] / twii_m.iloc[ep] - 1)

            picks.append({
                "date": d.isoformat(), "stock_id": sid, "score": s["score"],
                "profile": s.get("profile"),
                "breakout": bool(s.get("breakout")), "pullback_turn": bool(s.get("pullback_turn")),
                "new_stock": bool(s.get("new_stock")),
                "index_below_ma20": index_below_ma20,
                "sig_close": round(sig_close, 2),
                "sig_rets": sig_rets, "bench_rets": bench_rets,
                "exit": sim, "bench_exec": bench_exec,
            })

        if (di + 1) % 20 == 0:
            print(f"  replay {di+1}/{len(replay_days)}  {d.date()}  累計選股 {len(picks)} 筆  ({time.time()-t0:.0f}s)")

    print(f"重放完成:{len(picks)} 筆選股, {time.time()-t0:.0f}s")
    return _aggregate(picks, bench_name, replay_days)


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
    p.add_argument("--out", default=str(DATA_DIR / "backtest.json"), help="輸出 JSON 路徑")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 主控台預設 cp950,中文/符號會亂碼或崩潰
    except Exception:
        pass
    args = parse_args()
    rep = run_backtest(universe_limit=args.limit, start=args.start, end=args.end,
                       use_regime=not args.no_regime)
    print_report(rep)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(_json_safe(rep), f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整結果已寫入 {args.out}")
