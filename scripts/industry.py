from __future__ import annotations
import pandas as pd


def compute_industry_trends(rows: list[dict], min_count: int = 3) -> list[dict]:
    """Aggregate per-stock daily metrics into an industry-level trend ranking.

    rows: list of dicts, each with keys:
        industry, stock_id, change_pct, above_ma20, above_ma60, bullish, ret20, combo_hit

    Returns industries sorted by composite score desc. Industries with fewer
    than `min_count` stocks are dropped (avoids tiny-sample noise).

    Score is a 0~100 composite of:
        站上季線比例 (35) + 多頭排列比例 (25) + 站上月線比例 (15) + 近20日動能 (25)
    """
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df = df[df["industry"].astype(bool)]
    if df.empty:
        return []

    agg = df.groupby("industry").agg(
        count=("stock_id", "count"),
        avg_change=("change_pct", "mean"),
        avg_ret20=("ret20", "mean"),
        breadth_ma20=("above_ma20", "mean"),
        breadth_ma60=("above_ma60", "mean"),
        bullish_ratio=("bullish", "mean"),
        combo_hits=("combo_hit", "sum"),
    ).reset_index()

    agg = agg[agg["count"] >= min_count]
    if agg.empty:
        return []

    def norm_ret(x: float) -> float:
        # 0% → 0, +30% or more → 1 (cap), negatives → 0
        return max(0.0, min(float(x), 0.30)) / 0.30

    agg["score"] = (
        agg["breadth_ma60"] * 35
        + agg["bullish_ratio"] * 25
        + agg["breadth_ma20"] * 15
        + agg["avg_ret20"].apply(norm_ret) * 25
    ).round(1)

    agg = agg.sort_values(["score", "combo_hits"], ascending=False).reset_index(drop=True)

    out = []
    for _, r in agg.iterrows():
        out.append({
            "industry": r["industry"],
            "count": int(r["count"]),
            "avg_change": round(float(r["avg_change"]), 2),
            "avg_ret20": round(float(r["avg_ret20"]) * 100, 1),
            "breadth_ma20": round(float(r["breadth_ma20"]) * 100, 0),
            "breadth_ma60": round(float(r["breadth_ma60"]) * 100, 0),
            "bullish_ratio": round(float(r["bullish_ratio"]) * 100, 0),
            "combo_hits": int(r["combo_hits"]),
            "score": float(r["score"]),
        })
    return out
