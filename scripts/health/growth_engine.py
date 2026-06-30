"""成長能力(Growth)Engine — 個股健檢面向二。

月營收 YoY 用 ctx['revenue'](fetchers.fetch_monthly_revenue,index='YYYY-MM' 字串);
季成長/5年CAGR 用 ctx['financials'](季資料,quarters 需求 ≥20 季才有 5年CAGR,< 20 季時
該指標自動回 None,不是隨便拿不足的窗硬算)。
"""
from __future__ import annotations
import pandas as pd

from .metric import metric, missing_metric, engine_result, rating_from_thresholds, status_from_delta, avg_score, clip01, trend_point
from . import quarterly as q

_SRC_REV = "FinMind:TaiwanStockMonthRevenue"
_SRC_FS = "FinMind:TaiwanStockFinancialStatements"


def _monthly_trend(df: pd.DataFrame | None, col: str, periods: int = 18) -> list[dict]:
    if df is None or df.empty or col not in df.columns:
        return []
    s = df[col].dropna().tail(periods)
    return [trend_point(str(idx), v) for idx, v in s.items()]


def _consecutive_positive_months(df: pd.DataFrame | None, col: str = "revenue_yoy", max_check: int = 24) -> int:
    if df is None or df.empty or col not in df.columns:
        return 0
    s = df[col].dropna().tail(max_check)
    count = 0
    for v in reversed(s.tolist()):
        if v > 0:
            count += 1
        else:
            break
    return count


def _is_new_high(series: pd.Series) -> bool | None:
    s = series.dropna()
    if s.empty:
        return None
    return bool(s.iloc[-1] >= s.max())


def compute(ctx: dict) -> dict:
    rev = ctx.get("revenue")
    fin = ctx.get("financials")
    bench = ctx.get("industry_benchmarks") or {}
    updated = ctx.get("updated_at", "")

    metrics: list[dict] = []
    momentum: list[float] = []
    long_term: list[float] = []
    new_high: list[float] = []

    # ---------- 近況動能 ----------
    if rev is not None and not rev.empty and "revenue_yoy" in rev.columns:
        ry = rev["revenue_yoy"].dropna()
        latest_ym = rev.index[-1] if not rev.empty else ""
        if not ry.empty:
            latest = float(ry.iloc[-1]) * 100
            prev = float(ry.iloc[-2]) * 100 if len(ry) >= 2 else None
            metrics.append(metric(
                "monthly_revenue_yoy", "月營收年增率(YoY)", round(latest, 1), unit="%",
                trend=_monthly_trend(rev, "revenue_yoy"),
                industry_avg=bench.get("monthly_revenue_yoy"),
                status=status_from_delta(latest, prev),
                rating=rating_from_thresholds(latest, 10, -10),
                formula="本月營收 ÷ 去年同月營收 − 1,FinMind 已算好",
                source=_SRC_REV, asof=str(latest_ym), updated_at=updated,
            ))
            momentum.append(clip01((latest + 10) / 30))
        streak = _consecutive_positive_months(rev)
        metrics.append(metric(
            "revenue_growth_streak", "月營收連續正成長月數", streak, unit="個月",
            rating=("good" if streak >= 6 else ("neutral" if streak >= 1 else "bad")),
            formula="從最新月往回數,YoY 連續為正的月數",
            source=_SRC_REV, asof=str(latest_ym), updated_at=updated,
        ))
        rh = _is_new_high(rev["revenue"]) if "revenue" in rev.columns else None
        if rh is not None:
            metrics.append(metric(
                "revenue_new_high", "月營收是否創新高", "創新高" if rh else "未創新高",
                rating=("good" if rh else "neutral"),
                formula=f"本月營收 vs 歷史窗內(近{len(rev)}個月)最高月營收",
                source=_SRC_REV, asof=str(latest_ym), updated_at=updated,
            ))
            new_high.append(1.0 if rh else 0.0)
        else:
            metrics.append(missing_metric("revenue_new_high", "月營收是否創新高", source=_SRC_REV))
    else:
        metrics.append(missing_metric("monthly_revenue_yoy", "月營收年增率(YoY)", source=_SRC_REV))
        metrics.append(missing_metric("revenue_growth_streak", "月營收連續正成長月數", source=_SRC_REV))
        metrics.append(missing_metric("revenue_new_high", "月營收是否創新高", source=_SRC_REV))

    # ---------- 季成長(YoY) ----------
    asof_fs = q.last_period(fin, "revenue") or ""
    for key, label, col, full in (
        ("revenue_yoy_q", "季營收年增率", "revenue", 20),
        ("operating_income_yoy", "營業利益年增率", "operating_income", 30),
        ("net_income_yoy", "淨利年增率", "net_income", 30),
        ("eps_yoy", "EPS 年增率", "eps", 30),
    ):
        v = q.yoy(fin, col)
        if v is not None:
            v_pct = v * 100
            metrics.append(metric(
                key, label, round(v_pct, 1), unit="%",
                trend=q.trend(fin, col),
                industry_avg=bench.get(key),
                rating=rating_from_thresholds(v_pct, 10, -10),
                formula=f"(最新季{label[:2]} − 去年同季) ÷ |去年同季|", source=_SRC_FS, asof=asof_fs, updated_at=updated,
            ))
            momentum.append(clip01((v_pct + 10) / (full + 10)))
        else:
            metrics.append(missing_metric(key, label, source=_SRC_FS))

    # ---------- 長期增長(5年CAGR,需≥20季資料)----------
    for key, label, col in (
        ("revenue_cagr_5y", "營收 5 年 CAGR", "revenue"),
        ("net_income_cagr_5y", "淨利 5 年 CAGR", "net_income"),
        ("eps_cagr_5y", "EPS 5 年 CAGR", "eps"),
    ):
        v = q.cagr(fin, col, years=5)
        if v is not None:
            v_pct = v * 100
            metrics.append(metric(
                key, label, round(v_pct, 1), unit="%",
                rating=rating_from_thresholds(v_pct, 10, 0),
                formula="(最新季 ÷ 5年前同期季值) ^ (1/5) − 1,需累積 ≥20 季財報資料",
                source=_SRC_FS, asof=asof_fs, updated_at=updated,
            ))
            long_term.append(clip01((v_pct - 0) / 20))
        else:
            n = 0 if (fin is None or fin.empty or col not in fin.columns) else len(fin[col].dropna())
            metrics.append(missing_metric(
                key, label, source=_SRC_FS,
                reason="stale_cache" if n and n < 21 else "api_unavailable",
            ))

    # ---------- 獲利創高(以季淨利序列判斷)----------
    if fin is not None and not fin.empty and "net_income" in fin.columns:
        ph = _is_new_high(fin["net_income"])
        if ph is not None:
            metrics.append(metric(
                "profit_new_high", "獲利是否創新高(季)", "創新高" if ph else "未創新高",
                rating=("good" if ph else "neutral"),
                formula=f"最新季淨利 vs 歷史窗內(近{len(fin)}季)最高季淨利",
                source=_SRC_FS, asof=asof_fs, updated_at=updated,
            ))
            new_high.append(1.0 if ph else 0.0)
        else:
            metrics.append(missing_metric("profit_new_high", "獲利是否創新高(季)", source=_SRC_FS))
    else:
        metrics.append(missing_metric("profit_new_high", "獲利是否創新高(季)", source=_SRC_FS))

    notes = []
    if fin is None or fin.empty or len(fin) < 20:
        n = 0 if fin is None else len(fin)
        notes.append(f"目前累積季財報僅 {n} 季(<20),5年CAGR 系列指標暫不計算;隨每日批次持續累積會自動補上。")

    sub_scores = [s for s in (avg_score(momentum), avg_score(long_term), avg_score(new_high)) if s is not None]
    score = (sum(sub_scores) / len(sub_scores) * 100) if sub_scores else None
    return engine_result(score, metrics, notes=notes)
