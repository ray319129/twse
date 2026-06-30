"""新聞分析(News)Engine — 個股健檢面向七。

沿用 scripts/catalyst.py 的呼叫模式(零幻覺:結構化 JSON schema + 每筆判定必附 evidence
原文引用、找不到根據就不要列),但 schema 改成「逐則新聞標記」而非整批摘要 —— 對每則
新聞標 sentiment/durability/impact/confidence,**只呼叫一次 Haiku**,7/30/90 天三個視窗
完全用本地已知的新聞發布日期(fetchers.fetch_news 的 published_date,2026-06-30 新增)分桶,
不需要也不會多打 LLM 三次。

沒有 ANTHROPIC_API_KEY 或沒有新聞 → analyze_news() 回 None,compute() 對應降級成
score=None + 缺資料說明,不影響其餘 Engine(沿用 catalyst.py 既有降級慣例)。
"""
from __future__ import annotations
from datetime import date, datetime, timedelta

from ..config import ANTHROPIC_API_KEY as _ANTHROPIC_API_KEY
from ..utils import log
from .metric import metric, missing_metric, engine_result, clip01

_SRC = "Google News RSS + Claude Haiku(逐則新聞標記,evidence 強制原文引用)"

_SENTIMENT_VALUES = {"利多": 1.0, "利空": -1.0, "中性": 0.0}
_IMPACT_WEIGHTS = {"高": 1.5, "中": 1.0, "低": 0.5}

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "sentiment": {"type": "string", "enum": ["利多", "利空", "中性"]},
                    "durability": {"type": "string", "enum": ["一次性", "長期"]},
                    "impact": {"type": "string", "enum": ["高", "中", "低"]},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["idx", "sentiment", "durability", "impact", "confidence", "evidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "summary", "risk_flags"],
    "additionalProperties": False,
}

_SYSTEM = (
    "你是台股基本面分析助理。我會給你某檔股票近90天的新聞標題清單,每則標題前有編號 idx。"
    "請針對清單中的『每一則』標題逐一判斷:"
    "sentiment(利多/利空/中性,對該公司股價而言)、durability(一次性事件 或 長期趨勢/結構性利多利空)、"
    "impact(高/中/低,對基本面影響程度)、confidence(0~1,你對這個判斷的把握程度)。"
    "evidence 必須是該則標題的原文引用,不可改寫或杜撰。idx 必須對應到輸入清單的編號,"
    "每個 idx 最多輸出一筆。只根據標題字面內容判斷,不要自己腦補沒寫出來的資訊,"
    "也不要預測股價漲跌。summary 用一句繁體中文摘要整體基調;"
    "risk_flags 每則用『簡短片語』(≤30字,如「遭調查」「Q1財測下修」)列出標題中的負面/風險訊號,沒有則空陣列。"
)


def _clip01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0


def analyze_news(stock_id: str, name: str, news_items: list[dict], cfg: dict | None = None) -> dict | None:
    """單次 Haiku 呼叫,逐則新聞標記。回傳 {items:[{idx,sentiment,durability,impact,confidence,evidence}],
    summary, risk_flags} 或 None(降級:無 API key / 無新聞 / 呼叫失敗)。"""
    cfg = cfg or {}
    if not _ANTHROPIC_API_KEY or not news_items:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("未安裝 anthropic 套件,略過健檢新聞分析。")
        return None

    max_news = int(cfg.get("max_news", 60))
    numbered = []
    for i, n in enumerate(news_items[:max_news]):
        t = (n.get("title") or "").strip()
        if not t:
            continue
        pub = n.get("published_date") or (n.get("published") or "")[:16]
        numbered.append(f"{i}. {t}" + (f"({pub})" if pub else ""))
    if not numbered:
        return None
    user = f"股票:{stock_id} {name}\n新聞標題清單:\n" + "\n".join(numbered)

    model = cfg.get("model", "claude-haiku-4-5")
    try:
        client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=model,
            max_tokens=int(cfg.get("max_tokens", 1600)),
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except Exception as e:
        log.warning(f"健檢新聞分析 {stock_id} 失敗:{e}")
        return None

    import json
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    try:
        data = json.loads(text)
    except Exception:
        return None

    items = []
    for it in (data.get("items") or []):
        idx = it.get("idx")
        if not isinstance(idx, int) or not (0 <= idx < len(numbered)) or not it.get("evidence"):
            continue
        if it.get("sentiment") not in _SENTIMENT_VALUES:
            continue
        items.append({
            "idx": idx,
            "sentiment": it["sentiment"],
            "durability": it.get("durability") if it.get("durability") in ("一次性", "長期") else "一次性",
            "impact": it.get("impact") if it.get("impact") in _IMPACT_WEIGHTS else "中",
            "confidence": round(_clip01(it.get("confidence")), 2),
            "evidence": str(it["evidence"])[:120],
        })
    return {
        "items": items,
        "summary": str(data.get("summary", ""))[:200],
        "risk_flags": [str(x)[:80] for x in (data.get("risk_flags") or [])][:5],
    }


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _window_stats(items: list[dict], news_items: list[dict], today: date, days: int) -> dict:
    cutoff = today - timedelta(days=days)
    weighted_sum = 0.0
    weight_total = 0.0
    count = 0
    pos = neg = 0
    for it in items:
        idx = it["idx"]
        if idx >= len(news_items):
            continue
        d = _parse_date(news_items[idx].get("published_date"))
        if d is None or d < cutoff or d > today:
            continue
        count += 1
        sv = _SENTIMENT_VALUES.get(it["sentiment"], 0.0)
        w = _IMPACT_WEIGHTS.get(it["impact"], 1.0) * it["confidence"]
        weighted_sum += sv * w
        weight_total += w
        if sv > 0:
            pos += 1
        elif sv < 0:
            neg += 1
    net_sentiment = (weighted_sum / weight_total) if weight_total > 0 else None
    return {"count": count, "positive": pos, "negative": neg, "net_sentiment": net_sentiment}


def compute(ctx: dict) -> dict:
    analysis = ctx.get("news_analysis")
    news_items = ctx.get("news_items") or []
    updated = ctx.get("updated_at", "")
    today = ctx.get("today") or date.today()

    if not analysis or not analysis.get("items"):
        reason = "not_applicable" if not news_items else "api_unavailable"
        return engine_result(None, [missing_metric(
            "news_coverage", "新聞涵蓋", source=_SRC, reason=reason,
        )], notes=["無 ANTHROPIC_API_KEY、無新聞、或本次 AI 分析失敗,新聞面向不計入總分。"])

    items = analysis["items"]
    metrics: list[dict] = []
    windows = {7: "近7天", 30: "近30天", 90: "近90天"}
    window_weight = {7: 0.5, 30: 0.3, 90: 0.2}
    weighted_score_sum = 0.0
    weighted_score_w = 0.0

    for days, label in windows.items():
        stats = _window_stats(items, news_items, today, days)
        net = stats["net_sentiment"]
        metrics.append(metric(
            f"news_net_sentiment_{days}d", f"{label}新聞淨情緒", round(net, 2) if net is not None else None,
            rating=(None if net is None else ("good" if net > 0.2 else ("bad" if net < -0.2 else "neutral"))),
            formula=f"{label}內每則新聞 sentiment(利多+1/利空−1/中性0)依 impact權重×confidence 加權平均",
            source=_SRC, asof=str(today), updated_at=updated,
            missing_reason=(None if net is not None else "not_applicable"),
        ))
        metrics.append(metric(
            f"news_density_{days}d", f"{label}事件則數", stats["count"], unit="則",
            formula=f"{label}內被標記出明確 sentiment 的新聞則數(利多 {stats['positive']} / 利空 {stats['negative']})",
            source=_SRC, asof=str(today), updated_at=updated,
        ))
        if net is not None:
            weighted_score_sum += net * window_weight[days]
            weighted_score_w += window_weight[days]

    durable_pos = sum(1 for it in items if it["sentiment"] == "利多" and it["durability"] == "長期")
    one_off_pos = sum(1 for it in items if it["sentiment"] == "利多" and it["durability"] == "一次性")
    metrics.append(metric(
        "durable_vs_oneoff", "利多事件:長期 vs 一次性", f"{durable_pos} : {one_off_pos}",
        formula="AI 逐則標記的 durability 欄位統計(僅統計 sentiment=利多 的新聞)",
        source=_SRC, asof=str(today), updated_at=updated,
        rating=("good" if durable_pos > one_off_pos else "neutral"),
    ))

    if analysis.get("summary"):
        metrics.append(metric(
            "ai_news_summary", "AI 新聞摘要(原文佐證見上方逐則標記)", analysis["summary"],
            formula="Haiku 對本次新聞清單的一句話摘要", source=_SRC, asof=str(today), updated_at=updated,
        ))

    score = clip01((weighted_score_sum / weighted_score_w + 1) / 2) * 100 if weighted_score_w > 0 else None
    return engine_result(score, metrics)
