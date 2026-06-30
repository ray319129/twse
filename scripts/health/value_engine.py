"""估值分析(Valuation)Engine — 個股健檢面向三。

歷史 PE/PB 百分位需要長窗每日 PE/PB 序列:ctx['per_hist'] 來自 fetchers.fetch_per_yield()
(本次健檢開發前是死 import,2026-06-30 起由批次/即時路徑各自用長窗呼叫,見 HANDOFF)。
沒有 per_hist 時退用 ctx['valuation_snapshot'](TWSE/TPEx 估值快照,當下單日,無法算百分位)。

DCF/EV-EBITDA 屬於假設密集型指標,刻意「不」計入 score(避免假精確污染可解釋性),
只附在回傳的 dcf 欄位、所有假設攤開,前端可做成可調滑桿即時重算(純前端公式,不必重打 API)。
"""
from __future__ import annotations
import pandas as pd

from .metric import metric, missing_metric, engine_result, rating_from_thresholds, status_from_delta, avg_score, clip01, safe_round
from . import quarterly as q

_SRC_PER = "FinMind:TaiwanStockPER"
_SRC_SNAP = "TWSE/TPEx 估值快照(BWIBBU)"
_SRC_FS = "FinMind:TaiwanStockFinancialStatements"
_SRC_BS = "FinMind:TaiwanStockBalanceSheet"
_SRC_CF = "FinMind:TaiwanStockCashFlowsStatement"


def _current_pe_pb_yield(ctx: dict) -> tuple[dict, str, str]:
    """回傳 (值dict{pe,pb,yield_pct}, source, asof)。優先用 per_hist 最新一筆,沒有才退估值快照。"""
    per_hist = ctx.get("per_hist")
    if per_hist is not None and not per_hist.empty:
        last_row = per_hist.dropna(how="all").iloc[-1] if not per_hist.dropna(how="all").empty else None
        if last_row is not None:
            asof = str(per_hist.dropna(how="all").index[-1].date())
            return ({k: (float(last_row[k]) if k in last_row and pd.notna(last_row[k]) else None)
                    for k in ("pe", "pb", "yield_pct")}, _SRC_PER, asof)
    snap = ctx.get("valuation_snapshot") or {}
    if snap:
        return ({k: snap.get(k) for k in ("pe", "pb", "yield_pct")}, _SRC_SNAP, ctx.get("today_str", ""))
    return ({}, "", "")


def _percentile(series: pd.Series, value: float) -> float | None:
    s = series.dropna()
    if len(s) < 20 or value is None:
        return None
    return float((s <= value).sum()) / len(s) * 100.0


def compute(ctx: dict) -> dict:
    bal = ctx.get("balance"); fin = ctx.get("financials"); cf = ctx.get("cashflow")
    bench = ctx.get("industry_benchmarks") or {}
    updated = ctx.get("updated_at", "")
    per_hist = ctx.get("per_hist")
    price = ctx.get("current_price")

    cur, src, asof = _current_pe_pb_yield(ctx)
    metrics: list[dict] = []
    cheapness: list[float] = []

    pe, pb, yld = cur.get("pe"), cur.get("pb"), cur.get("yield_pct")

    if pe is not None:
        metrics.append(metric(
            "pe", "本益比(PE)", round(pe, 2),
            industry_avg=bench.get("pe"),
            rating=rating_from_thresholds(pe, 15, 30, higher_is_better=False),
            formula="股價 ÷ 每股盈餘(近四季)", source=src, asof=asof, updated_at=updated,
        ))
        cheapness.append(clip01((30 - pe) / (30 - 15)))
    else:
        metrics.append(missing_metric("pe", "本益比(PE)", source=src or _SRC_PER))

    if pb is not None:
        metrics.append(metric(
            "pb", "股價淨值比(PB)", round(pb, 2),
            industry_avg=bench.get("pb"),
            rating=rating_from_thresholds(pb, 1.5, 4, higher_is_better=False),
            formula="股價 ÷ 每股淨值", source=src, asof=asof, updated_at=updated,
        ))
        cheapness.append(clip01((4 - pb) / (4 - 1.5)))
    else:
        metrics.append(missing_metric("pb", "股價淨值比(PB)", source=src or _SRC_PER))

    if yld is not None:
        metrics.append(metric(
            "dividend_yield", "殖利率", round(yld, 2), unit="%",
            industry_avg=bench.get("dividend_yield"),
            rating=rating_from_thresholds(yld, 4, 1),
            formula="近四季現金股利 ÷ 股價", source=src, asof=asof, updated_at=updated,
        ))
        cheapness.append(clip01(yld / 6))
    else:
        metrics.append(missing_metric("dividend_yield", "殖利率", source=src or _SRC_PER))

    # ---------- 歷史 PE/PB 百分位 ----------
    if per_hist is not None and not per_hist.empty and "pe" in per_hist.columns:
        pe_pct = _percentile(per_hist["pe"], pe)
        if pe_pct is not None:
            span = f"{per_hist.index.min().date()} ~ {per_hist.index.max().date()}"
            metrics.append(metric(
                "pe_percentile", "PE 歷史百分位(自身)", round(pe_pct, 0), unit="%",
                rating=("good" if pe_pct <= 30 else ("bad" if pe_pct >= 70 else "neutral")),
                formula=f"目前 PE 落在自身歷史序列({span})由低到高排序的第幾百分位;越低代表相對自己過去越便宜",
                source=_SRC_PER, asof=asof, updated_at=updated,
            ))
            cheapness.append(clip01((100 - pe_pct) / 100))
        else:
            metrics.append(missing_metric("pe_percentile", "PE 歷史百分位(自身)", source=_SRC_PER,
                                          reason="stale_cache"))
    else:
        metrics.append(missing_metric("pe_percentile", "PE 歷史百分位(自身)", source=_SRC_PER))

    if per_hist is not None and not per_hist.empty and "pb" in per_hist.columns:
        pb_pct = _percentile(per_hist["pb"], pb)
        if pb_pct is not None:
            metrics.append(metric(
                "pb_percentile", "PB 歷史百分位(自身)", round(pb_pct, 0), unit="%",
                rating=("good" if pb_pct <= 30 else ("bad" if pb_pct >= 70 else "neutral")),
                formula="目前 PB 落在自身歷史序列由低到高排序的第幾百分位",
                source=_SRC_PER, asof=asof, updated_at=updated,
            ))
        else:
            metrics.append(missing_metric("pb_percentile", "PB 歷史百分位(自身)", source=_SRC_PER,
                                          reason="stale_cache"))
    else:
        metrics.append(missing_metric("pb_percentile", "PB 歷史百分位(自身)", source=_SRC_PER))

    # ---------- PEG ----------
    eps_yoy = q.yoy(fin, "eps")
    if pe is not None and eps_yoy is not None and eps_yoy > 0:
        peg = pe / (eps_yoy * 100)
        metrics.append(metric(
            "peg", "PEG", round(peg, 2),
            rating=rating_from_thresholds(peg, 1.0, 2.0, higher_is_better=False),
            formula="PE ÷ EPS 年增率(%)", source=f"{src} + {_SRC_FS}", asof=asof, updated_at=updated,
        ))
        cheapness.append(clip01((2.0 - peg) / (2.0 - 1.0)))
    else:
        metrics.append(missing_metric(
            "peg", "PEG", source=f"{src or _SRC_PER} + {_SRC_FS}",
            reason="not_applicable" if (eps_yoy is not None and eps_yoy <= 0) else "api_unavailable",
        ))

    # ---------- EV/EBITDA ----------
    equity = q.last(bal, "equity")
    market_cap = (pb * equity) if (pb is not None and equity is not None) else None
    cash = q.last(bal, "cash"); std = q.last(bal, "short_term_debt"); ltd = q.last(bal, "long_term_debt")
    net_debt = None
    if cash is not None and (std is not None or ltd is not None):
        net_debt = (std or 0) + (ltd or 0) - cash
    oi = q.last(fin, "operating_income"); da = q.last(cf, "depreciation_amortization")
    ebitda = (oi + abs(da)) if (oi is not None and da is not None) else None
    if market_cap is not None and net_debt is not None and ebitda is not None and ebitda > 0:
        ev = market_cap + net_debt
        ev_ebitda = ev / ebitda
        metrics.append(metric(
            "ev_ebitda", "EV/EBITDA", round(ev_ebitda, 2),
            rating=rating_from_thresholds(ev_ebitda, 8, 15, higher_is_better=False),
            formula="(市值[≈PB×權益] + 淨負債[短期+長期借款−現金]) ÷ (營業利益 + 折舊攤銷);市值用 PB×權益近似,非交易所即時市值",
            source=f"{src} + {_SRC_BS} + {_SRC_FS} + {_SRC_CF}", asof=asof, updated_at=updated,
        ))
        cheapness.append(clip01((15 - ev_ebitda) / (15 - 8)))
    else:
        metrics.append(missing_metric("ev_ebitda", "EV/EBITDA", source=f"{_SRC_BS} + {_SRC_FS} + {_SRC_CF}"))

    # ---------- 是否低於合理價(PE均值回歸法,粗略估算)----------
    fair_price = None
    if per_hist is not None and not per_hist.empty and "pe" in per_hist.columns and price is not None:
        pe_mean = float(per_hist["pe"].dropna().mean()) if per_hist["pe"].dropna().shape[0] >= 20 else None
        eps_ttm = None
        if fin is not None and not fin.empty and "eps" in fin.columns:
            tail4 = fin["eps"].dropna().tail(4)
            eps_ttm = float(tail4.sum()) if len(tail4) == 4 else None
        if pe_mean is not None and eps_ttm is not None and eps_ttm > 0:
            fair_price = pe_mean * eps_ttm
            below = price < fair_price
            metrics.append(metric(
                "below_fair_value", "現價是否低於估算合理價", "低於" if below else "高於",
                rating=("good" if below else "neutral"),
                formula=f"合理價估算 = 自身歷史平均PE({pe_mean:.1f}) × 近四季EPS合計({eps_ttm:.2f}) = {fair_price:.1f};現價 {price:.1f}。"
                        "屬粗略的均值回歸估算,非嚴謹估值模型,僅供參考。",
                source=f"{_SRC_PER} + {_SRC_FS}", asof=asof, updated_at=updated,
            ))
            cheapness.append(1.0 if below else 0.2)
        else:
            metrics.append(missing_metric("below_fair_value", "現價是否低於估算合理價", source=f"{_SRC_PER} + {_SRC_FS}"))
    else:
        metrics.append(missing_metric("below_fair_value", "現價是否低於估算合理價", source=f"{_SRC_PER} + {_SRC_FS}"))

    # ---------- DCF(選配,不計分,假設全攤開)----------
    dcf = _compute_dcf(ctx, market_cap, price)

    score = avg_score(cheapness)
    score = score * 100 if score is not None else None
    notes = []
    if per_hist is None or per_hist.empty:
        notes.append("尚無歷史 PE/PB 序列(per_hist),百分位/合理價估算暫缺;僅用單日估值快照算 PE/PB/殖利率。")
    result = engine_result(score, metrics, notes=notes)
    result["dcf"] = dcf
    return result


def _compute_dcf(ctx: dict, market_cap: float | None, price: float | None) -> dict:
    """兩階段 FCF 折現,選配揭露用,不納入 Value Score。所有假設都回傳給前端,
    前端可讓使用者調整 growth_rate/discount_rate/terminal_growth 後純前端重算(公式簡單,不必重打 API)。
    FCF 用 OCF−|capex| 近似 FCFE,不再額外扣淨負債(避免雙重計算,已在公式註記此簡化)。
    """
    cf = ctx.get("cashflow"); fin = ctx.get("financials")
    ocf = q.last(cf, "op_cashflow"); capex = q.last(cf, "capex")
    if ocf is None or capex is None or price is None or market_cap is None or market_cap <= 0:
        return {"available": False, "reason": "缺現金流或市值資料(market_cap 需要 PB×權益,price 需要近收盤價)"}
    fcf_q = ocf - abs(capex)
    fcf_ttm = fcf_q * 4  # 簡化:用最新一季 ×4 年化(若有完整近4季加總會更準,先用最新季年化保持簡單可解釋)
    cf_4 = cf["op_cashflow"].dropna().tail(4) if (cf is not None and "op_cashflow" in cf.columns) else None
    capex_4 = cf["capex"].dropna().tail(4) if (cf is not None and "capex" in cf.columns) else None
    if cf_4 is not None and capex_4 is not None and len(cf_4) == 4 and len(capex_4) == 4:
        fcf_ttm = float(cf_4.sum()) - abs(float(capex_4.sum()))

    growth_rate = q.cagr(fin, "revenue", years=5)
    if growth_rate is None:
        growth_rate = q.yoy(fin, "revenue") or 0.05
    growth_rate = max(-0.10, min(growth_rate, 0.25))
    discount_rate = 0.08
    terminal_growth = 0.02

    shares_outstanding = market_cap / price if price else None
    if not shares_outstanding:
        return {"available": False, "reason": "無法估算流通股數(market_cap ÷ price)"}

    pv_sum = 0.0
    fcf_t = fcf_ttm
    for year in range(1, 6):
        fcf_t = fcf_t * (1 + growth_rate)
        pv_sum += fcf_t / ((1 + discount_rate) ** year)
    terminal_value = fcf_t * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / ((1 + discount_rate) ** 5)
    equity_value = pv_sum + pv_terminal
    fair_value_per_share = equity_value / shares_outstanding

    return {
        "available": True,
        "assumptions": {
            "growth_rate_pct": safe_round(growth_rate * 100, 1),
            "discount_rate_pct": safe_round(discount_rate * 100, 1),
            "terminal_growth_pct": safe_round(terminal_growth * 100, 1),
            "base_fcf_ttm": safe_round(fcf_ttm, 0),
        },
        "fair_value_per_share": safe_round(fair_value_per_share, 1),
        "current_price": safe_round(price, 1),
        "upside_pct": safe_round((fair_value_per_share / price - 1) * 100, 1) if price else None,
        "formula": "兩階段FCF折現:近4季FCF(OCF−|資本支出|)為基準,未來5年以「成長率」複合成長後折現(折現率),"
                   "第6年起以「永續成長率」算終值再折現,加總後除以流通股數(≈市值[PB×權益]÷現價)估算每股合理價。"
                   "growth_rate 預設取5年營收CAGR(無則退用最新YoY,夾在-10%~25%);discount_rate/terminal_growth 為固定估算值,"
                   "三者皆可在前端調整即時重算。屬假設密集型粗估,刻意不計入 Value Score。",
    }
