from __future__ import annotations
import logging
import time
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("twse")

UA = "Mozilla/5.0 (compatible; twse-screener/0.1)"


def http_get_json(url: str, params: dict | None = None, retries: int = 3, delay: float = 3.0):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log.warning(f"http_get_json {url} fail {i+1}/{retries}: {e}")
            time.sleep(delay * (i + 1))
    raise RuntimeError(f"http_get_json failed: {last}")


def chunked(seq, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def extract_json(text: str) -> dict:
    """從 LLM 回覆抽出 JSON 物件(容忍 ```json 圍欄或前後雜訊)。失敗回 {}。

    健檢/催化劑改用「純 messages.create + 自己 parse JSON」而非 output_config 結構化輸出
    (2026-07-09):實測 Vercel 上安裝的 anthropic 版本對 `output_config` 參數會丟例外 →
    analyze_news/classify_catalysts 靜默回 None → 面向恆「資料不足」。OCR/ai_summary 用純
    呼叫都正常,故統一改回純呼叫,用這支穩健地抽 JSON。"""
    if not text:
        return {}
    import json
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return {}
    return {}
