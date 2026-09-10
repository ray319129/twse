"""多週期支撐壓力(月線 / 日線 / 小時線)—— 使用者 2026-09-10 要求。

## 為什麼要多週期

第一版的水位只有「開盤區間 / 昨高低 / VWAP / 整數關卡」,前三個都只看**當天**,
整數關卡則根本不是真的支撐壓力(它是心理價位,常穿了又回)。
結果就是訊號多而不準:一堆穿越發生在沒有任何實質意義的價位上。

**多週期匯流(confluence)才是重點**:同一個價位如果在月線、日線、小時線上
都是轉折點,它就比只有單一週期認得的價位可靠得多。這一支的產出不只是「有哪些水位」,
而是**每個水位有幾個週期背書**,讓訊號層可以只挑有匯流的來推。

## 三個週期怎麼取

| 週期 | 資料源 | 取什麼 |
|---|---|---|
| 月線 | 本機日線重採樣(零 API) | 近 24 個月的月高/月低 + 前月高低 |
| 日線 | 本機 parquet(零 API) | 擺動高低點(swing pivot)+ MA20/MA60/MA120 |
| 小時線 | yfinance 60m(要 API) | 近 60 天的擺動高低點 |

⚠️ 小時線要打網路,所以**只對真正要盯的標的算**(盤中掃描宇宙,約 60 檔),
不要對整個標的池呼叫。日線與月線是零 API,可以隨便算。

## 擺動高低點的定義

`n` 根 K 棒的區域極值:某根的 high 是前後各 n 根裡最高 → 擺動高點。
不用複雜的 ZigZag —— 當沖用不到那個精度,而且參數多了反而過擬合。

## 匯流怎麼合併

價位差在 `tolerance_pct`(預設 0.4%)以內的視為同一個區域,合併成一個 zone,
權重相加。月線權重最高(3)、日線次之(2)、小時線最低(1)——
週期越長的水位越少人能推動,也越多人在看。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import pandas as pd

from ..utils import log

# 各週期的權重。月線 > 日線 > 小時線 —— 週期越長,水位越硬。
W_MONTH, W_DAY, W_HOUR = 3.0, 2.0, 1.0
SWING_N_DAY = 3          # 日線擺動點:前後各 3 根
SWING_N_HOUR = 3         # 小時線同上
MONTHS_BACK = 24
HOURS_DAYS = 60          # 小時線回看幾天(yfinance 60m 上限約 730 天,取 60 足夠)
DEFAULT_TOLERANCE_PCT = 0.4
# 只保留「今天走得到」的水位。月線回看 24 個月,對漲了好幾倍的股票會挖出
# 幾年前的價位 —— 實測 2426 鼎元現價 99.5 卻跑出 20.11 / 16.71 / 14.81 的月線低,
# 那些對當沖毫無意義,還會把權重最高的位置佔滿。
# 範圍取 max(固定 6%, 2 倍日 ATR)。ATR 是**一天**的區間,所以 2 倍已經是
# 「今天最多走到哪」的上緣;第一版用 5 倍,對 ATR 9.5% 的鼎元算出 ±47 元(±47%),
# 等於整整一週的幅度,又把幾年前的價位放回來了。
RELEVANT_PCT = 6.0
RELEVANT_ATR_MULT = 2.0


@dataclass
class Zone:
    """一個支撐/壓力區域(可能由多個週期的水位合併而成)。"""
    price: float
    weight: float
    kind: str                      # support / resistance(相對於參考價)
    sources: list = field(default_factory=list)   # ["月線高", "日線MA20", ...]

    @property
    def timeframes(self) -> set:
        return {s.split("線")[0] + "線" for s in self.sources if "線" in s}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timeframes"] = sorted(self.timeframes)
        d["confluence"] = len(self.timeframes)
        return d


def _swing_levels(df: pd.DataFrame, n: int, label: str) -> list[tuple[float, str]]:
    """區域極值:某根的 high 是前後各 n 根裡最高 → 擺動高點(低點同理)。

    刻意不用 ZigZag —— 當沖用不到那個精度,參數多了只會過擬合。
    """
    out: list[tuple[float, str]] = []
    if df is None or len(df) < n * 2 + 1:
        return out
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(n, len(df) - n):
        window_h = h[i - n:i + n + 1]
        window_l = l[i - n:i + n + 1]
        if h[i] == window_h.max():
            out.append((float(h[i]), f"{label}高"))
        if l[i] == window_l.min():
            out.append((float(l[i]), f"{label}低"))
    return out


def month_levels(daily: pd.DataFrame) -> list[tuple[float, str]]:
    """月線水位:近 N 個月的月高/月低。零 API(日線重採樣)。"""
    if daily is None or daily.empty:
        return []
    try:
        m = (daily.resample("ME")
                  .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
                  .dropna(subset=["close"]).tail(MONTHS_BACK))
    except Exception as e:
        log.warning(f"月線重採樣失敗:{e}")
        return []
    out: list[tuple[float, str]] = []
    for _, r in m.iterrows():
        out.append((float(r["high"]), "月線高"))
        out.append((float(r["low"]), "月線低"))
    return out


def day_levels(daily: pd.DataFrame) -> list[tuple[float, str]]:
    """日線水位:擺動高低點 + 三條關鍵均線。零 API。"""
    if daily is None or len(daily) < 30:
        return []
    out = _swing_levels(daily.tail(120), SWING_N_DAY, "日線")
    c = daily["close"]
    for win, name in ((20, "日線MA20"), (60, "日線MA60"), (120, "日線MA120")):
        if len(c) >= win:
            v = float(c.tail(win).mean())
            if v > 0:
                out.append((v, name))
    return out


def hour_levels(stock_id: str, market: str = "twse") -> list[tuple[float, str]]:
    """小時線水位:近 60 天的擺動高低點。**會打網路**,只給要盯的標的用。"""
    try:
        import yfinance as yf
        from ..fetchers import yf_ticker
        df = yf.download(yf_ticker(stock_id, market), period=f"{HOURS_DAYS}d",
                         interval="60m", progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        df = df.dropna(subset=["high", "low"])
        return _swing_levels(df, SWING_N_HOUR, "小時線")
    except Exception as e:
        log.info(f"小時線水位取得失敗 {stock_id}(略過該週期):{e}")
        return []


def _weight_of(source: str) -> float:
    if source.startswith("月線"):
        return W_MONTH
    if source.startswith("日線"):
        return W_DAY
    return W_HOUR


def merge_zones(levels: list[tuple[float, str]], ref_price: float, *,
                tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> list[Zone]:
    """把相近的水位合併成區域,權重相加。回傳依「離參考價的距離」排序的清單。

    合併是必要的:同一個價位在月線、日線、小時線上往往差幾毛錢,不合併的話
    會變成三條幾乎重疊的線,既看不出匯流也會讓穿越判斷重複觸發。
    """
    if not levels or not ref_price or ref_price <= 0:
        return []
    tol = ref_price * tolerance_pct / 100.0
    buckets: list[dict] = []
    for price, src in sorted(levels, key=lambda x: x[0]):
        if price <= 0:
            continue
        if buckets and abs(price - buckets[-1]["sum"] / buckets[-1]["n"]) <= tol:
            b = buckets[-1]
            b["sum"] += price
            b["n"] += 1
            b["w"] += _weight_of(src)
            if src not in b["sources"]:
                b["sources"].append(src)
        else:
            buckets.append({"sum": price, "n": 1, "w": _weight_of(src), "sources": [src]})

    zones = [Zone(price=round(b["sum"] / b["n"], 2), weight=round(b["w"], 1),
                  kind=("resistance" if b["sum"] / b["n"] > ref_price else "support"),
                  sources=b["sources"]) for b in buckets]
    zones.sort(key=lambda z: abs(z.price - ref_price))
    return zones


def relevant_range(ref_price: float, atr: float | None = None) -> float:
    """今天可能走到的範圍(單邊,絕對值)。超出這個範圍的水位對當沖沒有意義。"""
    base = ref_price * RELEVANT_PCT / 100.0
    if atr and atr > 0:
        base = max(base, atr * RELEVANT_ATR_MULT)
    return base


def build(stock_id: str, daily: pd.DataFrame, ref_price: float, *,
          market: str = "twse", with_hourly: bool = False,
          atr: float | None = None,
          tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> list[Zone]:
    """組出這一檔的多週期支撐壓力區。

    `with_hourly=False` 時只用月線+日線(零 API)—— 建整個標的池時用這個;
    盤中只對真正要盯的那幾十檔開 `with_hourly=True`。

    **會先濾掉離現價太遠的水位**(見 RELEVANT_PCT)—— 否則月線那 24 個月會把
    幾年前的價位也算進來,對當沖完全沒有參考價值。
    """
    lv: list[tuple[float, str]] = []
    lv += month_levels(daily)
    lv += day_levels(daily)
    if with_hourly:
        lv += hour_levels(stock_id, market)
    rng = relevant_range(ref_price, atr)
    lv = [(p, src) for p, src in lv if abs(p - ref_price) <= rng]
    return merge_zones(lv, ref_price, tolerance_pct=tolerance_pct)


def nearest(zones: list[Zone], ref_price: float, kind: str,
            min_weight: float = 0.0) -> Zone | None:
    """離參考價最近的支撐(kind='support')或壓力(kind='resistance')。

    `min_weight` 讓呼叫端只要有份量的水位 —— 交易計畫的目標價用這個,
    比原本的「20 日高低」準得多(那只是一個窗口的極值,沒有任何匯流概念)。
    """
    cands = [z for z in zones if z.kind == kind and z.weight >= min_weight]
    if kind == "resistance":
        cands = [z for z in cands if z.price > ref_price]
    else:
        cands = [z for z in cands if z.price < ref_price]
    return min(cands, key=lambda z: abs(z.price - ref_price)) if cands else None


def summary(zones: list[Zone], top: int = 6) -> list[dict]:
    """給卡片顯示用:最重要的幾個區域(依權重),含哪些週期背書。"""
    return [z.to_dict() for z in sorted(zones, key=lambda z: -z.weight)[:top]]
