"""新聞事件 AI 分析(催化劑偵測)— stage-2,只對核心候選算。

把近 ~30 天的新聞標題丟給 Claude Haiku,結構化分類成固定催化劑類別(強制 json_schema,
每個催化劑必附 evidence 引用實際標題 → 防幻覺)。只做「分類」不做「漲跌預測」,當軟訊號。

沒有 ANTHROPIC_API_KEY、未安裝 anthropic、或任何呼叫錯誤 → 一律回 None,不影響其餘流程。
成本:Haiku 4.5($1/$5 每百萬 token),每天約 15 檔 × ~2K token ≈ 數美分。
"""
from __future__ import annotations

from .config import ANTHROPIC_API_KEY
from .utils import log

# 使用者指定的催化劑類別(LLM 只能從這個 enum 選)
CATALYST_TYPES = [
    "新訂單", "擴產", "法說會利多", "AI概念", "黃仁勳概念股",
    "蘋果供應鏈", "NVIDIA供應鏈", "Google供應鏈", "政策受惠", "匯率受惠", "產業轉機",
]

# 對短線的相對權重(可由 config 覆寫);題材/訂單/法說 > 一般概念
_DEFAULT_WEIGHTS = {
    "新訂單": 1.2, "擴產": 1.0, "法說會利多": 1.2, "AI概念": 1.0, "黃仁勳概念股": 1.1,
    "蘋果供應鏈": 1.0, "NVIDIA供應鏈": 1.1, "Google供應鏈": 1.0,
    "政策受惠": 0.9, "匯率受惠": 0.8, "產業轉機": 1.0,
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "catalysts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": CATALYST_TYPES},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["type", "confidence", "evidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["catalysts", "summary", "risk_flags"],
    "additionalProperties": False,
}

_SYSTEM = (
    "你是台股短線分析助理。我會給你某檔股票近一個月的新聞標題清單。"
    "請只根據『實際出現在標題裡的內容』判斷是否出現下列催化劑類別,"
    "每個判定的催化劑都必須在 evidence 欄『原文引用』觸發它的那則標題;"
    "找不到明確根據就不要列出該類別(寧缺勿濫)。confidence 0~1。"
    "summary 用一句繁體中文摘要;risk_flags 列出標題中的負面/風險訊號(如訴訟、下修、利空),沒有就空陣列。"
    "只做分類,不要預測漲跌。"
)


def _clip01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0


def classify_catalysts(stock_id: str, name: str, news_items: list[dict],
                       cfg: dict | None = None) -> dict | None:
    """回傳 {catalysts:[{type,confidence,evidence}], summary, risk_flags} 或 None(降級)。"""
    cfg = cfg or {}
    if not ANTHROPIC_API_KEY or not news_items:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("未安裝 anthropic 套件,略過新聞催化劑分析。")
        return None

    max_news = int(cfg.get("max_news", 25))
    titles = []
    for n in news_items[:max_news]:
        t = (n.get("title") or "").strip()
        if t:
            pub = (n.get("published") or "")[:16]
            titles.append(f"- {t}{(' ('+pub+')') if pub else ''}")
    if not titles:
        return None
    user = f"股票:{stock_id} {name}\n近一個月新聞標題:\n" + "\n".join(titles)

    model = cfg.get("model", "claude-haiku-4-5")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=model,
            max_tokens=int(cfg.get("max_tokens", 800)),
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except Exception as e:
        log.warning(f"催化劑分析 {stock_id} 失敗:{e}")
        return None

    import json
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    try:
        data = json.loads(text)
    except Exception:
        return None
    # 清洗:只留合法 enum + 夾 confidence
    cats = []
    for c in (data.get("catalysts") or []):
        t = c.get("type")
        if t in CATALYST_TYPES and c.get("evidence"):
            cats.append({"type": t, "confidence": round(_clip01(c.get("confidence")), 2),
                         "evidence": str(c.get("evidence"))[:120]})
    return {"catalysts": cats,
            "summary": str(data.get("summary", ""))[:200],
            "risk_flags": [str(x)[:40] for x in (data.get("risk_flags") or [])][:5]}


def catalyst_score(catalysts: list[dict] | None, cfg: dict | None = None) -> float | None:
    """催化劑 0~1 分:Σ(confidence × 類別權重),除以 full 後夾 [0,1]。無催化劑回 0,無資料(None)回 None。"""
    if catalysts is None:
        return None
    cfg = cfg or {}
    weights = {**_DEFAULT_WEIGHTS, **(cfg.get("weights") or {})}
    full = float(cfg.get("full", 1.5))
    total = sum(_clip01(c.get("confidence")) * float(weights.get(c.get("type"), 1.0)) for c in catalysts)
    return _clip01(total / full) if full else 0.0
