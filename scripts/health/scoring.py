"""Final Scoring Engine — 個股健檢面向九:可切換投資風格權重的總分,不是平均。

design 原則(對應 specs):
- 任一面向 score=None(資料不足)→ 從加權分母剔除、等比例重分配剩餘權重,不是偷偷補0或補中性值。
- Risk Engine 命中 Critical → 總分強制封頂,不被其他面向稀釋(唯一不走加權平均的例外)。
- 星等診斷是純規則對照表,不是 LLM 生成。
- 三組預設權重可在 config/screeners.yaml 的 health.weights.<style> 覆寫;這裡的 DEFAULT_WEIGHTS
  只在 config 沒填時當 fallback,確保不設 config 也能跑(沿用 scoring.py compute_conviction 的 _g() 慣例)。
"""
from __future__ import annotations

from .metric import clip01

ENGINE_KEYS = ("financial", "growth", "value", "risk", "technical", "chip", "news")

DEFAULT_WEIGHTS = {
    "value_investing":  {"financial": 30, "growth": 10, "value": 30, "risk": 20, "technical": 5, "chip": 0, "news": 5},
    "growth_investing": {"financial": 20, "growth": 30, "value": 10, "risk": 15, "technical": 15, "chip": 5, "news": 5},
    "short_term":        {"financial": 5, "growth": 5, "value": 0, "risk": 15, "technical": 35, "chip": 25, "news": 15},
}
STYLE_LABELS = {"value_investing": "價值投資", "growth_investing": "成長投資", "short_term": "短線交易"}

DEFAULT_RISK_CAP = {"Critical": 40.0}


def get_weights(style: str, cfg: dict | None = None) -> dict[str, float]:
    cfg = cfg or {}
    configured = ((cfg.get("weights") or {}).get(style))
    if configured:
        return {k: float(configured.get(k, 0)) for k in ENGINE_KEYS}
    return dict(DEFAULT_WEIGHTS.get(style, DEFAULT_WEIGHTS["value_investing"]))


def compute_final_score(engine_scores: dict[str, float | None], weights: dict[str, float]) -> dict:
    """engine_scores 的 risk 鍵已是「分數越高越安全」方向,跟其他面向同向,可直接加權混算。
    回傳總分(None=完全無可用面向)、涵蓋權重佔比、缺漏面向清單、逐面向貢獻明細(供前端攤開公式)。
    """
    covered = {k: v for k, v in engine_scores.items() if v is not None and weights.get(k, 0) > 0}
    missing = [k for k in ENGINE_KEYS if engine_scores.get(k) is None and weights.get(k, 0) > 0]
    full_weight = sum(weights.get(k, 0) for k in ENGINE_KEYS)
    covered_weight = sum(weights.get(k, 0) for k in covered)
    if covered_weight <= 0:
        return {"total": None, "covered_weight_pct": 0.0, "missing_engines": missing, "breakdown": []}

    breakdown = []
    weighted_sum = 0.0
    for k in ENGINE_KEYS:
        if k not in covered:
            continue
        w = weights.get(k, 0)
        v = covered[k]
        contrib = v * w / covered_weight
        weighted_sum += contrib
        breakdown.append({
            "engine": k, "score": round(v, 1), "weight": w,
            "effective_weight_pct": round(w / covered_weight * 100, 1),
            "contribution": round(contrib, 1),
        })
    return {
        "total": round(weighted_sum, 1),
        "covered_weight_pct": round(covered_weight / full_weight * 100, 1) if full_weight else 0.0,
        "missing_engines": missing,
        "breakdown": breakdown,
    }


def apply_risk_cap(total: float | None, risk_level: str | None,
                   cap_table: dict[str, float] | None = None) -> dict:
    """Risk=Critical(可擴充其他等級)→ 總分強制封頂。回傳 {total, capped, cap_reason}。"""
    cap_table = cap_table or DEFAULT_RISK_CAP
    if total is None:
        return {"total": None, "capped": False, "cap_reason": None}
    cap = cap_table.get(risk_level) if risk_level else None
    if cap is not None and total > cap:
        return {"total": cap, "capped": True,
                "cap_reason": f"Risk 等級為 {risk_level},總分強制封頂於 {cap} 分,不被其他面向稀釋。"}
    return {"total": total, "capped": False, "cap_reason": None}


def diagnosis(total: float | None, risk_level: str | None) -> dict:
    """星等 + 文字診斷,純規則對照表(對應 specs:不是 LLM 生成)。"""
    if total is None:
        return {"stars": 0, "label": "資料不足", "reason": "可用 Engine 數量不足,暫無法形成總分"}
    if risk_level == "Critical" or total < 40:
        reason = f"總分 {total} 分" + (",且 Risk 等級為 Critical" if risk_level == "Critical" else "")
        return {"stars": 1, "label": "避免投資", "reason": reason}
    if risk_level == "High" or total < 60:
        reason = f"總分 {total} 分" + (",且 Risk 等級為 High" if risk_level == "High" else "")
        return {"stars": 2, "label": "高風險", "reason": reason}
    if total < 75:
        return {"stars": 3, "label": "觀望", "reason": f"總分 {total} 分"}
    if total < 90:
        return {"stars": 4, "label": "可持續追蹤", "reason": f"總分 {total} 分,且風險可控"}
    return {"stars": 5, "label": "優質標的", "reason": f"總分 {total} 分,且風險可控"}


def _find_metric_value(metrics: list[dict] | None, key: str):
    if not metrics:
        return None
    for m in metrics:
        if m.get("key") == key:
            return m.get("value")
    return None


def swing_scores(technical_metrics: list[dict] | None, chip_metrics: list[dict] | None,
                 financial_score: float | None, technical_score: float | None) -> dict:
    """短線評估(Swing Score):當沖/隔日沖/波段/中長線適合度,0~100。
    純規則組合既有 Engine 已算好的指標(ATR波動度、日均成交額、技術面/財務面分數),不是新邏輯,
    也不重新呼叫任何 API。"""
    atr_pct = _find_metric_value(technical_metrics, "atr_pct")
    dollar_vol = _find_metric_value(chip_metrics, "dollar_volume")  # 百萬元

    def clamp(x, lo, hi):
        return max(lo, min(hi, x))

    out: dict = {}
    reasons: dict = {}
    if atr_pct is not None and dollar_vol is not None:
        liq_factor = clamp(dollar_vol, 0, 300) / 300
        vol_factor = clamp(atr_pct, 0, 6) / 6
        out["day_trade"] = round(clip01(vol_factor * 0.6 + liq_factor * 0.4) * 100, 0)
        out["overnight"] = round(clip01(vol_factor * 0.45 + liq_factor * 0.55) * 100, 0)
        reasons["day_trade"] = f"ATR波動度 {atr_pct}%、日均成交額 {dollar_vol} 百萬元"
        reasons["overnight"] = reasons["day_trade"]
        if dollar_vol < 30:
            reasons["day_trade"] += "(流動性偏低,當沖滑價風險高)"
    else:
        out["day_trade"] = None
        out["overnight"] = None

    if technical_score is not None:
        liq_factor2 = clamp(dollar_vol or 0, 0, 200) / 200
        out["swing"] = round(clip01(technical_score / 100 * 0.7 + liq_factor2 * 0.3) * 100, 0)
        reasons["swing"] = f"技術面分數 {technical_score}、流動性納入30%權重"
    else:
        out["swing"] = None

    out["long_term"] = round(financial_score, 0) if financial_score is not None else None
    if financial_score is not None:
        reasons["long_term"] = f"財務體質分數 {round(financial_score,0)}(中長線以基本面為主,不看短線波動)"

    out["reasons"] = reasons
    return out
