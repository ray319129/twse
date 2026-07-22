"""盤中訊號的個股背景資料(2026-07-21)。

使用者反映:Discord 通知只有代號、價格、觸發理由 —— 「這是誰、在做什麼、
基本面如何、最近有什麼新聞」全都得自己再去查一次。這支就是把那些補上。

## 資料來源與成本(重要)

| 內容 | 來源 | API 成本 |
|---|---|---|
| 產業 / 細產業(在做什麼) | `docs/sector_map.json`(FinMind 產業鏈,盤後批次算好) | **0** |
| 市值 / 股本 / 均線 / 20日高 | `docs/levels.json`(盤前建好) | **0** |
| EPS / 營收 YoY | `levels.json`,缺就現抓 | 0~1 次 |
| 本益比 / 股價淨值比 / 殖利率 | `TaiwanStockPER`(現抓) | 1 次 |
| 近期新聞 | Google News RSS | **0**(不算 FinMind 額度) |
| 日K圖 | 本機 `data/prices/*.parquet` | **0** |

一檔約 1~2 次 FinMind 呼叫,一天 20 檔 = 40 次,對照 6000/hr 可忽略。

## 為什麼要有當日快取

`_CACHE` 以「當天 + 代號」為 key。同一檔可能先觸發突破、稍後又觸發回檔買點,
沒有快取就會把新聞和財報再抓一次。而且 **enrich 是在盯盤迴圈裡同步跑的** ——
每 10 秒一輪,一批 8 檔如果每檔都要等 3 秒新聞,整個輪詢會被卡住。

## 絕不 raise

這整支的任何失敗都只是「通知裡少一段」,不該影響訊號本身。所有對外呼叫都包在
try 裡,失敗回 None,呼叫端自己處理缺欄位。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .config import DATA_DIR, now_tpe
from .utils import log

DOCS = DATA_DIR.parent / "docs"

_CACHE: dict[str, dict] = {}
_STATIC: dict = {"day": "", "levels": None, "sector": None}

NEWS_TIMEOUT = 6.0        # Google News 慢的時候不能拖垮盯盤迴圈
NEWS_MAX = 3
NEWS_DAYS = 14            # 只要「近期」——三個月前的新聞對盤中訊號沒有意義


def _load_static() -> tuple[dict, dict]:
    """levels.json / sector_map.json 一天只變一次,載一次就好。"""
    today = now_tpe().strftime("%Y-%m-%d")
    if _STATIC["day"] != today or _STATIC["levels"] is None:
        lv, sec = {}, {}
        try:
            lv = (json.loads((DOCS / "levels.json").read_text(encoding="utf-8"))
                  .get("levels") or {})
        except Exception as e:
            log.warning(f"enrich: levels.json 讀取失敗:{e}")
        try:
            sec = json.loads((DOCS / "sector_map.json").read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"enrich: sector_map.json 讀取失敗:{e}")
        _STATIC.update({"day": today, "levels": lv, "sector": sec})
    return _STATIC["levels"] or {}, _STATIC["sector"] or {}


def _business(sid: str, sector: dict, lv: dict) -> str:
    """「這家公司在做什麼」。

    FinMind 產業鏈是「一檔對多條」(鴻海同時在筆電/伺服器/連接器/電動車),
    這比 TWSE 那種「一檔一個粗分類」有用得多 —— 台股講的族群就是這個。
    取 primary(最具代表的那條)+ 最多 2 條其他細產業。
    完全查不到就退回 levels 的 TWSE 產業別(KY 外國企業常見)。
    """
    primary = (sector.get("primary") or {}).get(sid)
    chain = (sector.get("chain") or {}).get(sid) or []

    def _short(s: str) -> str:
        """FinMind 有些細產業名稱本身就是一長串列舉,例如鴻海的
        「印表機、傳真機、掃瞄器、多功能事務機、投影機」—— 整串貼進通知會洗掉版面。
        取第一項當代表就夠了(那也是最具代表性的那個)。"""
        s = str(s or "").strip()
        if "、" in s and len(s) > 12:
            s = s.split("、")[0] + "等"
        return s[:16]

    parts = []
    if primary and len(primary) >= 2:
        a, b = _short(primary[0]), _short(primary[1])
        # CMoney 分類是單層(sec==sub==類股)→ 只顯示一次,不要「IC-代工 · IC-代工」
        parts.append(a if a == b else f"{a} · {b}")
    subs = [c[1] for c in chain if len(c) >= 2 and (not primary or c[1] != primary[1])]
    # 排掉 FinMind 每個產業都有的「其他…」垃圾桶分類,那個講了等於沒講
    subs = [_short(s) for s in subs if not str(s).startswith("其他")][:2]
    if subs:
        parts.append("、".join(subs))
    if not parts:
        ind = (lv.get(sid) or {}).get("industry")
        if ind:
            parts.append(str(ind))
    return "　".join(parts)


def _valuation(sid: str) -> dict:
    """本益比 / 股價淨值比 / 殖利率。FinMind `TaiwanStockPER`,一檔一次呼叫。
    只取最近一筆 —— 這是「現在貴不貴」,不是歷史序列。"""
    out = {}
    try:
        from .fetchers import fetch_finmind
        start = (date.today() - timedelta(days=14)).isoformat()
        rows = fetch_finmind("TaiwanStockPER", data_id=sid, start_date=start) or []
        if rows:
            r = sorted(rows, key=lambda x: str(x.get("date") or ""))[-1]
            for src, dst in (("PER", "pe"), ("PBR", "pb"), ("dividend_yield", "yield")):
                v = r.get(src)
                try:
                    f = float(v)
                    if f == f and f > 0:
                        out[dst] = round(f, 2)
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        log.warning(f"enrich: {sid} 估值抓取失敗:{e}")
    return out


def _news(sid: str, name: str) -> list[dict]:
    """近期新聞標題。走 Google News RSS —— **不吃 FinMind 額度**,但會拖時間,
    所以自己控 timeout,而且只留最近 NEWS_DAYS 天的。"""
    import urllib.parse
    try:
        import feedparser
        import requests
        q = urllib.parse.quote(f"{sid} {name}" if name else sid)
        url = (f"https://news.google.com/rss/search?q={q}"
               f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        r = requests.get(url, timeout=NEWS_TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        log.info(f"enrich: {sid} 新聞抓取略過:{e}")
        return []
    cutoff = date.today() - timedelta(days=NEWS_DAYS)
    out = []
    for e in feed.entries:
        pp = getattr(e, "published_parsed", None)
        d = None
        if pp:
            try:
                d = date(pp.tm_year, pp.tm_mon, pp.tm_mday)
            except Exception:
                d = None
        if d and d < cutoff:
            continue
        title = getattr(e, "title", "") or ""
        # Google News 的標題結尾常是「 - 經濟日報」,來源另外有欄位,重複顯示很占空間
        src = getattr(getattr(e, "source", None), "title", "") or ""
        if src and title.endswith(f" - {src}"):
            title = title[: -len(f" - {src}")]
        out.append({"title": title, "link": getattr(e, "link", ""),
                    "source": src, "date": d.isoformat() if d else ""})
        if len(out) >= NEWS_MAX:
            break
    return out


def enrich(sid: str, name: str, price: float | None = None,
           with_news: bool = True, with_valuation: bool = True) -> dict:
    """一檔的完整背景。當日快取,同一檔第二次觸發不會重抓。"""
    sid = str(sid)
    key = f"{now_tpe().strftime('%Y-%m-%d')}|{sid}"
    if key in _CACHE:
        out = dict(_CACHE[key])
        out["market_cap"] = _mktcap(out.get("shares"), price)
        return out

    lv, sector = _load_static()
    L = lv.get(sid) or {}
    out: dict = {
        "business": _business(sid, sector, lv),
        "shares": L.get("shares"),
        "eps_ttm": L.get("eps_ttm"),
        "revenue_yoy": L.get("revenue_yoy"),
        "ma5": L.get("ma5"), "ma20": L.get("ma20"),
        "ma60": L.get("ma60"), "high20": L.get("high20"),
    }
    if with_valuation:
        out.update(_valuation(sid))
    if with_news:
        out["news"] = _news(sid, name)
    _CACHE[key] = dict(out)
    out["market_cap"] = _mktcap(out.get("shares"), price)
    return out


def _mktcap(shares, price) -> float | None:
    """市值(億元)。股本 × 現價 —— 兩個數都已經在手上,不用再查。"""
    try:
        if shares and price:
            return round(float(shares) * float(price) / 1e8, 1)
    except (TypeError, ValueError):
        pass
    return None
