"""基本面(獲利能力 / 成長性 / 財務體質)— stage-2,只對核心候選算。

資料來自 FinMind 季財報 / 資產負債 / 現金流(+ 主流程已抓的月營收),逐檔快取成 parquet。
季資料一季才更新一次,故有「新鮮度守衛」:快取已含近一季就不重抓,大幅省 FinMind 額度。

對短線而言基本面是落後指標 → 預設**低權重**,主要當「體質過濾」(避開高負債/燒錢),
而非選誰會噴。所有計算缺資料即略過該項,不會讓流程當掉。
"""
from __future__ import annotations
from datetime import date, timedelta

import pandas as pd

from .fetchers import fetch_financial_statements, fetch_balance_sheet, fetch_cashflow
from .storage import (
    load_financials, upsert_financials,
    load_balance, upsert_balance,
    load_cashflow, upsert_cashflow,
)
from .utils import log

_FRESH_DAYS = 80   # 快取最新季 < 今天-80 天才重抓(季報約季末後 45 天出);避免每天打 FinMind


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _stale(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return True
    try:
        return (date.today() - df.index.max().date()).days > _FRESH_DAYS
    except Exception:
        return True


def update_fundamentals(stock_id: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """回傳 (financials, balance, cashflow);快取新鮮就不打 FinMind(省額度)。"""
    fin = load_financials(stock_id)
    bal = load_balance(stock_id)
    cf = load_cashflow(stock_id)
    if _stale(fin):
        new = fetch_financial_statements(stock_id)
        if not new.empty:
            fin = upsert_financials(stock_id, new)
    if _stale(bal):
        new = fetch_balance_sheet(stock_id)
        if not new.empty:
            bal = upsert_balance(stock_id, new)
    if _stale(cf):
        new = fetch_cashflow(stock_id)
        if not new.empty:
            cf = upsert_cashflow(stock_id, new)
    return fin, bal, cf


def _last(df: pd.DataFrame, col: str):
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    return float(s.iloc[-1]) if not s.empty else None


def _yoy(df: pd.DataFrame, col: str):
    """同季年增:最新季 vs 4 季前。"""
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    if len(s) < 5:
        return None
    prev = float(s.iloc[-5])
    if prev == 0:
        return None
    return (float(s.iloc[-1]) - prev) / abs(prev)


def fundamental_summary(financials: pd.DataFrame, balance: pd.DataFrame,
                        cashflow: pd.DataFrame, revenue_df: pd.DataFrame | None = None) -> dict:
    """抽出可顯示/可評分的最新基本面數字(缺的就不放)。比率類(毛利/營益/負債)穩健;
    ROE/ROA 以最新淨利÷權益(資產)的近似,跨公司比較性有限,僅供參考。"""
    out: dict = {}
    rev = _last(financials, "revenue")
    gp = _last(financials, "gross_profit")
    oi = _last(financials, "operating_income")
    ni = _last(financials, "net_income")
    eps = _last(financials, "eps")
    if eps is not None:
        out["eps_latest"] = round(eps, 2)
    yoy = _yoy(financials, "eps")
    if yoy is not None:
        out["eps_yoy"] = round(yoy * 100, 1)
    if rev and gp is not None:
        out["gross_margin"] = round(gp / rev * 100, 1)
    if rev and oi is not None:
        out["operating_margin"] = round(oi / rev * 100, 1)

    assets = _last(balance, "total_assets")
    liab = _last(balance, "total_liab")
    equity = _last(balance, "equity")
    if assets and liab is not None:
        out["debt_ratio"] = round(liab / assets * 100, 1)
    if equity and ni is not None:
        out["roe"] = round(ni / equity * 100, 1)
    if assets and ni is not None:
        out["roa"] = round(ni / assets * 100, 1)

    ocf = _last(cashflow, "op_cashflow")
    if ocf is not None:
        out["op_cashflow"] = round(ocf, 0)
        out["op_cashflow_positive"] = bool(ocf > 0)

    if revenue_df is not None and not revenue_df.empty and "revenue_yoy" in revenue_df.columns:
        ry = revenue_df["revenue_yoy"].dropna()
        if not ry.empty:
            out["revenue_yoy"] = round(float(ry.iloc[-1]) * 100, 1)
            tail = ry.tail(3)
            out["revenue_growth_months"] = int((tail > 0).sum())
    return out


def fundamental_score(summary: dict, cfg: dict | None = None) -> float | None:
    """0~1 基本面分:獲利能力 / 成長性 / 財務體質三組,有幾項算幾項。無任何資料回 None。"""
    cfg = cfg or {}
    g = cfg.get("gross_full", 40.0); o = cfg.get("operating_full", 20.0)
    ry_full = cfg.get("revenue_yoy_full", 20.0); eps_full = cfg.get("eps_yoy_full", 30.0)
    debt_lo = cfg.get("debt_good", 30.0); debt_hi = cfg.get("debt_bad", 70.0)

    parts: list[float] = []
    # 獲利能力
    if summary.get("gross_margin") is not None:
        parts.append(_clip01(summary["gross_margin"] / g))
    if summary.get("operating_margin") is not None:
        parts.append(_clip01(summary["operating_margin"] / o))
    if summary.get("eps_latest") is not None:
        parts.append(1.0 if summary["eps_latest"] > 0 else 0.0)
    # 成長性
    if summary.get("revenue_yoy") is not None:
        parts.append(_clip01(summary["revenue_yoy"] / ry_full))
    if summary.get("eps_yoy") is not None:
        parts.append(_clip01(summary["eps_yoy"] / eps_full))
    # 財務體質
    if summary.get("debt_ratio") is not None:
        d = summary["debt_ratio"]
        parts.append(_clip01((debt_hi - d) / (debt_hi - debt_lo)))   # 負債越低越高分
    if summary.get("op_cashflow_positive") is not None:
        parts.append(1.0 if summary["op_cashflow_positive"] else 0.0)
    if not parts:
        return None
    return sum(parts) / len(parts)
