"""信心分「全市場」重驗(2026-07-21)。

## 為什麼要做這件事

線上台帳量到的相關性只有 **−0.056**(信心分 vs 後續超額),看起來像「排序完全無效」。
但那個相關是**只在已入選的核心股之間**算的 —— 分數被截斷在 66~86 這一小段。
**Restriction of range** 會系統性地把相關壓向 0,所以那個數字**不能**用來下結論。

要分辨的是兩個世界(修法完全相反,猜錯浪費數週):

* **A —— 整體有效,只在高分區失去鑑別力。**
  → 信心分留著當**篩選器**,但**停止拿它做排序**;力氣改放在「門檻切在哪」。
* **B —— 整體就是雜訊。**
  → 整套評分邏輯要換,排序權重再怎麼調都是白工。

**判準:把每天**全市場**的評分股按分數分十組,看各組後續超額報酬是否單調遞增。**
十組單調排列 → A;雜亂無章 → B。

## 為什麼分組要「每天各自分」(cross-sectional)

分數的絕對水位會隨大盤起伏整體漂移(多頭時全市場趨勢分都高)。若把 58 個月的分數
倒在一起切十等分,切出來的其實泰半是「多頭日 vs 空頭日」,不是「強股 vs 弱股」——
會量到 regime 而不是選股力。**所以一律在「同一天之內」排名分組**,
每組再各自算超額,最後跨日平均。

## 事件時間 + 超額報酬

每筆從**它自己的選股日**往後數 h 天(event time),不是對齊到共同終點 ——
共同終點會讓後期樣本持有期變短、並把最後一段行情的方向灌進所有樣本
(見 memory/twse-live-ledger-negative-alpha)。
報酬一律減掉同期間的 TWII,量的是超額不是 beta。

## 正確性:沿用 backtest 的因果切片

指標全部是因果的(rolling/ewm/shift,無置中),所以「先對完整歷史算一次指標,
再用 `.iloc[:cut]` 切片餵評分」與「每天重切重算」逐位元一致,但快 ~400 倍。
細節與前提見 `backtest.py` 檔頭。

⚠️ **`sig_close` 必須取原始價**(`raws`),不是指標表的還原價 ——
補史後 adj 覆蓋率 ~100%,還原價比原始價低一個累積除息因子,拿它當分母會讓
前向報酬整體膨脹約 +10%(2026-07-18 踩過,見 memory/twse-backtest-signal-close-adj-bug)。

## 與既有 backtest 的差別

`backtest._replay` 只留 `trigger=True` 且 `score >= min_score` 的**前 10 名**;
這支**完全不過濾**,每天所有能評分的股票(約 860 檔)全部記錄 —— 這正是重驗的重點。
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict

import numpy as np
import pandas as pd

from .backtest import (WARMUP_BARS, _load_index_close, _prepare, _json_safe)
from .config import load_screeners, DATA_DIR
from .scoring import compute_conviction
from .utils import log

# 只看短線這幾個天期。20/30 日對隔日沖/週內策略沒有決策意義,
# 而且拖長 horizon 會讓樣本重疊更嚴重、統計更不獨立。
HORIZONS = [1, 3, 5, 10]
NBUCKETS = 10
# 高分區細切:核心選股實際落在最高的那一小段,整個 D10 太粗看不出「頂端是否失去鑑別力」
TOP_SLICES = [(90, 95), (95, 99), (99, 100)]


def _fwd(raw: pd.DataFrame, pos_d: int, sig_close: float) -> dict:
    """買在選股日收盤、持有 h 個交易日的報酬。用原始價(見檔頭警告)。"""
    n = len(raw)
    close = raw["close"]
    out = {}
    for h in HORIZONS:
        tp = pos_d + h
        if tp < n and sig_close:
            c = close.iloc[tp]
            out[h] = float(c / sig_close - 1) if pd.notna(c) else None
        else:
            out[h] = None
    return out


class _Acc:
    """一組(某個 bucket)的累加器。只留總和與計數,不留逐筆 —— 全市場 58 個月
    約 90 萬列,逐筆存下來沒必要也吃記憶體。"""

    __slots__ = ("n", "sum_ex", "win", "sum_raw", "sum_score")

    def __init__(self):
        self.n = {h: 0 for h in HORIZONS}
        self.sum_ex = {h: 0.0 for h in HORIZONS}
        self.sum_raw = {h: 0.0 for h in HORIZONS}
        self.win = {h: 0 for h in HORIZONS}
        self.sum_score = 0.0

    def add(self, score: float, rets: dict, bench: dict):
        self.sum_score += score
        for h in HORIZONS:
            r, b = rets.get(h), bench.get(h)
            if r is None or b is None:
                continue
            ex = r - b
            self.n[h] += 1
            self.sum_ex[h] += ex
            self.sum_raw[h] += r
            if ex > 0:
                self.win[h] += 1

    def out(self) -> dict:
        d = {"n": self.n[HORIZONS[0]],
             "avg_score": round(self.sum_score / max(1, self.n[HORIZONS[0]]), 1)}
        for h in HORIZONS:
            n = self.n[h]
            d[f"h{h}"] = {
                "n": n,
                "excess": round(self.sum_ex[h] / n * 100, 3) if n else None,
                "raw": round(self.sum_raw[h] / n * 100, 3) if n else None,
                "win": round(self.win[h] / n * 100, 1) if n else None,
            }
        return d


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """組序 vs 平均超額的等級相關。十組單調 → 接近 +1。
    自己算,免得為了一個數字多裝 scipy。"""
    pairs = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pairs) < 3:
        return None
    n = len(pairs)
    rx = _rank([p[0] for p in pairs])
    ry = _rank([p[1] for p in pairs])
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 3)


def _rank(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    for pos, i in enumerate(order):
        r[i] = pos + 1
    return r


def validate(universe_limit=None, start=None, end=None, every: int = 1) -> dict:
    cfg = load_screeners()
    score_cfg = dict(cfg.get("scoring", {}) or {})
    rank_cfg = cfg.get("ranking", {}) or {}
    score_cfg["min_dollar_volume"] = float(rank_cfg.get("min_dollar_volume", 30_000_000))
    min_score_cfg = float(rank_cfg.get("min_score", 45))

    index_close, bench_name = _load_index_close()
    if index_close is None:
        raise RuntimeError("缺 TWII 基準,無法算超額報酬")
    print(f"Benchmark: {bench_name}")
    raws, inds, master = _prepare(universe_limit, index_close)
    if not inds:
        raise RuntimeError("無可用股票資料")

    master_vals = master.values
    master_pos = {ts: i for i, ts in enumerate(master)}
    twii_m = index_close.reindex(master).ffill()
    twii_ma20 = twii_m.rolling(20).mean()
    ind_dates = {sid: ind.index.values for sid, ind in inds.items()}

    lo, hi = WARMUP_BARS, len(master) - 1
    if start:
        lo = max(lo, int(np.searchsorted(master_vals, np.datetime64(start), side="left")))
    if end:
        hi = min(hi, int(np.searchsorted(master_vals, np.datetime64(end), side="right")))
    days = master[lo:hi]
    if every > 1:
        # 每 N 個交易日取一天。**這不只是為了快,統計上其實更乾淨**:
        # 相鄰兩天的 5 日前向報酬有 4 天重疊,幾乎是同一個觀測值重複計入 ——
        # 樣本數看起來很大但有效樣本遠小於此,標準誤會被嚴重低估。
        # 取樣間隔 >= horizon 時,樣本之間才接近獨立。
        days = days[::every]
    print(f"重放交易日:{len(days)} 天 ({days[0].date()} -> {days[-1].date()})"
          + (f"　每 {every} 日取樣" if every > 1 else ""))

    buckets = [_Acc() for _ in range(NBUCKETS)]
    # 對照當日等權平均(橫斷面)的版本 —— 見迴圈裡的說明,這才是量排序能力的正確對照
    xs_buckets = [_Acc() for _ in range(NBUCKETS)]
    tops = {f"p{a}_{b}": _Acc() for a, b in TOP_SLICES}
    # 分 regime:記憶裡「動能是 beta 不是 alpha」的結論就是靠這種切法看出來的
    by_regime = {"strong": [_Acc() for _ in range(NBUCKETS)],
                 "weak": [_Acc() for _ in range(NBUCKETS)]}
    # 對照組:線上實際會選到的那一小撮(trigger 且 >= min_score 的前 10 名)
    live_like = _Acc()
    trig_acc, notrig_acc = _Acc(), _Acc()
    score_hist = defaultdict(int)
    day_rows = 0
    t0 = time.time()

    for di, d in enumerate(days):
        d64 = np.datetime64(d)
        pos_master = master_pos[d]
        weak = bool(pd.notna(twii_ma20.iloc[pos_master])
                    and twii_m.iloc[pos_master] < twii_ma20.iloc[pos_master])

        # 這一天的 benchmark 前向報酬:全市場共用,算一次就好
        bench = {}
        for h in HORIZONS:
            tp = pos_master + h
            if tp < len(master) and pd.notna(twii_m.iloc[pos_master]) \
                    and pd.notna(twii_m.iloc[tp]) and twii_m.iloc[pos_master]:
                bench[h] = float(twii_m.iloc[tp] / twii_m.iloc[pos_master] - 1)
            else:
                bench[h] = None
        if bench[HORIZONS[0]] is None:
            continue                      # 尾端沒有未來資料,整天跳過

        scored = []
        for sid, ind in inds.items():
            cut = int(np.searchsorted(ind_dates[sid], d64, side="right"))
            if cut < WARMUP_BARS:
                continue
            conv = compute_conviction(ind.iloc[:cut], None, cfg=score_cfg)
            if not conv:
                continue                  # 資料不足或流動性不足 = 本來就不在池子裡
            rc = raws[sid]["close"].iloc[cut - 1]
            if pd.isna(rc) or rc <= 0:
                continue
            scored.append((float(conv["score"]), sid, cut - 1, float(rc), conv))

        if len(scored) < NBUCKETS * 3:
            continue                      # 當天可評分家數太少,分十組沒意義

        scored.sort(key=lambda x: x[0])
        m = len(scored)
        day_rows += m
        # 當天入選線上核心的那幾檔(用同一套規則),供對照
        elig = sorted([s for s in scored if s[4].get("trigger") and s[0] >= min_score_cfg],
                      key=lambda x: -x[0])[:int(rank_cfg.get("core_count", 10))]
        elig_ids = {s[1] for s in elig}

        # ⚠️ **兩種 benchmark 都要算**(2026-07-21 加,第一次跑完才發現非加不可)。
        # 只用 TWII 時十組全是負的 —— 那不是「選股很爛」,是 **TWII 是市值加權**、
        # 被台積電那種權值股主導,而這裡是「每檔一票」的等權平均。大盤由少數大型股拉抬時,
        # 平均個股本來就會輸給指數,整張表被這個常數往下平移,看起來像全盤皆墨。
        #
        # 要衡量「分數有沒有排序能力」,正確的對照是**當日全體評分股的等權平均**:
        # 各組減掉它之後總和為零,剩下的純粹是橫斷面的高低差,與大盤漲跌完全無關。
        # TWII 版仍保留 —— 那回答的是另一個問題(「買這組能不能贏大盤」)。
        day_rets = []
        for sc, sid, pos_d, rc, conv in scored:
            r = _fwd(raws[sid], pos_d, rc)
            day_rets.append(r)
        xmean = {}
        for h in HORIZONS:
            vals = [r[h] for r in day_rets if r.get(h) is not None]
            xmean[h] = sum(vals) / len(vals) if vals else None

        for i, (sc, sid, pos_d, rc, conv) in enumerate(scored):
            rets = day_rets[i]
            if rets[HORIZONS[0]] is None:
                continue
            b = min(NBUCKETS - 1, i * NBUCKETS // m)      # 0 = 最低分組
            xs_buckets[b].add(sc, rets, xmean)
            buckets[b].add(sc, rets, bench)
            by_regime["weak" if weak else "strong"][b].add(sc, rets, bench)
            score_hist[int(sc // 5) * 5] += 1
            pct = i / m * 100
            for a, bb in TOP_SLICES:
                if a <= pct < bb or (bb == 100 and pct >= a):
                    tops[f"p{a}_{bb}"].add(sc, rets, bench)
            (trig_acc if conv.get("trigger") else notrig_acc).add(sc, rets, bench)
            if sid in elig_ids:
                live_like.add(sc, rets, bench)

        if (di + 1) % 50 == 0:
            print(f"  {di+1}/{len(days)}  {d.date()}  累計 {day_rows:,} 列  ({time.time()-t0:.0f}s)")

    print(f"重放完成:{day_rows:,} 檔·日, {time.time()-t0:.0f}s")

    out_b = [b.out() for b in buckets]
    out_x = [b.out() for b in xs_buckets]
    rep = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "benchmark": bench_name,
        "days": len(days), "rows": day_rows,
        "period": [str(days[0].date()), str(days[-1].date())],
        "horizons": HORIZONS,
        "buckets": out_b,
        "buckets_xs": out_x,
        "monotonicity_xs": {
            f"h{h}": _spearman(list(range(NBUCKETS)), [b[f"h{h}"]["excess"] for b in out_x])
            for h in HORIZONS
        },
        "spread_xs": {
            f"h{h}": (round(out_x[-1][f"h{h}"]["excess"] - out_x[0][f"h{h}"]["excess"], 3)
                      if out_x[-1][f"h{h}"]["excess"] is not None
                      and out_x[0][f"h{h}"]["excess"] is not None else None)
            for h in HORIZONS
        },
        # 中位以上是否還有排序能力 —— D10 vs D6。第一次跑完發現梯度幾乎全在下半部,
        # 這個數字直接回答「上半部值不值得排序」。
        "spread_xs_top_vs_mid": {
            f"h{h}": (round(out_x[-1][f"h{h}"]["excess"] - out_x[5][f"h{h}"]["excess"], 3)
                      if out_x[-1][f"h{h}"]["excess"] is not None
                      and out_x[5][f"h{h}"]["excess"] is not None else None)
            for h in HORIZONS
        },
        "top_slices": {k: v.out() for k, v in tops.items()},
        "by_regime": {k: [b.out() for b in v] for k, v in by_regime.items()},
        "trigger": trig_acc.out(), "no_trigger": notrig_acc.out(),
        "live_like_core": live_like.out(),
        "score_hist": dict(sorted(score_hist.items())),
        "monotonicity": {
            f"h{h}": _spearman(list(range(NBUCKETS)),
                               [b[f"h{h}"]["excess"] for b in out_b])
            for h in HORIZONS
        },
        "spread_top_minus_bottom": {
            f"h{h}": (round(out_b[-1][f"h{h}"]["excess"] - out_b[0][f"h{h}"]["excess"], 3)
                      if out_b[-1][f"h{h}"]["excess"] is not None
                      and out_b[0][f"h{h}"]["excess"] is not None else None)
            for h in HORIZONS
        },
    }
    return rep


def print_report(rep: dict) -> None:
    P = print
    P("=" * 78)
    P(f"信心分全市場重驗　{rep['period'][0]} → {rep['period'][1]}"
      f"　{rep['days']} 個交易日　{rep['rows']:,} 檔·日")
    P(f"基準:{rep['benchmark']}　報酬皆為**超額**(個股報酬 − 同期間大盤)")
    P("=" * 78)

    hs = rep["horizons"]
    P("\n【全市場十等分】每天在當日所有評分股中排名分組,D1=最低分 D10=最高分")
    P(f"{'組':<5}{'平均分':>7}{'樣本':>9}" + "".join(f"{'超額'+str(h)+'日':>11}" for h in hs))
    for i, b in enumerate(rep["buckets"], 1):
        row = f"D{i:<4}{b['avg_score']:>7.1f}{b['n']:>9,}"
        for h in hs:
            v = b[f"h{h}"]["excess"]
            row += f"{v:>+10.2f}%" if v is not None else f"{'-':>11}"
        P(row)

    P(f"\n{'單調性(Spearman,+1=完美遞增)':<32}"
      + "".join(f"{rep['monotonicity'][f'h{h}']:>11}" for h in hs))
    P(f"{'D10 − D1 價差':<32}"
      + "".join(f"{rep['spread_top_minus_bottom'][f'h{h}']:>+10.2f}%" for h in hs))

    P("\n【同一批,但對照『當日全體評分股等權平均』】")
    P("  ← 這才是量『排序能力』的正確對照。TWII 是市值加權、被權值股主導,")
    P("    等權平均個股輸給它是常態,那個常數位移與分數好壞無關。")
    P(f"{'組':<5}{'平均分':>7}{'樣本':>9}" + "".join(f"{'超額'+str(h)+'日':>11}" for h in hs))
    for i, b in enumerate(rep["buckets_xs"], 1):
        row = f"D{i:<4}{b['avg_score']:>7.1f}{b['n']:>9,}"
        for h in hs:
            v = b[f"h{h}"]["excess"]
            row += f"{v:>+10.2f}%" if v is not None else f"{'-':>11}"
        P(row)
    P(f"\n{'單調性(Spearman)':<32}"
      + "".join(f"{rep['monotonicity_xs'][f'h{h}']:>11}" for h in hs))
    P(f"{'D10 − D1(全距)':<32}"
      + "".join(f"{rep['spread_xs'][f'h{h}']:>+10.2f}%" for h in hs))
    P(f"{'D10 − D6(中位以上還有嗎)':<28}"
      + "".join(f"{rep['spread_xs_top_vs_mid'][f'h{h}']:>+10.2f}%" for h in hs))

    P("\n【高分區細切】核心選股實際落在最頂端 —— 這裡才看得出頂端是否失去鑑別力")
    P(f"{'百分位':<12}{'平均分':>7}{'樣本':>9}" + "".join(f"{'超額'+str(h)+'日':>11}" for h in hs))
    for k, v in rep["top_slices"].items():
        lab = k.replace("p", "").replace("_", "~") + "%"
        row = f"{lab:<12}{v['avg_score']:>7.1f}{v['n']:>9,}"
        for h in hs:
            e = v[f"h{h}"]["excess"]
            row += f"{e:>+10.2f}%" if e is not None else f"{'-':>11}"
        P(row)

    P("\n【對照】")
    for lab, key in (("trigger=True", "trigger"), ("trigger=False", "no_trigger"),
                     ("線上核心(前10)", "live_like_core")):
        v = rep[key]
        row = f"{lab:<18}{v['avg_score']:>7.1f}{v['n']:>9,}"
        for h in hs:
            e = v[f"h{h}"]["excess"]
            row += f"{e:>+10.2f}%" if e is not None else f"{'-':>11}"
        P(row)

    P("\n【分 regime 的十等分:超額 5 日】(大盤在月線上 / 下)")
    P(f"{'組':<5}{'強盤':>12}{'弱盤':>12}")
    for i in range(NBUCKETS):
        s = rep["by_regime"]["strong"][i]["h5"]["excess"]
        w = rep["by_regime"]["weak"][i]["h5"]["excess"]
        P(f"D{i+1:<4}" + (f"{s:>+11.2f}%" if s is not None else f"{'-':>12}")
          + (f"{w:>+11.2f}%" if w is not None else f"{'-':>12}"))

    P("\n" + "=" * 78)
    P("怎麼讀:十組單調遞增 → 信心分整體有效(世界 A),高分區平坦才是問題,")
    P("        對策是「留著當篩選器、別拿來排序」。")
    P("        十組雜亂無章 → 世界 B,整套評分邏輯要換,調權重是白工。")
    P("=" * 78)


def parse_args():
    p = argparse.ArgumentParser(description="信心分全市場重驗(十等分超額報酬)")
    p.add_argument("--limit", type=int, help="只用前 N 檔(冒煙測試用)")
    p.add_argument("--start", help="起始日 YYYY-MM-DD")
    p.add_argument("--end", help="結束日 YYYY-MM-DD")
    p.add_argument("--every", type=int, default=1,
                   help="每 N 個交易日取樣一天(預設 1=全部)。>=5 時樣本才接近獨立,"
                        "且大幅縮短重放時間")
    p.add_argument("--out", default=str(DATA_DIR / "score_validation.json"))
    return p.parse_args()


if __name__ == "__main__":
    a = parse_args()
    rep = validate(a.limit, a.start, a.end, a.every)
    print_report(rep)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(_json_safe(rep), f, ensure_ascii=False, indent=1)
    log.info(f"已寫出 {a.out}")
