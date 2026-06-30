"""AI 解讀(AI Summary)Engine — 個股健檢面向八 + 最終診斷文字。

設計取捨(使用者明確選擇):**規則先產生所有事實句,LLM 只負責把這些句子潤飾得更通順**,
不能新增數字、不能新增結論、不能引入未提供的事實。規則句永遠保留並可切換查看,
LLM 潤飾版只是顯示用的「更通順版」,沒有 ANTHROPIC_API_KEY 時自動降級成純規則句,
不影響其餘功能(沿用 catalyst.py/news_engine.py 既有降級慣例)。
"""
from __future__ import annotations

from ..config import ANTHROPIC_API_KEY
from ..utils import log

_GOOD_RATING = "good"
_BAD_RATING = "bad"


def _fmt_value(m: dict) -> str:
    v = m.get("value")
    if v is None:
        return "無資料"
    unit = m.get("unit") or ""
    if isinstance(v, float):
        v = round(v, 2)
    return f"{v}{unit}"


def _fact_sentence(label: str, m: dict, *, direction: str) -> str:
    val = _fmt_value(m)
    industry = m.get("industry_avg")
    industry_part = f"(同業平均約{industry}{m.get('unit','')})" if industry is not None else ""
    if direction == "improving":
        return f"{label}持續改善,目前{val}{industry_part}"
    if direction == "worsening":
        return f"{label}持續惡化,目前{val}{industry_part}"
    return f"{label}目前{val}{industry_part}"


def build_facts(engine_results: dict[str, dict]) -> list[str]:
    """規則句:掃過所有 Engine 的 metrics,挑 status 明確(improving/worsening)的項目組句。
    100% 規則產生,零成本,零幻覺風險 —— 這是永遠存在的 fallback。"""
    facts: list[str] = []
    for _name, res in (engine_results or {}).items():
        for m in res.get("metrics", []):
            status = m.get("status")
            if status in ("improving", "worsening") and m.get("value") is not None:
                facts.append(_fact_sentence(m["label"], m, direction=status))
    return facts


def pick_pros_cons(engine_results: dict[str, dict], *, per_engine: int = 2) -> tuple[list[str], list[str]]:
    """每個 Engine 最多各取前 N 項 rating=good/bad 的指標組句(依 metrics 原始順序,
    各 Engine 內建構順序已大致依重要性排列;不做額外加權排序,維持規則透明、零黑盒)。"""
    pros: list[str] = []
    cons: list[str] = []
    for _name, res in (engine_results or {}).items():
        g = c = 0
        for m in res.get("metrics", []):
            if m.get("value") is None:
                continue
            if m.get("rating") == _GOOD_RATING and g < per_engine:
                pros.append(_fact_sentence(m["label"], m, direction="neutral"))
                g += 1
            elif m.get("rating") == _BAD_RATING and c < per_engine:
                cons.append(_fact_sentence(m["label"], m, direction="neutral"))
                c += 1
    return pros, cons


def pick_watch_items(engine_results: dict[str, dict]) -> list[str]:
    """待觀察:目前還是 good/neutral 但方向在惡化的早期訊號,值得追蹤但還不到「缺點」程度。"""
    watch: list[str] = []
    for _name, res in (engine_results or {}).items():
        for m in res.get("metrics", []):
            if m.get("status") == "worsening" and m.get("rating") in (None, "neutral", "good") and m.get("value") is not None:
                watch.append(f"{m['label']}近期轉弱({_fmt_value(m)}),雖未達警示門檻,值得持續追蹤。")
    return watch


def build_risks(risk_result: dict | None) -> list[str]:
    if not risk_result:
        return []
    risks = list(risk_result.get("hit_rules") or [])
    risks.append("董監質押/重大違約/重大減資/財報重編四項僅靠新聞最佳努力涵蓋,非完整監控(可能有漏報)。")
    return risks


def rule_narrative(facts: list[str], pros: list[str], cons: list[str]) -> str:
    """零成本規則段落(fallback,永遠存在)。"""
    parts = []
    if pros:
        parts.append("優勢:" + ";".join(pros[:4]) + "。")
    if cons:
        parts.append("待留意:" + ";".join(cons[:4]) + "。")
    if facts:
        recent = [f for f in facts if "改善" in f][:3]
        if recent:
            parts.append("近期趨勢:" + "、".join(recent) + "。")
    return " ".join(parts) if parts else "目前可用資料不足以形成完整摘要。"


def polish(facts: list[str], pros: list[str], cons: list[str], *, cfg: dict | None = None) -> str | None:
    """LLM 只負責把給定的規則句重新組織成通順段落,prompt 強制不能新增數字/結論/未提供的事實。
    沒有 ANTHROPIC_API_KEY 或呼叫失敗 → 回 None(呼叫端 fallback 用 rule_narrative)。"""
    cfg = cfg or {}
    if not ANTHROPIC_API_KEY or not (facts or pros or cons):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("未安裝 anthropic 套件,略過健檢 AI 潤飾,使用規則句。")
        return None

    given = []
    if pros:
        given.append("優點事實句:" + "；".join(pros))
    if cons:
        given.append("缺點事實句:" + "；".join(cons))
    if facts:
        given.append("其他趨勢事實句:" + "；".join(facts[:10]))
    user = "\n".join(given)

    system = (
        "你是台股基本面分析助理。下面是已經由規則計算好的『事實句』,每句都已經是正確的結論。"
        "請把這些事實句重新組織成一段通順的繁體中文摘要(約100~150字),"
        "**絕對不能新增任何數字、不能新增任何給定事實句以外的結論、不能做出買賣建議**,"
        "只能調整語句順序與連接詞讓它讀起來更像一段分析。"
    )
    model = cfg.get("model", "claude-haiku-4-5")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=model, max_tokens=int(cfg.get("max_tokens", 400)),
            system=system, messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        return text.strip()[:400] or None
    except Exception as e:
        log.warning(f"健檢 AI 潤飾失敗:{e}")
        return None


def summarize(engine_results: dict[str, dict], risk_result: dict | None, *, cfg: dict | None = None) -> dict:
    """組裝完整 AI 解讀區塊。永遠回傳規則版(narrative);ai_narrative 視 API key 而定。"""
    facts = build_facts(engine_results)
    pros, cons = pick_pros_cons(engine_results)
    watch = pick_watch_items(engine_results)
    risks = build_risks(risk_result)
    narrative = rule_narrative(facts, pros, cons)
    ai_narrative = polish(facts, pros, cons, cfg=cfg)
    return {
        "facts": facts, "pros": pros, "cons": cons, "risks": risks, "watch": watch,
        "narrative": narrative, "narrative_ai": ai_narrative,
    }
