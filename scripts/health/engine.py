"""個股健檢 Orchestrator — 單一入口,批次路徑(scripts/main.py)與即時路徑(Vercel api/health.py)
都呼叫這裡的 compute_stock_health(ctx),確保兩條路徑共用同一套財務公式(單一事實來源,
不會出現「批次一套、即時一套」的不一致)。

可擴充性:新增 Engine 只要 1) 寫一個 compute(ctx)->engine_result 的新檔案
2) 在下面 ENGINES 註冊 3) 在 config/screeners.yaml 的 health.weights.* 補一欄權重
(預設0=不影響現有總分)。不必改其他 Engine、不必改 Final Scoring Engine、
不必改前端渲染邏輯(前端對回傳的 engines 陣列泛型渲染)。
"""
from __future__ import annotations
from datetime import date, timedelta

from . import financial_engine, growth_engine, value_engine, risk_engine
from . import technical_engine, chip_engine, news_engine
from . import ai_summary
from . import industry_benchmark as ib
from . import scoring as health_scoring
from . import metric
from ..utils import log

ENGINES = {
    "financial": financial_engine,
    "growth": growth_engine,
    "value": value_engine,
    "risk": risk_engine,
    "technical": technical_engine,
    "chip": chip_engine,
    "news": news_engine,
}
ENGINE_LABELS = {
    "financial": "財務體質", "growth": "成長能力", "value": "估值分析", "risk": "風險分析",
    "technical": "技術面", "chip": "籌碼分析", "news": "新聞分析",
}


def compute_stock_health(ctx: dict, *, styles: list[str] | None = None, health_cfg: dict | None = None) -> dict:
    """跑全部 Engine + Final Scoring(各投資風格)+ AI 摘要,回傳可直接 json.dump 的完整結構。
    ctx 需求欄位見各 Engine docstring;批次路徑建議用下面的 build_ctx_batch() 組裝。
    單一 Engine 例外不應讓整檔健檢失敗 —— 失敗的 Engine 退化成 score=None + notes 記錄原因。
    """
    health_cfg = health_cfg or {}
    styles = styles or list(health_scoring.DEFAULT_WEIGHTS.keys())

    engine_results: dict[str, dict] = {}
    for key, module in ENGINES.items():
        try:
            engine_results[key] = module.compute(ctx)
        except Exception as e:
            log.warning(f"health engine '{key}' 計算失敗({ctx.get('stock_id')}):{e}")
            engine_results[key] = {"score": None, "metrics": [], "notes": [f"Engine 計算時發生例外:{e}"]}

    risk_result = engine_results.get("risk") or {}
    risk_level = risk_result.get("level")
    engine_scores = {k: engine_results.get(k, {}).get("score") for k in health_scoring.ENGINE_KEYS}

    # 指標層級覆蓋率:每個 Engine 內部「有值指標數 ÷ 應有指標數」,附回 engine dict 供前端顯示,
    # 並據以算「誠實」的整體資料覆蓋率(見下 data_coverage_pct),取代只看 Engine 有無分數的 covered_weight_pct。
    for key in ENGINES:
        engine_results[key]["coverage"] = metric.metric_coverage(engine_results[key].get("metrics"))
    cov_pct = {k: (engine_results.get(k, {}).get("coverage") or {}).get("pct", 0.0) for k in health_scoring.ENGINE_KEYS}

    scores_by_style: dict[str, dict] = {}
    for style in styles:
        weights = health_scoring.get_weights(style, health_cfg)
        fs = health_scoring.compute_final_score(engine_scores, weights)
        capped = health_scoring.apply_risk_cap(fs["total"], risk_level, health_cfg.get("risk_cap"))
        diag = health_scoring.diagnosis(capped["total"], risk_level)
        # 誠實的整體資料覆蓋率 = Σ(面向權重 × 該面向指標覆蓋率) ÷ 全權重。
        # 面向有分數但內部指標半缺 → 這個數字會明顯低於 covered_weight_pct,讓使用者看到真實證據密度。
        full_w = sum(weights.get(k, 0) for k in health_scoring.ENGINE_KEYS)
        data_cov = (sum(weights.get(k, 0) * cov_pct.get(k, 0.0) / 100.0
                        for k in health_scoring.ENGINE_KEYS) / full_w * 100.0) if full_w else 0.0
        scores_by_style[style] = {
            "label": health_scoring.STYLE_LABELS.get(style, style),
            "weights": weights, "raw_total": fs["total"], "total": capped["total"],
            "capped": capped["capped"], "cap_reason": capped["cap_reason"],
            "covered_weight_pct": fs["covered_weight_pct"], "missing_engines": fs["missing_engines"],
            "data_coverage_pct": round(data_cov, 1),
            "breakdown": fs["breakdown"], "diagnosis": diag,
        }

    swing = health_scoring.swing_scores(
        engine_results.get("technical", {}).get("metrics"),
        engine_results.get("chip", {}).get("metrics"),
        engine_results.get("financial", {}).get("score"),
        engine_results.get("technical", {}).get("score"),
    )
    summary = ai_summary.summarize(engine_results, risk_result, cfg=health_cfg.get("ai_summary"))

    return {
        "stock_id": ctx.get("stock_id"), "name": ctx.get("name"), "industry": ctx.get("industry"),
        "updated_at": ctx.get("updated_at"), "live": bool(ctx.get("live", False)),
        "engines": [{"key": k, "label": ENGINE_LABELS.get(k, k), **engine_results[k]} for k in ENGINES],
        "risk_level": risk_level,
        "scores": scores_by_style,
        "default_style": styles[0] if styles else None,
        "swing": swing,
        "ai_summary": summary,
        "dcf": engine_results.get("value", {}).get("dcf"),
    }


def _stale(df, fresh_days: int = 80) -> bool:
    if df is None or df.empty:
        return True
    try:
        return (date.today() - df.index.max().date()).days > fresh_days
    except Exception:
        return True


def build_ctx_batch(*, stock_id: str, name: str, industry: str | None, today: date,
                    price_df, valuation_snapshot: dict | None = None, current_price: float | None = None,
                    revenue_df=None, chips_df=None, news_items: list[dict] | None = None,
                    news_analysis_precomputed: dict | None = None, health_cfg: dict | None = None) -> dict:
    """批次路徑(scripts/main.py daily_run)用:健檢需要比既有 stage-2 enrichment 更長的歷史窗
    (20季財報供5年CAGR、長窗PE/PB供歷史百分位、持股分散表供大戶/股東人數),這裡統一補抓+組裝,
    沿用既有 storage 快取機制(新鮮 + 已有足夠長度就不重打 FinMind)。"""
    from ..storage import (load_financials, upsert_financials, load_balance, upsert_balance,
                           load_cashflow, upsert_cashflow, load_per, upsert_per, upsert_chips)
    from ..fetchers import (fetch_financial_statements, fetch_balance_sheet, fetch_cashflow,
                            fetch_per_yield, fetch_holder_distribution, fetch_chips_history)

    health_cfg = health_cfg or {}
    quarters = int(health_cfg.get("quarters", 20))

    fin = load_financials(stock_id)
    if _stale(fin) or len(fin) < quarters:
        new = fetch_financial_statements(stock_id, quarters=quarters)
        if not new.empty:
            fin = upsert_financials(stock_id, new)
    bal = load_balance(stock_id)
    if _stale(bal) or len(bal) < quarters:
        new = fetch_balance_sheet(stock_id, quarters=quarters)
        if not new.empty:
            bal = upsert_balance(stock_id, new)
    cf = load_cashflow(stock_id)
    if _stale(cf) or len(cf) < quarters:
        new = fetch_cashflow(stock_id, quarters=quarters)
        if not new.empty:
            cf = upsert_cashflow(stock_id, new)

    per_hist = load_per(stock_id)
    per_days = int(health_cfg.get("per_history_days", 1825))
    if per_hist.empty or _stale(per_hist, fresh_days=3):
        new = fetch_per_yield(stock_id, days=per_days)
        if not new.empty:
            per_hist = upsert_per(stock_id, new)

    # 籌碼:即時路徑(Vercel)無持久 parquet 快取,load_chips 恆空 → 籌碼分析永遠「資料不足」。
    # 批次路徑則可能有快取但當日尚未更新。統一在此:過期/太短就現抓一段窗回補
    # (chip_engine 需近21個交易日供外資持股趨勢/融資5日變化,抓 ~120 個日曆日確保足量)。
    chips_days = int(health_cfg.get("chips_days", 120))
    if _stale(chips_df) or (chips_df is not None and len(chips_df) < 21):
        try:
            new_chips = fetch_chips_history(stock_id, today - timedelta(days=chips_days), today)
            if not new_chips.empty:
                # upsert_chips 的寫檔已有唯讀檔案系統防護(serverless 寫入失敗只記警告);
                # 回傳合併後 DataFrame 供本次健檢直接使用,不必再 load。
                chips_df = upsert_chips(stock_id, new_chips)
        except Exception as e:
            log.warning(f"chips fetch {stock_id} 失敗(續用既有/空):{e}")

    try:
        holder_dist = fetch_holder_distribution(stock_id, today - timedelta(days=400), today)
    except Exception as e:
        log.warning(f"holder_distribution {stock_id} 失敗:{e}")
        holder_dist = None

    industry_benchmarks = ib.lookup(industry)

    news_analysis = news_analysis_precomputed
    if news_analysis is None and news_items:
        news_analysis = news_engine.analyze_news(stock_id, name, news_items, cfg=health_cfg.get("news"))

    return {
        "stock_id": stock_id, "name": name, "industry": industry, "today": today,
        "today_str": today.isoformat(), "updated_at": today.isoformat(),
        "price_df": price_df, "current_price": current_price,
        "financials": fin, "balance": bal, "cashflow": cf, "revenue": revenue_df,
        "per_hist": per_hist, "valuation_snapshot": valuation_snapshot,
        "chips": chips_df, "holder_dist": holder_dist,
        "news_items": news_items or [], "news_analysis": news_analysis,
        "news_risk_flags": (news_analysis or {}).get("risk_flags", []),
        "industry_benchmarks": industry_benchmarks,
        "live": False,
    }
