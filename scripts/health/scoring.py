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


# ── Swing Score 的橫斷面校準表 ───────────────────────────────────────────────
# 舊版用固定上限(日均成交額 300 百萬 / ATR 6%)把兩個因子壓成 0~1,但台股的成交額分布
# 橫跨近四個數量級 —— 2026-08-14 實測 1,977 檔:P50 僅 14.1 百萬,P99 已達 11,251 百萬。
# 結果是 16.5% 的股票流動性頂天、18.5% 的股票波動度頂天,凡是「夠大又夠活潑」的標的
# 一律得 100 分:2303/2337/2344/2449 四檔的當沖與隔日沖分數全部是 100,零鑑別力。
#
# 改成「這檔在全市場排第幾百分位」——天然落在 0~100、不會飽和、也不必再猜門檻。
# 校準表 = 2026-08-14 全市場橫斷面的分位數斷點,線性內插。
# 分布形狀以月為單位變動很慢,建議每半年用 tools/refresh_swing_calibration.py 重算一次。
_DOLLAR_VOL_PCTL = [(0.6, 10), (2.3, 25), (14.1, 50), (107.5, 75),
                    (734.7, 90), (2180.9, 95), (11250.7, 99)]
_ATR_PCT_PCTL = [(1.59, 10), (2.36, 25), (3.71, 50), (5.37, 75),
                 (6.88, 90), (7.59, 95), (9.25, 99)]


def _percentile_of(value: float, table: list[tuple[float, float]]) -> float:
    """value 落在校準表的第幾百分位(線性內插)。低於首個斷點 → 由 0 起算;高於末端 → 逼近 100。"""
    if value <= table[0][0]:
        return table[0][1] * (value / table[0][0]) if table[0][0] > 0 else 0.0
    for (x0, p0), (x1, p1) in zip(table, table[1:]):
        if value <= x1:
            return p0 + (p1 - p0) * (value - x0) / (x1 - x0)
    x_last, p_last = table[-1]
    # 末端之上用對數收斂到 100,避免超大型股全部並列同分。
    import math
    return min(100.0, p_last + (100 - p_last) * min(1.0, math.log10(value / x_last)))


def swing_scores(technical_metrics: list[dict] | None, chip_metrics: list[dict] | None,
                 financial_score: float | None, technical_score: float | None) -> dict:
    """短線評估(Swing Score):當沖/隔日沖/波段/中長線適合度,0~100。
    純規則組合既有 Engine 已算好的指標(ATR波動度、日均成交額、技術面/財務面分數),不是新邏輯,
    也不重新呼叫任何 API。流動性/波動度兩個因子是**全市場百分位**,不是絕對值(見上方校準表)。"""
    atr_pct = _find_metric_value(technical_metrics, "atr_pct")
    dollar_vol = _find_metric_value(chip_metrics, "dollar_volume")  # 百萬元

    out: dict = {}
    reasons: dict = {}
    liq_pct = _percentile_of(dollar_vol, _DOLLAR_VOL_PCTL) if dollar_vol is not None else None
    if atr_pct is not None and dollar_vol is not None:
        vol_pct = _percentile_of(atr_pct, _ATR_PCT_PCTL)
        liq_factor, vol_factor = liq_pct / 100, vol_pct / 100
        out["day_trade"] = round(clip01(vol_factor * 0.6 + liq_factor * 0.4) * 100, 0)
        out["overnight"] = round(clip01(vol_factor * 0.45 + liq_factor * 0.55) * 100, 0)
        reasons["day_trade"] = (f"ATR波動度 {atr_pct}%(全市場第 {vol_pct:.0f} 百分位)、"
                                f"日均成交額 {dollar_vol} 百萬元(第 {liq_pct:.0f} 百分位)")
        reasons["overnight"] = reasons["day_trade"]
        if dollar_vol < 30:
            reasons["day_trade"] += "(流動性偏低,當沖滑價風險高)"
    else:
        out["day_trade"] = None
        out["overnight"] = None

    if technical_score is not None:
        # 舊版的 liq_factor2 上限只有 200 百萬,20% 的股票頂天 → 那 30% 權重實際是固定送 30 分地板,
        # 「流動性納入30%權重」這句話在實務上是假的。改用同一套百分位後才真的會依流動性拉開。
        liq_factor2 = (liq_pct / 100) if liq_pct is not None else 0.0
        out["swing"] = round(clip01(technical_score / 100 * 0.7 + liq_factor2 * 0.3) * 100, 0)
        reasons["swing"] = (f"技術面分數 {technical_score} 佔 70%、"
                            + (f"流動性全市場第 {liq_pct:.0f} 百分位佔 30%" if liq_pct is not None
                               else "流動性資料不足,該 30% 以 0 計"))
    else:
        out["swing"] = None

    out["long_term"] = round(financial_score, 0) if financial_score is not None else None
    if financial_score is not None:
        reasons["long_term"] = f"財務體質分數 {round(financial_score,0)}(中長線以基本面為主,不看短線波動)"

    out["reasons"] = reasons
    return out
