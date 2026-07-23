"""可解釋性資料契約(Metric / EngineResult)— 個股健檢所有 Engine 共用的最小介面。

設計原則(對應 specs 的「可解釋」要求):每個數字都要能回推依據,所以一個 Metric
除了 value 之外,永遠帶 formula(怎麼算的)、source(從哪抓的)、asof(資料本身的
時間點)、updated_at(系統算這次的時間)。缺資料時用 missing_metric() 誠實地說
「為什麼缺」,不是沉默留白或偷塞 0。

不用 dataclass/pydantic:這包資料最終都要進 JSON(docs/health/*.json),用簡單的
dict builder 最省事,也方便 main.py 既有的 _json_safe(NaN→null)直接吃。
"""
from __future__ import annotations
import math
from typing import Any


def _clean(x: Any) -> Any:
    """NaN/Inf → None,其餘原樣回傳。比照 main.py _json_safe 的精神,Metric 層先做一次,
    避免任何一個 Engine 忘記處理 NaN 就外洩到 JSON。"""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def safe_round(x: Any, n: int = 2) -> Any:
    x = _clean(x)
    if x is None:
        return None
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def trend_point(period: str, value: Any) -> dict:
    return {"period": str(period), "value": _clean(safe_round(value, 4))}


def status_from_delta(latest: float | None, prev: float | None, *,
                       higher_is_better: bool = True, tol: float = 1e-9) -> str | None:
    """依最新值 vs 前一期判斷 improving/worsening/stable。資料不足回 None(不是 stable)。"""
    if latest is None or prev is None:
        return None
    try:
        latest = float(latest); prev = float(prev)
    except (TypeError, ValueError):
        return None
    if math.isnan(latest) or math.isnan(prev):
        return None
    diff = latest - prev
    if abs(diff) <= tol:
        return "stable"
    improving = diff > 0 if higher_is_better else diff < 0
    return "improving" if improving else "worsening"


def rating_from_thresholds(value: float | None, good: float, bad: float, *,
                            higher_is_better: bool = True) -> str | None:
    """value 落在 good/bad 門檻的哪一側 → good/neutral/bad。資料不足回 None。"""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    if higher_is_better:
        if value >= good:
            return "good"
        if value <= bad:
            return "bad"
        return "neutral"
    else:
        if value <= good:
            return "good"
        if value >= bad:
            return "bad"
        return "neutral"


def metric(key: str, label: str, value: Any, *, unit: str = "",
           trend: list[dict] | None = None, industry_avg: Any = None,
           status: str | None = None, rating: str | None = None,
           formula: str = "", source: str = "", asof: str = "",
           updated_at: str = "", missing_reason: str | None = None) -> dict:
    """組一個符合契約的 Metric dict。value 為 None 時自動補 missing_reason='not_applicable'
    (除非呼叫端已指定別的原因),避免「看起來有資料其實是 None」的曖昧狀態。"""
    v = _clean(value)
    if v is None and missing_reason is None:
        missing_reason = "not_applicable"
    return {
        "key": key, "label": label, "value": v, "unit": unit,
        "trend": trend or [],
        "industry_avg": _clean(industry_avg),
        "status": status, "rating": rating,
        "formula": formula, "source": source, "asof": asof, "updated_at": updated_at,
        "missing_reason": missing_reason,
    }


def missing_metric(key: str, label: str, *, reason: str = "api_unavailable",
                    formula: str = "", source: str = "") -> dict:
    """明確標示「這項本來該有,但這次缺資料」,前端統一渲染成「資料不足」而非空白。"""
    return metric(key, label, None, formula=formula, source=source, missing_reason=reason)


def engine_result(score: float | None, metrics: list[dict], *, notes: list[str] | None = None) -> dict:
    """每個 Engine 的標準回傳格式。score=None 代表「該面向資料不足,不計入總分」
    (沿用 fundamentals.fundamental_score() 既有「缺資料就跳過,不是給0」的精神)。"""
    return {
        "score": safe_round(score, 1) if score is not None else None,
        "metrics": metrics,
        "notes": notes or [],
    }


def metric_coverage(metrics: list[dict] | None) -> dict:
    """一個 Engine 內部的「指標層級」覆蓋率 = 有值的指標數 ÷ 應有的指標數。
    誠實度重點:面向層級的 covered_weight_pct 只要 Engine 有分數就算 100%,會把
    「Engine 裡一半指標其實是資料不足」這件事藏起來。這裡回真正的證據密度。

    分母排除 missing_reason=='not_applicable'(該指標本就不適用,如成長為負時的 PEG),
    但保留 api_unavailable / stale_cache 等「本該有卻缺」的(如籌碼大戶比、Tier2 風險旗標)
    —— 這些正是要誠實讓使用者看到的缺口。"""
    present = 0
    total = 0
    for m in (metrics or []):
        has_value = m.get("value") is not None
        if not has_value and m.get("missing_reason") == "not_applicable":
            continue
        total += 1
        if has_value:
            present += 1
    pct = round(present / total * 100, 0) if total else 100.0
    return {"present": present, "total": total, "pct": pct}


def avg_score(parts: list[float]) -> float | None:
    """多個 0~1 子分平均成一個 0~1 分;沒有任何子分回 None(全缺資料)。"""
    parts = [p for p in parts if p is not None and not (isinstance(p, float) and math.isnan(p))]
    if not parts:
        return None
    return sum(parts) / len(parts)
