"""季資料存取小工具 — financial/growth/risk engine 共用,避免三個模組各自重寫
「拿最新一筆/拿N期前/算YoY/算趨勢序列」這幾個一定會重複的小函式。

所有函式對「資料不足」一律回 None / [],不丟例外 — 健檢模組的核心原則是
「缺資料就誠實標缺,不要讓整個 Engine 當掉」。
"""
from __future__ import annotations
import pandas as pd

from .metric import trend_point


def _period_label(idx) -> str:
    try:
        ts = pd.Timestamp(idx)
        q = (ts.month - 1) // 3 + 1
        return f"{ts.year}Q{q}"
    except Exception:
        return str(idx)


def last(df: pd.DataFrame | None, col: str) -> float | None:
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    return float(s.iloc[-1]) if not s.empty else None


def last_period(df: pd.DataFrame | None, col: str) -> str | None:
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    return _period_label(s.index[-1]) if not s.empty else None


def at(df: pd.DataFrame | None, col: str, offset: int) -> float | None:
    """offset=1→上一期,offset=4→4期前(季資料=去年同季)。不足回 None。"""
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    if len(s) <= offset:
        return None
    return float(s.iloc[-1 - offset])


def yoy(df: pd.DataFrame | None, col: str) -> float | None:
    cur, prev = last(df, col), at(df, col, 4)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def qoq(df: pd.DataFrame | None, col: str) -> float | None:
    cur, prev = last(df, col), at(df, col, 1)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def trend(df: pd.DataFrame | None, col: str, periods: int = 8) -> list[dict]:
    if df is None or df.empty or col not in df.columns:
        return []
    s = df[col].dropna().tail(periods)
    return [trend_point(_period_label(idx), v) for idx, v in s.items()]


def ratio(df: pd.DataFrame | None, num_col: str, den_col: str, *, offset: int = 0, scale: float = 1.0) -> float | None:
    """同一期 num_col/den_col 的比值(如毛利率=gross_profit/revenue)。offset 同 at()。"""
    if df is None or df.empty or num_col not in df.columns or den_col not in df.columns:
        return None
    sub = df[[num_col, den_col]].dropna()
    if len(sub) <= offset:
        return None
    row = sub.iloc[-1 - offset]
    d = row[den_col]
    if d == 0 or pd.isna(d):
        return None
    return float(row[num_col]) / float(d) * scale


def ratio_trend(df: pd.DataFrame | None, num_col: str, den_col: str, *, periods: int = 8, scale: float = 1.0) -> list[dict]:
    if df is None or df.empty or num_col not in df.columns or den_col not in df.columns:
        return []
    sub = df[[num_col, den_col]].dropna().tail(periods)
    out = []
    for idx, row in sub.iterrows():
        d = row[den_col]
        v = (float(row[num_col]) / float(d) * scale) if d != 0 and not pd.isna(d) else None
        out.append(trend_point(_period_label(idx), v))
    return out


def cagr(df: pd.DataFrame | None, col: str, years: int = 5) -> float | None:
    """近 years 年 CAGR(季資料,首尾各一筆;首/尾任一 <=0 視為不可比,回 None)。"""
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    n_q = years * 4
    if len(s) <= n_q:
        return None
    start, end = float(s.iloc[-1 - n_q]), float(s.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def consecutive(df: pd.DataFrame | None, col: str, *, negative: bool = True, max_check: int = 12) -> int:
    """近幾期連續為負(negative=True)或連續為正(negative=False)的期數,從最新一期往回數。"""
    if df is None or df.empty or col not in df.columns:
        return 0
    s = df[col].dropna().tail(max_check)
    if s.empty:
        return 0
    count = 0
    for v in reversed(s.tolist()):
        hit = (v < 0) if negative else (v > 0)
        if hit:
            count += 1
        else:
            break
    return count
