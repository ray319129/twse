"""統一報價層 —— 盤中即時 / 盤前試撮 / 收盤價的單一入口。

## 為什麼要這一層(2026-07-19)

FinMind Sponsor 方案開通後多了一支 `taiwan_stock_tick_snapshot`:**不帶 data_id
就回全市場 2852 檔、一次呼叫 0.7 秒**,而且帶了三個我們原本完全沒有的欄位 ——
均價(VWAP)、量比 volume_ratio、最佳一檔委買賣。

但訂閱是**按月**的(實測 2026-07-17 ~ 08-17),隨時可能不續。所以所有用到即時
報價的地方都必須走這一層,由這裡決定資料來源並**逐級降級**:

    ① Sponsor 全市場快照   2852 檔・含均價/量比/委買賣   ← 有訂閱時
    ② TWSE MIS 非官方 API   逐檔查・50 檔/批・易被 ban    ← 原本的作法
    ③ 本機 parquet 昨收     完全靜態                      ← 最後防線

## 三條鐵則(使用者 2026-07-19 明確要求,不要為了方便繞過)

**鐵則一:即時資料絕不進信心分。**
`scoring.py` / `indicators.py` / `backtest.py` **不得** import 本模組。一旦選股
邏輯依賴只有 Sponsor 拿得到的欄位,訂閱到期那天選股會直接壞掉,而且會壞得很安靜。
即時資料只餵「監控 / 執行 / 顯示」這一層 —— 這層本來就無狀態,斷了就退回現況。
(`scripts/check_realtime_isolation.py` 會在 CI 檢查這條線沒被跨過。)

**鐵則二:降級要看得見,不准靜默。**
每筆報價都帶 `source` 與 `ts`,網頁必須顯示「這是即時還是昨收」。使用者要能一眼
看出今天的數字能不能拿來做決定。

**鐵則三:每天存檔(見 `scripts/snapshot_archive.py`)。**
訂閱斷掉後,存下來的均價/量比歷史仍然是我們的。

## 額度與儲存

Sponsor = 6000 requests/hour(每小時重置)。全市場快照 = 1 request。
**瓶頸不是額度是儲存**:一份快照 parquet 171 KB,每分鐘存一次 = 900 MB/月,
公開 repo 撐不住。所以「監控」可以高頻(不存檔),「存檔」一天 7 個檢查點。
"""
from __future__ import annotations
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import pandas as pd
import requests

from .config import FINMIND_TOKEN, now_tpe
from .fetchers import fetch_mis_quotes, fetch_stock_info
from .storage import load_prices
from .utils import log

SNAPSHOT_API = "https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot"
USER_INFO_API = "https://api.web.finmindtrade.com/v2/user_info"

# 全市場快照在同一秒內可能被多處呼叫(持倉頁 + 熱力圖 + 出場監控),
# 這個 TTL 讓它們共用同一份,避免無謂地打 API。監控要更即時就把 ttl 調小。
_SNAP_CACHE: dict = {"ts": 0.0, "df": None}
# 10 秒 = 上游更新頻率(FinMind 文件寫明 tick_snapshot 約 10 秒換一次,本專案實測中位 11 秒)。
# 原本設 20 秒表示網頁最壞會拿到 20 秒前的快取 + 上游本身 10 秒 = 落後 30 秒。
# 設成 10 之後,快取永遠不會比上游還舊 —— 也不會因此多打 API,因為前端輪詢間隔比它長。
_DEFAULT_TTL = 10.0

SRC_SPONSOR = "sponsor"      # FinMind Sponsor 全市場即時快照
SRC_MIS = "mis"              # TWSE MIS 非官方即時
SRC_CLOSE = "close"          # 本機 parquet 昨收(靜態)


@dataclass
class Quote:
    """統一報價。無論來自哪一層,呼叫端只看這個結構。
    vwap / volume_ratio / bid / ask 只有 sponsor 層有,其餘為 None —— 呼叫端必須
    當「可能沒有」處理,不准假設一定拿得到(否則降級時會炸)。"""
    stock_id: str
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    volume: float | None = None          # 累計成交量(張)
    vwap: float | None = None            # 當日均價
    volume_ratio: float | None = None    # 量比(vs 昨日同時段)
    bid: float | None = None
    ask: float | None = None
    name: str = ""
    source: str = SRC_CLOSE
    ts: str = ""                         # 報價時間戳(資料本身的時間,不是抓取時間)

    @property
    def is_live(self) -> bool:
        return self.source in (SRC_SPONSOR, SRC_MIS)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 訂閱狀態 ----------

_LEVEL_CACHE: dict = {"ts": 0.0, "info": None}


def sponsor_status(ttl: float = 3600.0) -> dict:
    """查 FinMind 帳號等級與訂閱到期日。回傳
    {active, level, level_title, expires, days_left, limit_hour, error}。
    抓不到一律回 active=False(保守),讓上層自動走降級路徑。"""
    now = time.time()
    if _LEVEL_CACHE["info"] and now - _LEVEL_CACHE["ts"] < ttl:
        return _LEVEL_CACHE["info"]
    out = {"active": False, "level": 0, "level_title": "", "expires": "",
           "days_left": None, "limit_hour": 0, "error": ""}
    if not FINMIND_TOKEN:
        out["error"] = "無 FINMIND_TOKEN"
    else:
        try:
            j = requests.get(USER_INFO_API, params={"token": FINMIND_TOKEN}, timeout=20).json()
            out["level"] = int(j.get("level") or 0)
            out["level_title"] = j.get("level_title") or ""
            out["limit_hour"] = int(j.get("api_request_limit") or 0)
            # Sponsor / SponsorPro / Backer 各有一段;取有到期日且最晚的那個
            exp = ""
            for key in ("SponsorProInfo", "SponsorInfo", "BackerInfo"):
                e = (j.get(key) or {}).get("subscription_expired_date") or ""
                if e and e > exp:
                    exp = e
            out["expires"] = exp
            if exp:
                try:
                    d = (datetime.strptime(exp, "%Y-%m-%d").date() - now_tpe().date()).days
                    out["days_left"] = d
                except Exception:
                    pass
            # level>=3 才有 tick_snapshot;到期日已過就不算 active
            out["active"] = out["level"] >= 3 and (out["days_left"] is None or out["days_left"] >= 0)
        except Exception as e:
            out["error"] = str(e)
    _LEVEL_CACHE.update({"ts": now, "info": out})
    return out


# ---------- ① Sponsor 全市場快照 ----------

def fetch_snapshot_all(ttl: float = _DEFAULT_TTL, force: bool = False) -> pd.DataFrame:
    """全市場即時快照(2852 檔)。**一次呼叫**,不是逐檔。
    無 token / 非 Sponsor / API 失敗 → 回空 DataFrame(呼叫端據此降級,不 raise)。"""
    now = time.time()
    if not force and _SNAP_CACHE["df"] is not None and now - _SNAP_CACHE["ts"] < ttl:
        return _SNAP_CACHE["df"]
    if not FINMIND_TOKEN:
        return pd.DataFrame()
    try:
        r = requests.get(SNAPSHOT_API, params={"token": FINMIND_TOKEN}, timeout=45)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        log.warning(f"全市場快照抓取失敗(將降級):{e}")
        return pd.DataFrame()
    data = j.get("data") if isinstance(j, dict) else None
    if not data:
        log.warning(f"全市場快照無資料(msg={j.get('msg') if isinstance(j, dict) else '?'}),將降級。")
        return pd.DataFrame()
    df = pd.DataFrame(data)
    _SNAP_CACHE.update({"ts": now, "df": df})
    return df


def _num(v, zero_as_none: bool = False):
    """快照欄位 → float | None。**NaN 在 Python 是 truthy**,所以 `x or None` 擋不掉,
    停牌/未開盤的股票會把 NaN 一路帶進前端變成 null 以外的怪值 —— 一律走這裡。"""
    try:
        if v is None or pd.isna(v):
            return None
        f = float(v)
        return None if (zero_as_none and f == 0) else f
    except Exception:
        return None


def _q_from_snapshot(row: dict) -> Quote:
    """快照單列 → Quote。close 在盤中就是最新成交價;開盤前為 0/NaN → 用昨收頂著。
    FinMind 的 total_volume 為當日累計成交量(張)。"""
    c, chg = _num(row.get("close")), _num(row.get("change_price"))
    prev = round(c - chg, 4) if (c is not None and chg is not None) else None
    price = _num(row.get("close"), zero_as_none=True)
    name = row.get("name")
    return Quote(
        stock_id=str(row.get("stock_id") or ""),
        price=price if price is not None else prev,
        open=_num(row.get("open"), zero_as_none=True),
        high=_num(row.get("high"), zero_as_none=True),
        low=_num(row.get("low"), zero_as_none=True),
        prev_close=prev,
        change_pct=_num(row.get("change_rate")),
        volume=_num(row.get("total_volume")),
        vwap=_num(row.get("average_price"), zero_as_none=True),
        volume_ratio=_num(row.get("volume_ratio")),
        bid=_num(row.get("buy_price"), zero_as_none=True),
        ask=_num(row.get("sell_price"), zero_as_none=True),
        name="" if (name is None or pd.isna(name)) else str(name),
        source=SRC_SPONSOR,
        ts=str(row.get("date") or ""),
    )


# ---------- ③ 本機收盤(最後防線) ----------

def _q_from_close(sid: str) -> Quote:
    try:
        df = load_prices(sid)
        if df is None or df.empty:
            return Quote(stock_id=sid, source=SRC_CLOSE)
        last = df.iloc[-1]
        prev = float(df.iloc[-2]["close"]) if len(df) > 1 else None
        c = float(last["close"])
        return Quote(
            stock_id=sid, price=c, open=_f(last.get("open")), high=_f(last.get("high")),
            low=_f(last.get("low")), prev_close=prev,
            change_pct=round((c / prev - 1) * 100, 2) if prev else None,
            volume=_f(last.get("volume")), source=SRC_CLOSE,
            ts=str(df.index[-1])[:10],
        )
    except Exception:
        return Quote(stock_id=sid, source=SRC_CLOSE)


def _f(v):
    try:
        return None if v is None or pd.isna(v) else float(v)
    except Exception:
        return None


# ---------- 名稱補齊 ----------

_NAME_MAP: dict = {}


def _name_map() -> dict[str, str]:
    """快照的 name 欄實測只有 3.2% 有值,其餘為 NaN。用本機月快取的 stock_info 補,
    不額外打 API。抓不到就算了,名稱缺失不該讓報價失敗。"""
    global _NAME_MAP
    if _NAME_MAP:
        return _NAME_MAP
    try:
        info = fetch_stock_info()
        if info is not None and not info.empty:
            _NAME_MAP = dict(zip(info["stock_id"].astype(str), info["stock_name"].astype(str)))
    except Exception as e:
        log.warning(f"名稱對照載入失敗(不影響報價):{e}")
        _NAME_MAP = {}
    return _NAME_MAP


# ---------- 對外主入口 ----------

def get_quotes(symbols, ttl: float = _DEFAULT_TTL, allow_mis: bool = True) -> dict[str, Quote]:
    """取報價。symbols 可為 ["2330", ...] 或 [("2330","twse"), ...]。
    逐級降級:Sponsor 全市場快照 → MIS → 本機昨收。**永遠回得到東西**,
    每筆帶 source,呼叫端(與網頁)必須顯示來源,不准當成都是即時。"""
    pairs = [(s, "twse") if isinstance(s, str) else (str(s[0]), s[1]) for s in symbols]
    ids = [p[0] for p in pairs]
    out: dict[str, Quote] = {}

    snap = fetch_snapshot_all(ttl=ttl)
    if not snap.empty:
        want = set(ids)
        names = _name_map()
        for row in snap[snap["stock_id"].isin(want)].to_dict("records"):
            q = _q_from_snapshot(row)
            if q.stock_id:
                q.name = q.name or names.get(q.stock_id, "")
                out[q.stock_id] = q

    missing = [p for p in pairs if p[0] not in out]
    if missing and allow_mis:
        try:
            mis = fetch_mis_quotes(missing)
        except Exception as e:
            log.warning(f"MIS 降級層也失敗:{e}")
            mis = {}
        for sid, m in (mis or {}).items():
            prev = m.get("prev_close")
            px = m.get("price")
            out[sid] = Quote(
                stock_id=sid, price=px, open=m.get("open"), high=m.get("high"),
                low=m.get("low"), prev_close=prev,
                change_pct=round((px / prev - 1) * 100, 2) if (px and prev) else None,
                volume=m.get("acc_vol"), name=m.get("name", ""),
                source=SRC_MIS, ts=now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
            )

    for sid in ids:
        if sid not in out:
            out[sid] = _q_from_close(sid)
    return out


def market_snapshot_source() -> dict:
    """給網頁顯示的資料源狀態:哪一層在服務、資料時間、訂閱還剩幾天。
    鐵則二 —— 降級必須看得見。"""
    st = sponsor_status()
    snap = fetch_snapshot_all()
    if not snap.empty:
        # ⚠️ 取眾數不取第一列:每檔的時間戳是它自己的最後成交時間,冷門股會停在幾小時前。
        # 用 iloc[0] 會讓網頁顯示「資料時間 11:00」而其實是即時的(2026-07-20 踩到)。
        ts, lag = "", None
        try:
            ts = str(snap["date"].astype(str).str[:19].mode().iloc[0])
            lag = (now_tpe().replace(tzinfo=None)
                   - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except Exception:
            pass
        return {"source": SRC_SPONSOR, "label": "即時(全市場)", "ts": ts,
                "lag_s": None if lag is None else round(lag), "n": len(snap), "sponsor": st}
    if st.get("active"):
        # 有訂閱卻拿不到 → API 端出事,不是到期。講清楚差別,免得誤判成該續訂。
        return {"source": SRC_MIS, "label": "降級:即時快照暫時無法取得", "ts": "", "n": 0, "sponsor": st}
    return {"source": SRC_CLOSE, "label": "非即時(昨收)", "ts": "", "n": 0, "sponsor": st}
