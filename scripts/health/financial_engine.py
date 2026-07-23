"""財務體質(Financial Health)Engine — 個股健檢面向一。

資料來自 ctx['financials']/['balance']/['cashflow'](季資料,FinMind,quarters 由呼叫端決定;
健檢模組要求至少 8 季供趨勢、20 季供 Growth Engine 的 5 年 CAGR)。流動比/速動比/應收應付/
利息保障倍數用到的欄位是 2026-06-30 新增進 fetchers.py 的 type_map,候選名稱未實測驗證,
缺資料時對應 Metric 自動標 missing_reason,不影響其餘指標。

三組子分(獲利能力 / 償債能力 / 現金品質)各自「有幾項算幾項」平均,缺一項不強制補值,
沿用 fundamentals.fundamental_score() 既有精神。
"""
from __future__ import annotations

from .metric import metric, missing_metric, engine_result, rating_from_thresholds, status_from_delta, avg_score, clip01
from . import quarterly as q

_SRC_FS = "FinMind:TaiwanStockFinancialStatements"
_SRC_BS = "FinMind:TaiwanStockBalanceSheet"
_SRC_CF = "FinMind:TaiwanStockCashFlowsStatement"


def _ratio_metric(fin_or_bal, key, label, num, den, *, good, bad, higher_is_better=True,
                  formula, source, asof, updated, bench=None, scale=100.0):
    val = q.ratio(fin_or_bal, num, den, scale=scale)
    if val is None:
        return missing_metric(key, label, formula=formula, source=source), None
    prev = q.ratio(fin_or_bal, num, den, offset=1, scale=scale)
    m = metric(
        key, label, round(val, 2), unit="%",
        trend=q.ratio_trend(fin_or_bal, num, den, scale=scale),
        industry_avg=(bench or {}).get(key),
        status=status_from_delta(val, prev, higher_is_better=higher_is_better),
        rating=rating_from_thresholds(val, good, bad, higher_is_better=higher_is_better),
        formula=formula, source=source, asof=asof, updated_at=updated,
    )
    sub_score = clip01((val - bad) / (good - bad)) if higher_is_better else clip01((bad - val) / (bad - good))
    return m, sub_score


def compute(ctx: dict) -> dict:
    fin = ctx.get("financials"); bal = ctx.get("balance"); cf = ctx.get("cashflow")
    bench = ctx.get("industry_benchmarks") or {}
    updated = ctx.get("updated_at", "")
    asof_fs = q.last_period(fin, "revenue") or ""
    asof_bs = q.last_period(bal, "total_assets") or ""
    asof_cf = q.last_period(cf, "op_cashflow") or ""

    metrics: list[dict] = []
    profitability: list[float] = []
    solvency: list[float] = []
    cash_quality: list[float] = []

    # ---------- 獲利能力 ----------
    m, s = _ratio_metric(fin, "gross_margin", "毛利率", "gross_profit", "revenue",
                          good=30, bad=5, formula="毛利 ÷ 營收 × 100",
                          source=_SRC_FS, asof=asof_fs, updated=updated, bench=bench)
    metrics.append(m); profitability.append(s)

    m, s = _ratio_metric(fin, "operating_margin", "營業利益率", "operating_income", "revenue",
                          good=15, bad=0, formula="營業利益 ÷ 營收 × 100",
                          source=_SRC_FS, asof=asof_fs, updated=updated, bench=bench)
    metrics.append(m); profitability.append(s)

    m, s = _ratio_metric(fin, "net_margin", "淨利率", "net_income", "revenue",
                          good=10, bad=0, formula="稅後淨利 ÷ 營收 × 100",
                          source=_SRC_FS, asof=asof_fs, updated=updated, bench=bench)
    metrics.append(m); profitability.append(s)

    eps = q.last(fin, "eps")
    eps_prev = q.at(fin, "eps", 1)
    metrics.append(metric(
        "eps_latest", "最新季 EPS", round(eps, 2) if eps is not None else None, unit="元",
        trend=q.trend(fin, "eps"), industry_avg=bench.get("eps_latest"),
        status=status_from_delta(eps, eps_prev),
        rating=("good" if (eps is not None and eps > 0) else ("bad" if eps is not None else None)),
        formula="季財報揭露之每股盈餘", source=_SRC_FS, asof=asof_fs, updated_at=updated,
    ) if eps is not None else missing_metric("eps_latest", "最新季 EPS", source=_SRC_FS))
    if eps is not None:
        profitability.append(1.0 if eps > 0 else 0.0)

    # ROE/ROA 用「淨利 ÷ 權益(資產)」近似(分子分母來源不同表,quarterly.ratio 假設同一張表,
    # 故這裡手動算,而非借用 ratio()):
    ni_latest = q.last(fin, "net_income"); ni_prev_q = q.at(fin, "net_income", 1)
    equity_latest = q.last(bal, "equity"); equity_prev_q = q.at(bal, "equity", 1)
    assets_latest = q.last(bal, "total_assets"); assets_prev_q = q.at(bal, "total_assets", 1)
    roe_now = (ni_latest / equity_latest * 100) if (ni_latest is not None and equity_latest) else None
    roe_prev = (ni_prev_q / equity_prev_q * 100) if (ni_prev_q is not None and equity_prev_q) else None
    roa_now = (ni_latest / assets_latest * 100) if (ni_latest is not None and assets_latest) else None
    roa_prev = (ni_prev_q / assets_prev_q * 100) if (ni_prev_q is not None and assets_prev_q) else None
    if roe_now is not None:
        metrics.append(metric(
            "roe", "ROE(近似)", round(roe_now, 2), unit="%",
            industry_avg=bench.get("roe"), status=status_from_delta(roe_now, roe_prev),
            rating=rating_from_thresholds(roe_now, 15, 0),
            formula="單季淨利 ÷ 當期股東權益 × 100(近似,跨公司比較性有限,僅供參考)",
            source=f"{_SRC_FS} + {_SRC_BS}", asof=asof_fs, updated_at=updated,
        ))
        profitability.append(clip01((roe_now - 0) / 15))
    else:
        metrics.append(missing_metric("roe", "ROE(近似)", source=f"{_SRC_FS} + {_SRC_BS}"))
    if roa_now is not None:
        metrics.append(metric(
            "roa", "ROA(近似)", round(roa_now, 2), unit="%",
            industry_avg=bench.get("roa"), status=status_from_delta(roa_now, roa_prev),
            rating=rating_from_thresholds(roa_now, 8, 0),
            formula="單季淨利 ÷ 當期總資產 × 100(近似)",
            source=f"{_SRC_FS} + {_SRC_BS}", asof=asof_fs, updated_at=updated,
        ))
        profitability.append(clip01((roa_now - 0) / 8))
    else:
        metrics.append(missing_metric("roa", "ROA(近似)", source=f"{_SRC_FS} + {_SRC_BS}"))

    # ---------- 償債能力 ----------
    m, s = _ratio_metric(bal, "current_ratio", "流動比率", "current_assets", "current_liab",
                          good=150, bad=80, formula="流動資產 ÷ 流動負債 × 100",
                          source=_SRC_BS, asof=asof_bs, updated=updated, bench=bench)
    metrics.append(m); solvency.append(s)

    # 速動比需要 (流動資產-存貨)/流動負債;quarterly.ratio 只支援單一分子欄位,這裡手動算
    ca = q.last(bal, "current_assets"); inv = q.last(bal, "inventory"); cl = q.last(bal, "current_liab")
    ca_p = q.at(bal, "current_assets", 1); inv_p = q.at(bal, "inventory", 1); cl_p = q.at(bal, "current_liab", 1)
    if ca is not None and cl:
        quick_now = (ca - (inv or 0)) / cl * 100
        quick_prev = ((ca_p - (inv_p or 0)) / cl_p * 100) if (ca_p is not None and cl_p) else None
        metrics.append(metric(
            "quick_ratio", "速動比率", round(quick_now, 2), unit="%",
            industry_avg=bench.get("quick_ratio"), status=status_from_delta(quick_now, quick_prev),
            rating=rating_from_thresholds(quick_now, 100, 50),
            formula="(流動資產 − 存貨) ÷ 流動負債 × 100", source=_SRC_BS, asof=asof_bs, updated_at=updated,
        ))
        solvency.append(clip01((quick_now - 50) / (100 - 50)))
    else:
        metrics.append(missing_metric("quick_ratio", "速動比率", source=_SRC_BS))

    m, s = _ratio_metric(bal, "debt_ratio", "負債比率", "total_liab", "total_assets",
                          good=30, bad=70, higher_is_better=False,
                          formula="總負債 ÷ 總資產 × 100", source=_SRC_BS, asof=asof_bs, updated=updated, bench=bench)
    metrics.append(m); solvency.append(s)

    # 利息保障倍數:近12個月營業利益 ÷ 近12個月利息費用。利息費用取自現金流量表(損益表沒有此列),
    # 現金流是 YTD 累計故用 ttm_flow 去累計後滾動4季;營業利益是單季故用 ttm 直接滾動4季 → 同基期可相除。
    oi_ttm = q.ttm(fin, "operating_income"); ie_ttm = q.ttm_flow(cf, "interest_expense")
    if oi_ttm is not None and ie_ttm:
        cov_now = oi_ttm / abs(ie_ttm)
        oi_p = q.ttm(fin, "operating_income", offset=1); ie_p = q.ttm_flow(cf, "interest_expense", offset=1)
        cov_prev = (oi_p / abs(ie_p)) if (oi_p is not None and ie_p) else None
        metrics.append(metric(
            "interest_coverage", "利息保障倍數", round(cov_now, 2), unit="倍",
            industry_avg=bench.get("interest_coverage"), status=status_from_delta(cov_now, cov_prev),
            rating=rating_from_thresholds(cov_now, 5, 1),
            formula="近12個月營業利益 ÷ 近12個月利息費用(利息費用取自現金流量表,已去累計為單季再滾動4季)",
            source=f"{_SRC_FS} + {_SRC_CF}", asof=asof_cf, updated_at=updated,
        ))
        solvency.append(clip01((cov_now - 1) / (5 - 1)))
    else:
        metrics.append(missing_metric("interest_coverage", "利息保障倍數", source=f"{_SRC_FS} + {_SRC_CF}",
                                      reason="api_unavailable" if cf is not None and not cf.empty else "stale_cache"))

    # ---------- 現金品質 ----------
    ocf = q.last(cf, "op_cashflow"); ocf_prev = q.at(cf, "op_cashflow", 1)
    if ocf is not None:
        metrics.append(metric(
            "op_cashflow", "營業現金流", round(ocf, 0), unit="千元",
            trend=q.trend(cf, "op_cashflow"),
            status=status_from_delta(ocf, ocf_prev),
            rating=("good" if ocf > 0 else "bad"),
            formula="季財報現金流量表:營業活動之淨現金流入(出)",
            source=_SRC_CF, asof=asof_cf, updated_at=updated,
        ))
        cash_quality.append(1.0 if ocf > 0 else 0.0)
    else:
        metrics.append(missing_metric("op_cashflow", "營業現金流", source=_SRC_CF))

    # 自由現金流:近12個月營業現金流 − |近12個月資本支出|。兩者都在現金流量表(YTD 累計),
    # 用 ttm_flow 去累計後滾動4季 → 年化 FCF,正負判讀不受單季基期干擾。
    ocf_ttm = q.ttm_flow(cf, "op_cashflow"); capex_ttm = q.ttm_flow(cf, "capex")
    if ocf_ttm is not None and capex_ttm is not None:
        fcf = ocf_ttm - abs(capex_ttm)
        ocf_p = q.ttm_flow(cf, "op_cashflow", offset=1); capex_p = q.ttm_flow(cf, "capex", offset=1)
        fcf_prev = (ocf_p - abs(capex_p)) if (ocf_p is not None and capex_p is not None) else None
        metrics.append(metric(
            "free_cashflow", "自由現金流(近12個月)", round(fcf, 0), unit="千元",
            status=status_from_delta(fcf, fcf_prev),
            rating=("good" if fcf > 0 else "bad"),
            formula="近12個月營業現金流 − |近12個月資本支出|(現金流量表為YTD累計,已去累計為單季再滾動4季)",
            source=_SRC_CF, asof=asof_cf, updated_at=updated,
        ))
        cash_quality.append(1.0 if fcf > 0 else 0.0)
    else:
        metrics.append(missing_metric("free_cashflow", "自由現金流(近12個月)", source=_SRC_CF,
                                      reason="not_applicable" if ocf_ttm is None else "api_unavailable"))

    if ni_latest is not None and ocf is not None:
        diverging = bool(ni_latest > 0 and ocf < 0)
        metrics.append(metric(
            "earnings_quality", "淨利與現金流是否背離", "背離" if diverging else "一致",
            rating=("bad" if diverging else "good"),
            formula="淨利為正但營業現金流為負 → 標記背離(獲利品質紅旗,常見於認列未收現的營收)",
            source=f"{_SRC_FS} + {_SRC_CF}", asof=asof_cf, updated_at=updated,
        ))
        cash_quality.append(0.0 if diverging else 1.0)
    else:
        metrics.append(missing_metric("earnings_quality", "淨利與現金流是否背離", source=f"{_SRC_FS} + {_SRC_CF}"))

    cashflow_stable = None
    ocf_trend = q.trend(cf, "op_cashflow")
    if len(ocf_trend) >= 4:
        vals = [t["value"] for t in ocf_trend if t["value"] is not None]
        if len(vals) >= 4:
            pos_ratio = sum(1 for v in vals if v > 0) / len(vals)
            cashflow_stable = pos_ratio >= 0.75
            metrics.append(metric(
                "cashflow_stability", "現金流是否穩定", "穩定" if cashflow_stable else "不穩定",
                rating=("good" if cashflow_stable else "neutral"),
                formula=f"近{len(vals)}季營業現金流為正的比例 ≥ 75% 視為穩定(實際 {pos_ratio*100:.0f}%)",
                source=_SRC_CF, asof=asof_cf, updated_at=updated,
            ))
            cash_quality.append(1.0 if cashflow_stable else 0.3)

    notes = []
    if fin is None or fin.empty:
        notes.append("無季財報資料(FinMind 抓取失敗或尚未入榜過,可加入自選池讓系統開始累積)。")

    sub_scores = [s for s in (avg_score(profitability), avg_score(solvency), avg_score(cash_quality)) if s is not None]
    score = (sum(sub_scores) / len(sub_scores) * 100) if sub_scores else None
    return engine_result(score, metrics, notes=notes)
