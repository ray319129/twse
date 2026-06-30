"""風險分析(Risk)Engine — 個股健檢面向四。

分數方向與其他 Engine 相反:**分數越高越安全**。設計刻意不走加權平均掩蓋極端風險 ——
凡命中下列任一「Critical 規則」(同時發生才算,單一指標異常不足以斷定),總分直接封頂,
不被其他健康指標稀釋(詳見 specs)。

Tier 1(本檔可立即計算)沿用/擴充 financial_engine 同一批資料,不重抓。
Tier 2(董監質押 / 重大違約 / 重大減資 / 財報重編)目前沒有確認可行的免費結構化資料源
(專案過去已查證分點/目標價類資料的免費管道限制,情況類似),只能靠 News Engine
「新聞剛好有報導」補(ctx['news_risk_flags'],非完整監控,Metric 誠實標 missing_reason)。
"""
from __future__ import annotations

from .metric import metric, missing_metric, engine_result, clip01
from . import quarterly as q

_SRC_FS = "FinMind:TaiwanStockFinancialStatements"
_SRC_BS = "FinMind:TaiwanStockBalanceSheet"
_SRC_CF = "FinMind:TaiwanStockCashFlowsStatement"
_SRC_REV = "FinMind:TaiwanStockMonthRevenue"
_SRC_NEWS = "Google News(AI 分類,僅涵蓋新聞剛好報導的事件)"

_LEVELS = ("Low", "Medium", "High", "Critical")


def compute(ctx: dict) -> dict:
    fin = ctx.get("financials"); bal = ctx.get("balance"); cf = ctx.get("cashflow")
    rev = ctx.get("revenue")
    updated = ctx.get("updated_at", "")
    asof_fs = q.last_period(fin, "revenue") or ""
    asof_bs = q.last_period(bal, "total_assets") or ""

    metrics: list[dict] = []
    rules: list[dict] = []   # {key,label,hit,penalty,critical,detail}

    # ---------- 1. 連續虧損季數 ----------
    loss_streak = q.consecutive(fin, "net_income", negative=True)
    metrics.append(metric(
        "consecutive_loss_quarters", "連續虧損季數", loss_streak, unit="季",
        rating=("bad" if loss_streak >= 2 else "good"),
        formula="從最新季往回數,稅後淨利連續為負的季數",
        source=_SRC_FS, asof=asof_fs, updated_at=updated,
    ))
    rules.append({"key": "consecutive_loss", "label": f"連續虧損 {loss_streak} 季",
                 "hit": loss_streak >= 2, "penalty": min(loss_streak * 8, 30), "critical": False})

    # ---------- 2. 連續營收衰退月數 ----------
    decline_streak = 0
    if rev is not None and not rev.empty and "revenue_yoy" in rev.columns:
        s = rev["revenue_yoy"].dropna().tail(24)
        for v in reversed(s.tolist()):
            if v < 0:
                decline_streak += 1
            else:
                break
    metrics.append(metric(
        "revenue_decline_streak", "連續營收衰退月數", decline_streak, unit="個月",
        rating=("bad" if decline_streak >= 3 else "good"),
        formula="從最新月往回數,月營收 YoY 連續為負的月數",
        source=_SRC_REV, asof=(str(rev.index[-1]) if rev is not None and not rev.empty else ""),
        updated_at=updated,
    ))
    rules.append({"key": "revenue_decline", "label": f"連續營收衰退 {decline_streak} 個月",
                 "hit": decline_streak >= 3, "penalty": min(decline_streak * 5, 25), "critical": False})

    # ---------- 3. 現金流異常(淨利為正但OCF為負)----------
    ni = q.last(fin, "net_income"); ocf = q.last(cf, "op_cashflow")
    cashflow_anomaly = bool(ni is not None and ocf is not None and ni > 0 and ocf < 0)
    if ni is not None and ocf is not None:
        metrics.append(metric(
            "cashflow_anomaly", "現金流是否異常(淨利轉現金流背離)", "異常" if cashflow_anomaly else "正常",
            rating=("bad" if cashflow_anomaly else "good"),
            formula="淨利為正但營業現金流為負 → 標記異常(獲利品質紅旗)",
            source=f"{_SRC_FS} + {_SRC_CF}", asof=asof_fs, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("cashflow_anomaly", "現金流是否異常(淨利轉現金流背離)",
                                      source=f"{_SRC_FS} + {_SRC_CF}"))
    rules.append({"key": "cashflow_anomaly", "label": "淨利為正但營業現金流為負",
                 "hit": cashflow_anomaly, "penalty": 15, "critical": False})

    # ---------- 4. 負債比快速增加 ----------
    debt_now = None
    ta, tl = q.last(bal, "total_assets"), q.last(bal, "total_liab")
    if ta and tl is not None:
        debt_now = tl / ta * 100
    debt_yoy_delta = None
    ta_p, tl_p = q.at(bal, "total_assets", 4), q.at(bal, "total_liab", 4)
    if ta and tl is not None and ta_p and tl_p is not None:
        debt_prev = tl_p / ta_p * 100
        debt_yoy_delta = debt_now - debt_prev
    debt_spike = bool(debt_yoy_delta is not None and debt_yoy_delta >= 15)
    debt_critical = bool(debt_spike and debt_now is not None and debt_now > 70 and debt_yoy_delta >= 20)
    if debt_yoy_delta is not None:
        metrics.append(metric(
            "debt_ratio_yoy_change", "負債比年增(百分點)", round(debt_yoy_delta, 1), unit="pp",
            rating=("bad" if debt_spike else "good"),
            formula="本季負債比 − 去年同季負債比(百分點差)",
            source=_SRC_BS, asof=asof_bs, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("debt_ratio_yoy_change", "負債比年增(百分點)", source=_SRC_BS))
    rules.append({"key": "debt_spike", "label": f"負債比年增 {debt_yoy_delta:.1f}pp" if debt_yoy_delta is not None else "負債比年增",
                 "hit": debt_spike, "penalty": 20, "critical": debt_critical})

    # ---------- 5. 月營收年增暴跌 ----------
    rev_crash = False
    latest_yoy = None
    if rev is not None and not rev.empty and "revenue_yoy" in rev.columns:
        s = rev["revenue_yoy"].dropna()
        if not s.empty:
            latest_yoy = float(s.iloc[-1]) * 100
            rev_crash = latest_yoy < -30
    if latest_yoy is not None:
        metrics.append(metric(
            "revenue_crash", "月營收是否暴跌", "暴跌" if rev_crash else "正常",
            rating=("bad" if rev_crash else "good"),
            formula="最新月營收 YoY < −30% 視為暴跌",
            source=_SRC_REV, asof=(str(rev.index[-1]) if rev is not None else ""), updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("revenue_crash", "月營收是否暴跌", source=_SRC_REV))
    rev_crash_critical = bool(latest_yoy is not None and latest_yoy < -50)
    rules.append({"key": "revenue_crash", "label": f"月營收YoY {latest_yoy:.1f}%" if latest_yoy is not None else "月營收YoY",
                 "hit": rev_crash, "penalty": 20, "critical": rev_crash_critical})

    # ---------- 6. 應收帳款成長 > 營收成長 ----------
    ar_yoy = q.yoy(bal, "accounts_receivable")
    rev_yoy_q = q.yoy(fin, "revenue")
    ar_outpace = bool(ar_yoy is not None and rev_yoy_q is not None and (ar_yoy - rev_yoy_q) >= 0.15)
    if ar_yoy is not None and rev_yoy_q is not None:
        metrics.append(metric(
            "receivables_outpace_revenue", "應收帳款成長是否超過營收成長", "超過" if ar_outpace else "正常",
            rating=("bad" if ar_outpace else "good"),
            formula="應收帳款年增率 − 營收年增率 ≥ 15 個百分點 → 標記(可能放寬信用條件衝營收/或收現變慢)",
            source=f"{_SRC_BS} + {_SRC_FS}", asof=asof_bs, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("receivables_outpace_revenue", "應收帳款成長是否超過營收成長",
                                      source=f"{_SRC_BS} + {_SRC_FS}"))
    rules.append({"key": "ar_outpace", "label": "應收帳款成長超過營收成長",
                 "hit": ar_outpace, "penalty": 12, "critical": False})

    # ---------- 7. 存貨成長異常 ----------
    inv_yoy = q.yoy(bal, "inventory")
    inv_outpace = bool(inv_yoy is not None and rev_yoy_q is not None and (inv_yoy - rev_yoy_q) >= 0.20)
    if inv_yoy is not None and rev_yoy_q is not None:
        metrics.append(metric(
            "inventory_outpace_revenue", "存貨成長是否異常", "異常" if inv_outpace else "正常",
            rating=("bad" if inv_outpace else "good"),
            formula="存貨年增率 − 營收年增率 ≥ 20 個百分點 → 標記(可能去化變慢/未來有減損或跌價風險)",
            source=f"{_SRC_BS} + {_SRC_FS}", asof=asof_bs, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("inventory_outpace_revenue", "存貨成長是否異常",
                                      source=f"{_SRC_BS} + {_SRC_FS}"))
    rules.append({"key": "inventory_outpace", "label": "存貨成長異常",
                 "hit": inv_outpace, "penalty": 10, "critical": False})

    # ---------- Critical 組合規則(同時發生才算,單一指標不足以斷定)----------
    combo_critical = bool(loss_streak >= 4 and ocf is not None and ocf < 0)
    rules.append({"key": "combo_loss_cashflow", "label": "連續虧損≥4季 且 最新季營業現金流為負",
                 "hit": combo_critical, "penalty": 35, "critical": combo_critical})

    # ---------- Tier 2:新聞最佳努力涵蓋(非完整監控)----------
    news_flags = ctx.get("news_risk_flags") or []
    if news_flags:
        metrics.append(metric(
            "news_risk_flags", "新聞揭露之風險事件(僅涵蓋有報導者)", "、".join(news_flags),
            rating="bad",
            formula="近期新聞 AI 分類標記的風險片語(非完整監控,詳見 missing_reason)",
            source=_SRC_NEWS, asof=ctx.get("today_str", ""), updated_at=updated,
        ))
    else:
        metrics.append(missing_metric(
            "news_risk_flags", "新聞揭露之風險事件(僅涵蓋有報導者)",
            source=_SRC_NEWS, reason="not_applicable",
        ))
    for tier2_key, tier2_label in (
        ("pledge_ratio", "董監質押比例"), ("default_event", "重大違約"),
        ("capital_reduction", "重大減資"), ("restatement", "財報重編"),
    ):
        metrics.append(missing_metric(
            tier2_key, tier2_label, source="(目前無確認可行的免費結構化資料源,僅能靠上方新聞最佳努力涵蓋)",
            reason="api_unavailable",
        ))

    # ---------- 加總:分數越高越安全 ----------
    penalty_total = sum(r["penalty"] for r in rules if r["hit"])
    risk_score = clip01((100 - penalty_total) / 100) * 100
    any_critical = any(r["hit"] and r["critical"] for r in rules)
    if any_critical:
        risk_score = min(risk_score, 35.0)
        level = "Critical"
    elif risk_score >= 80:
        level = "Low"
    elif risk_score >= 60:
        level = "Medium"
    elif risk_score >= 40:
        level = "High"
    else:
        level = "Critical"

    hit_rules = [r["label"] for r in rules if r["hit"]]
    notes = []
    if hit_rules:
        notes.append("命中風險規則:" + "、".join(hit_rules))
    if any_critical:
        notes.append("⚠ 命中 Critical 組合規則,總分已強制封頂,不被其他面向稀釋。")
    notes.append("董監質押/重大違約/重大減資/財報重編四項目前無免費結構化資料源,僅靠新聞最佳努力涵蓋,非完整監控。")

    result = engine_result(risk_score, metrics, notes=notes)
    result["level"] = level
    result["hit_rules"] = hit_rules
    return result
