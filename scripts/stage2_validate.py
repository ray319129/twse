"""Stage-2 重排是否有害 —— 驗證(2026-07-23)。

## 動機(見 memory/twse-score-revalidation-done)

信心分全市場重驗發現:**純技術層**重放的「線上核心前10」第 1 日 +0.28%,
但**真實線上台帳**第 1 日 −0.52pp。兩者的差 = stage-2 加成(分點/籌碼/財報/催化劑/
產業)+ 樣本期不同。若 stage-2 在扣分,影響比調任何權重都大。

## 為什麼能驗:signals 檔存了完整的加成拆解

每筆核心選股同時存了:
  score       = 純技術信心分
  rank_score  = score + branch/chip/fund/catalyst/industry/combo 各 bonus
  以及各 bonus 分項
→ 不需要重現 stage-2 的資料抓取(那些沒有 point-in-time 快照),
  歷史「當時實際算出來的加成」就在檔案裡。

## 三個檢驗(由弱到強)

1. **分項相關(within-core)**:入選的核心裡,加成大的是否表現比較好?
   用「日內排名」比較 —— 同一天的選股共享大盤漲跌,跨日直接混會量到 regime。
2. **排序位移**:每天用 score 排 vs 用 rank_score 排,位次被加成「推上來」的股
   之後表現如何 vs 被「壓下去」的。這直接檢驗「重排」這個動作。
3. **反事實組合(最強)**:對同樣 47 天重放「純技術 top-k」(k=當天實際核心數),
   與實際核心做**同日配對**比較(paired by day,大盤效應完全消掉)。
   A∩B(兩邊都選)、A−B(stage-2 推進來的)、B−A(stage-2 擠出去的)三組分開看。

## 誠實邊界

- 樣本只有 47 個交易日 / 181 筆,且同日選股高度相關(day-cluster)——
  所有比較以「日」為單位配對或平均,不要被 181 這個數字騙。
- branch_bonus 只有 15 筆(07-18 才上線)→ 標記樣本不足,不下結論。
- 檢驗 3 的反事實用**現在的**技術評分 config 重放;線上 07-18 前用舊權重 ——
  對「今天該不該關 stage-2」這個決策,現 config 的反事實才是對的,但與歷史
  實際選股比較時混了 config 演進的效果,解讀要記得。
- sig_close 用 signals 存的原始收盤(顯示價),前向報酬用 parquet 原始收盤 ——
  同基準,無還原價膨脹問題(見 memory/twse-backtest-signal-close-adj-bug)。
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict

import numpy as np
import pandas as pd

from .backtest import _load_index_close, _prepare, _json_safe, WARMUP_BARS
from .config import load_screeners, DATA_DIR, SIGNALS_DIR
from .scoring import compute_conviction
from .storage import load_prices
from .utils import log

HORIZONS = [1, 3, 5, 10]
BONUS_KEYS = ["branch_bonus", "chip_bonus", "fund_bonus",
              "catalyst_bonus", "industry_bonus", "combo_bonus"]
MIN_N = 30          # 分項樣本低於此標記「樣本不足」


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------

def load_core_picks() -> list[dict]:
    rows = []
    for f in sorted(glob.glob(str(SIGNALS_DIR / "*.json"))):
        try:
            d = json.loads(open(f, encoding="utf-8").read())
        except Exception:
            continue
        for c in d.get("core") or []:
            if not c.get("stock_id"):
                continue
            rows.append({
                "date": d.get("date") or f.split("\\")[-1].replace(".json", ""),
                "stock_id": str(c["stock_id"]), "name": c.get("name", ""),
                "score": c.get("score"), "rank_score": c.get("rank_score"),
                "close": c.get("close"),
                **{k: (c.get(k) or 0.0) for k in BONUS_KEYS},
            })
    return rows


def _fwd_returns(sid: str, date: str, sig_close: float) -> dict:
    """事件時間前向報酬(原始收盤)。日期不在索引(資料缺)回全 None。"""
    out = {h: None for h in HORIZONS}
    try:
        df = load_prices(sid)
        if df.empty or "close" not in df.columns or not sig_close:
            return out
        idx = df.index
        d64 = pd.Timestamp(date)
        pos_arr = idx.get_indexer([d64])
        pos = int(pos_arr[0])
        if pos < 0:
            return out
        closes = df["close"]
        for h in HORIZONS:
            tp = pos + h
            if tp < len(df):
                c = closes.iloc[tp]
                if pd.notna(c):
                    out[h] = float(c / sig_close - 1)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 檢驗 1:分項相關(within-core,日內去大盤)
# ---------------------------------------------------------------------------

def component_analysis(picks: list[dict]) -> dict:
    """每個加成分項:高加成組 vs 低/零加成組 的前向報酬差(以日為單位聚合)。
    另算「日內名次相關」:同一天內,加成大小的排名 vs 後續報酬的排名(Spearman,
    只用 >=3 檔的日子)—— 日內比較天生免疫大盤方向。"""
    out = {}
    by_day = defaultdict(list)
    for p in picks:
        by_day[p["date"]].append(p)

    for key in BONUS_KEYS:
        have = [p for p in picks if p.get("rank_score") is not None]
        n_pos = sum(1 for p in have if p[key] > 0)
        stat = {"n_with": n_pos, "n_total": len(have)}
        if n_pos < MIN_N:
            stat["verdict"] = "樣本不足"
            out[key] = stat
            continue
        # 高 vs 低:以「有無加成」切(加成幾乎人人有的分項改用中位數切)
        vals = [p[key] for p in have]
        thr = 0.0 if (n_pos / max(1, len(have))) < 0.7 else float(np.median(vals))
        stat["split_at"] = round(thr, 2)
        diffs = {h: [] for h in HORIZONS}
        for day, ps in by_day.items():
            hi = [p for p in ps if p.get("rank_score") is not None and p[key] > thr]
            lo = [p for p in ps if p.get("rank_score") is not None and p[key] <= thr]
            if not hi or not lo:
                continue                      # 這天沒有對照,跳過(同日配對的代價)
            for h in HORIZONS:
                hv = [p["rets"][h] for p in hi if p["rets"][h] is not None]
                lv = [p["rets"][h] for p in lo if p["rets"][h] is not None]
                if hv and lv:
                    diffs[h].append(float(np.mean(hv) - np.mean(lv)))
        for h in HORIZONS:
            d = diffs[h]
            stat[f"h{h}"] = {"days": len(d),
                             "hi_minus_lo": round(float(np.mean(d)) * 100, 3) if d else None,
                             "pos_days_pct": round(sum(1 for x in d if x > 0) / len(d) * 100, 1) if d else None}
        out[key] = stat
    return out


# ---------------------------------------------------------------------------
# 檢驗 2:排序位移(score 排名 → rank_score 排名)
# ---------------------------------------------------------------------------

def rank_shift_analysis(picks: list[dict]) -> dict:
    """同一天內:被加成推升名次的(promoted) vs 被壓低的(demoted)之後表現。
    只用同時有 score 與 rank_score、且當天 >=4 檔的日子。"""
    by_day = defaultdict(list)
    for p in picks:
        if p.get("score") is not None and p.get("rank_score") is not None:
            by_day[p["date"]].append(p)
    diffs = {h: [] for h in HORIZONS}
    used_days = 0
    for day, ps in by_day.items():
        if len(ps) < 4:
            continue
        r_tech = {id(p): i for i, p in enumerate(sorted(ps, key=lambda x: -x["score"]))}
        r_rank = {id(p): i for i, p in enumerate(sorted(ps, key=lambda x: -x["rank_score"]))}
        for p in ps:
            p["_shift"] = r_tech[id(p)] - r_rank[id(p)]      # >0 = 被加成往前推
        promoted = [p for p in ps if p["_shift"] > 0]
        demoted = [p for p in ps if p["_shift"] < 0]
        if not promoted or not demoted:
            continue
        used_days += 1
        for h in HORIZONS:
            pv = [p["rets"][h] for p in promoted if p["rets"][h] is not None]
            dv = [p["rets"][h] for p in demoted if p["rets"][h] is not None]
            if pv and dv:
                diffs[h].append(float(np.mean(pv) - np.mean(dv)))
    out = {"days_used": used_days}
    for h in HORIZONS:
        d = diffs[h]
        out[f"h{h}"] = {"promoted_minus_demoted": round(float(np.mean(d)) * 100, 3) if d else None,
                        "days": len(d),
                        "pos_days_pct": round(sum(1 for x in d if x > 0) / len(d) * 100, 1) if d else None}
    return out


# ---------------------------------------------------------------------------
# 檢驗 3:反事實組合(純技術 top-k vs 實際核心,同日配對)
# ---------------------------------------------------------------------------

def counterfactual_analysis(picks: list[dict], universe_limit=None,
                            score_cfg_override: dict | None = None,
                            shared: tuple | None = None) -> dict:
    """對 signals 的每個日期,用技術評分重放全市場,取 top-k(k=當天實際核心數,
    條件同線上:trigger=True),與實際核心同日配對比較。

    `score_cfg_override`:注入歷史版 config 的 scoring 區塊 —— 用來把
    「config 改版效應」與「stage-2 效應」拆開(2026-07-23 第一輪跑出重疊僅 22%,
    幾乎可斷定被 07-18 的 rs 轉負 v3 改版混淆;對照組必須用**當時線上**的權重)。
    `shared`:重用 (raws, inds, master),跑多組 config 時不必重算指標。"""
    cfg = load_screeners()
    score_cfg = dict(score_cfg_override if score_cfg_override is not None
                     else (cfg.get("scoring", {}) or {}))
    rank_cfg = cfg.get("ranking", {}) or {}
    score_cfg["min_dollar_volume"] = float(rank_cfg.get("min_dollar_volume", 30_000_000))

    actual_by_day = defaultdict(list)
    for p in picks:
        actual_by_day[p["date"]].append(p)
    dates = sorted(actual_by_day.keys())

    if shared is not None:
        raws, inds, master = shared
    else:
        index_close, _ = _load_index_close()
        raws, inds, master = _prepare(universe_limit, index_close)
    master_vals = master.values
    ind_dates = {sid: ind.index.values for sid, ind in inds.items()}

    day_pairs = []          # 每日:{"date","n","actual":avg rets,"tech":avg rets,"overlap"}
    promoted_rets = {h: [] for h in HORIZONS}   # stage-2 推進來的(實際有、技術top-k沒有)
    demoted_rets = {h: [] for h in HORIZONS}    # stage-2 擠出去的(技術top-k有、實際沒有)
    for day in dates:
        d64 = np.datetime64(day)
        k = len(actual_by_day[day])
        scored = []
        for sid, ind in inds.items():
            cut = int(np.searchsorted(ind_dates[sid], d64, side="right"))
            if cut < WARMUP_BARS:
                continue
            conv = compute_conviction(ind.iloc[:cut], None, cfg=score_cfg)
            if not conv or not conv.get("trigger"):
                continue
            rc = raws[sid]["close"].iloc[cut - 1]
            if pd.isna(rc) or rc <= 0:
                continue
            scored.append((float(conv["score"]), sid, cut - 1, float(rc)))
        scored.sort(key=lambda x: -x[0])
        tech_top = scored[:k]
        tech_ids = {s[1] for s in tech_top}
        act_ids = {p["stock_id"] for p in actual_by_day[day]}

        tech_rets = {h: [] for h in HORIZONS}
        for sc, sid, pos_d, rc in tech_top:
            closes = raws[sid]["close"]
            for h in HORIZONS:
                tp = pos_d + h
                if tp < len(closes) and pd.notna(closes.iloc[tp]):
                    r = float(closes.iloc[tp] / rc - 1)
                    tech_rets[h].append(r)
                    if sid not in act_ids:
                        demoted_rets[h].append(r)
        act_rets = {h: [p["rets"][h] for p in actual_by_day[day] if p["rets"][h] is not None]
                    for h in HORIZONS}
        for p in actual_by_day[day]:
            if p["stock_id"] not in tech_ids:
                for h in HORIZONS:
                    if p["rets"][h] is not None:
                        promoted_rets[h].append(p["rets"][h])

        day_pairs.append({
            "date": day, "k": k,
            "overlap": len(act_ids & tech_ids),
            **{f"act_h{h}": (float(np.mean(act_rets[h])) if act_rets[h] else None) for h in HORIZONS},
            **{f"tech_h{h}": (float(np.mean(tech_rets[h])) if tech_rets[h] else None) for h in HORIZONS},
        })

    out = {"days": len(day_pairs),
           "avg_overlap_pct": round(float(np.mean([d["overlap"] / d["k"] for d in day_pairs if d["k"]])) * 100, 1)
           if day_pairs else None}
    for h in HORIZONS:
        pairs = [(d[f"act_h{h}"], d[f"tech_h{h}"]) for d in day_pairs
                 if d[f"act_h{h}"] is not None and d[f"tech_h{h}"] is not None]
        diffs = [a - t for a, t in pairs]
        out[f"h{h}"] = {
            "days": len(pairs),
            "actual_avg": round(float(np.mean([a for a, _ in pairs])) * 100, 3) if pairs else None,
            "tech_avg": round(float(np.mean([t for _, t in pairs])) * 100, 3) if pairs else None,
            "actual_minus_tech": round(float(np.mean(diffs)) * 100, 3) if diffs else None,
            "pos_days_pct": round(sum(1 for x in diffs if x > 0) / len(diffs) * 100, 1) if diffs else None,
            "promoted_avg": round(float(np.mean(promoted_rets[h])) * 100, 3) if promoted_rets[h] else None,
            "promoted_n": len(promoted_rets[h]),
            "demoted_avg": round(float(np.mean(demoted_rets[h])) * 100, 3) if demoted_rets[h] else None,
            "demoted_n": len(demoted_rets[h]),
        }
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def validate(universe_limit=None, skip_counterfactual=False) -> dict:
    picks = load_core_picks()
    print(f"載入核心選股 {len(picks)} 筆 / {len({p['date'] for p in picks})} 天")
    for p in picks:
        p["rets"] = _fwd_returns(p["stock_id"], p["date"], p.get("close"))
    usable = [p for p in picks if any(p["rets"][h] is not None for h in HORIZONS)]
    print(f"有前向報酬的 {len(usable)} 筆")

    rep = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "n_picks": len(usable), "n_days": len({p["date"] for p in usable}),
        "components": component_analysis(usable),
        "rank_shift": rank_shift_analysis(usable),
    }
    if not skip_counterfactual:
        rep["counterfactual"] = counterfactual_analysis(usable, universe_limit)
    return rep


def print_report(rep: dict) -> None:
    P = print
    hs = HORIZONS
    P("=" * 78)
    P(f"Stage-2 重排驗證　{rep['n_picks']} 筆核心 / {rep['n_days']} 天(2026-05-08 起的真實線上選股)")
    P("=" * 78)

    P("\n【檢驗1|分項:高加成 − 低加成(同日配對,+ = 該加成在幫忙)】")
    P(f"{'分項':<16}{'有加成':>8}" + "".join(f"{str(h)+'日':>10}" for h in hs) + f"{'  註':<6}")
    for k, s in rep["components"].items():
        if s.get("verdict") == "樣本不足":
            P(f"{k:<16}{s['n_with']:>8}" + " " * 40 + "  樣本不足,不下結論")
            continue
        row = f"{k:<16}{s['n_with']:>8}"
        for h in hs:
            v = (s.get(f"h{h}") or {}).get("hi_minus_lo")
            row += f"{v:>+9.2f}%" if v is not None else f"{'-':>10}"
        row += f"  切點>{s.get('split_at')}"
        P(row)

    rs = rep["rank_shift"]
    P(f"\n【檢驗2|排序位移:被加成推升 − 被壓低(同日配對,{rs['days_used']} 天可比)】")
    row = f"{'promoted−demoted':<24}"
    for h in hs:
        v = (rs.get(f"h{h}") or {}).get("promoted_minus_demoted")
        row += f"{v:>+9.2f}%" if v is not None else f"{'-':>10}"
    P(row + "   (+ = 重排在加分)")

    cf = rep.get("counterfactual")
    if cf:
        P(f"\n【檢驗3|反事實:實際核心 vs 純技術top-k(同日配對,{cf['days']} 天,重疊 {cf['avg_overlap_pct']}%)】")
        P(f"{'':<16}" + "".join(f"{str(h)+'日':>10}" for h in hs))
        for lab, key in (("實際核心", "actual_avg"), ("純技術top-k", "tech_avg"),
                         ("差(實際−技術)", "actual_minus_tech")):
            row = f"{lab:<16}"
            for h in hs:
                v = (cf.get(f"h{h}") or {}).get(key)
                row += f"{v:>+9.2f}%" if v is not None else f"{'-':>10}"
            P(row)
        P(f"\n  stage-2 推進來的(實際有、技術無):n={cf['h1']['promoted_n']}"
          + "".join(f"  {h}日 {cf[f'h{h}']['promoted_avg']:+.2f}%" if cf[f'h{h}']['promoted_avg'] is not None else "" for h in hs))
        P(f"  stage-2 擠出去的(技術有、實際無):n={cf['h1']['demoted_n']}"
          + "".join(f"  {h}日 {cf[f'h{h}']['demoted_avg']:+.2f}%" if cf[f'h{h}']['demoted_avg'] is not None else "" for h in hs))
    P("\n" + "=" * 78)
    P("⚠️ 47 天樣本、同日選股高度相關 —— 方向可參考,幅度信心區間很寬。")
    P("⚠️ 檢驗3 的技術評分用現行 config;07-18 前線上用舊權重,比較混了 config 演進。")
    P("=" * 78)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stage-2 重排驗證")
    ap.add_argument("--limit", type=int, help="反事實重放只用前 N 檔(冒煙測試)")
    ap.add_argument("--skip-cf", action="store_true", help="跳過反事實重放(快速看檢驗1/2)")
    ap.add_argument("--out", default=str(DATA_DIR / "stage2_validation.json"))
    a = ap.parse_args()
    rep = validate(a.limit, a.skip_cf)
    print_report(rep)
    json.dump(_json_safe(rep), open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log.info(f"已寫出 {a.out}")
