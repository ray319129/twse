"""當沖訊號 —— 價位穿越偵測 + 分級。多空對稱。

## 為什麼是「價位穿越」而不是「動能追逐」

現有架構的端到端延遲是 30 秒～3 分半(輪詢 + Discord 合併視窗 + 人看手機下單)。
在這個延遲下**追不到動能** —— 一根 1 分 K 就能走完 1~2%,晚 30 秒等於買別人的出場單。

但「價位穿越」不一樣:水位是**盤前就算好的**,訊號的意思是「它剛穿過 105.2 了」,
晚 30 秒通常還在可執行區間。所以這一層刻意只做水位穿越,不做 tick 級動能。

## 為什麼一定要分級

實測既有盤中掃描每日觸發 9~15 筆,而當沖來回成本門檻是 0.52%。
**全做必虧**(15 筆 × 0.52% = 7.8%)。所以本層的價值在**拒絕**:
每筆都算分,預設只推前 N 名(config `ranking.push_top_n`),其餘只進網頁與台帳。

## 多空對稱

同一條水位,上穿是多方訊號、下破是空方訊號。空方額外要求 `gate.short_ok`,
而且成本要含借券費 —— 所以同一檔的多空分數會不一樣,這是刻意的。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

from ..utils import log
from . import cost as C


@dataclass
class Level:
    """一條預先算好的水位。`kind` 決定它的意義與權重。"""
    kind: str            # orh / orl / prev_high / prev_low / vwap / round
    price: float
    label: str


@dataclass
class Signal:
    stock_id: str
    name: str
    side: str                 # long / short
    kind: str                 # 觸發的水位種類
    label: str
    price: float              # 觸發當下價格
    level: float              # 被穿越的水位
    change_pct: float | None
    volume_ratio: float | None
    atr_pct: float | None
    cost_pct: float
    edge_ratio: float | None  # ATR% ÷ 成本%
    score: float
    reasons: list[str] = field(default_factory=list)
    degraded: bool = False
    fired_at: str = ""
    borrow_fee_pct: float | None = None
    lots_affordable: int = 0
    suggested_stop: float | None = None
    risk_per_lot: float | None = None
    # 用「觸發當下的即時價」重算的交易計畫(見 plan.py)。盤前那份是以昨收為基準,
    # 盤中觸發時價格已經動了,直接沿用會給出錯的進場/停損 —— 所以這裡重算一份。
    plan: dict | None = None
    plan_note: str = ""
    reasons_pool: list = field(default_factory=list)   # 標的池的「為什麼推薦」
    indicators: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# 各水位的先天權重。開盤區間是當沖最主流的參考(時間明確、雙方都看得到),
# 昨高低次之,整數關卡最弱(心理價位,常被穿了又回)。
_KIND_WEIGHT = {
    "orh": 1.00, "orl": 1.00,
    "prev_high": 0.85, "prev_low": 0.85,
    # 多週期匯流區:權重由「有幾個週期背書」決定(見 build_levels),
    # 三個週期都認得的價位比開盤區間還硬,只有一個週期的則跟昨高低差不多。
    "mtf3": 1.10, "mtf2": 0.90, "mtf1": 0.60,
    "vwap": 0.70,
    "round": 0.35,
}


# 兩條水位至少要離這麼遠(%),也是水位與現價的最小距離。
# 來回成本約 0.52~0.72%,相距不到一次成本的兩條線在交易上是同一筆;
# 而貼著現價的水位會在第一個 tick 就「突破」。取 0.35%(約當半次成本)。
MIN_LEVEL_GAP_PCT = 0.35


def build_levels(*, prev_high: float | None, prev_low: float | None,
                 open_range_high: float | None, open_range_low: float | None,
                 vwap: float | None, price: float, stock_id: str = "",
                 use_round: bool = True, zones: list | None = None,
                 max_zones: int = 6, min_confluence: int = 2) -> list[Level]:
    """組出一檔的水位清單。缺的就不放 —— 不要用估的湊數。

    `zones` 是 `levels_mtf` 產出的多週期支撐壓力區(月線/日線/小時線匯流)。
    使用者 2026-09-10 要求加入 —— 原本的水位只有「當天的」加上整數關卡,
    後者根本不是真的支撐壓力(心理價位,常穿了又回),是訊號多而不準的主因之一。
    """
    out: list[Level] = []
    if open_range_high:
        out.append(Level("orh", float(open_range_high), "開盤區間上緣"))
    if open_range_low:
        out.append(Level("orl", float(open_range_low), "開盤區間下緣"))
    if prev_high:
        out.append(Level("prev_high", float(prev_high), "昨日高點"))
    if prev_low:
        out.append(Level("prev_low", float(prev_low), "昨日低點"))
    if vwap:
        out.append(Level("vwap", float(vwap), "均價(VWAP)"))
    # ── 多週期匯流區 ────────────────────────────────────────────
    # ⚠️ **一定要在這裡依「離現價的距離」重新排序,不能相信存檔的順序。**
    # 標的池存的是 `levels_mtf.summary()` 的輸出,那是依**權重**排的;
    # 直接 `[:max_zones]` 會留下「很重但今天走不到」的水位、砍掉近的。
    # 實測 6226 光鼎(現價 29.65)存檔第 7 個是 24.50(離 17.4%),
    # 而 27.12(離 8.5%)排第 8 —— 取前 6 會保留前者、丟掉後者,完全相反。
    # 而且盤中價格早就離盤前基準走掉了,那個順序本來就過期。
    parsed = []
    for z in zones or []:
        try:
            zp = float(z.get("price"))
            conf = int(z.get("confluence") or 1)
        except (TypeError, ValueError, AttributeError):
            continue
        if zp <= 0:
            continue
        # 只有一個週期背書的水位不收(config levels.min_mtf_confluence)。
        # 匯流才是加入多週期的整個理由:一個價位要三個週期都認得才叫硬。
        # 實測若全收,單週期水位會佔新增水位的 38%(67/175),而它們的權重 0.6
        # 比 VWAP 還低 —— 等於用最弱的水位把訊號量灌回去,正是使用者抱怨的
        # 「訊號過多而且不準」。
        if conf < min_confluence:
            continue
        parsed.append((zp, conf, "+".join(z.get("timeframes") or []) or "多週期"))
    if price and price > 0:
        parsed.sort(key=lambda x: abs(x[0] - price))
    kept = 0
    for zp, conf, tfs in parsed:
        if kept >= max_zones:
            break
        # 貼著現價的水位不要:buffer 只有 0.2%,距離現價比它還近的「突破」
        # 第一個 tick 就會觸發 —— 那不是突破,是雜訊。
        if price and price > 0 and abs(zp - price) / price < MIN_LEVEL_GAP_PCT / 100.0:
            continue
        # 跟已經放進去的水位太近的也不要:兩條相距不到一次來回成本的線,
        # 交易上根本是同一筆,留著只會讓同一次波動觸發兩則通知。
        if any(abs(zp - lv.price) / max(zp, 1e-9) < MIN_LEVEL_GAP_PCT / 100.0 for lv in out):
            continue
        out.append(Level(f"mtf{min(max(conf, 1), 3)}", zp, f"{tfs}支撐壓力"))
        kept += 1

    if use_round and price and price > 0:
        # 整數關卡:取最接近現價的一個整數刻度(依 tick 級距決定粒度)
        t = C.tick_size(price, stock_id)
        step = 10.0 if t >= 0.5 else (5.0 if t >= 0.1 else 1.0)
        near = round(price / step) * step
        # 容差原本 3% —— 對當沖太鬆:離現價 2.8% 的整數關卡早就在幾小時前穿越過了,
        # 留著它只會在報價抖動時製造假訊號。收到 1%(約當一根 5 分K 的幅度)。
        if near > 0 and abs(near - price) / price < 0.01:
            out.append(Level("round", float(near), f"整數關卡 {near:g}"))
    return out


def detect_cross(*, prev_price: float | None, price: float, levels: list[Level],
                 buffer_pct: float = 0.002) -> list[tuple[Level, str]]:
    """偵測從 prev_price 到 price 之間穿越了哪些水位。

    回傳 [(Level, 'long'|'short')]。要穿過 buffer 才算 —— 貼著水位來回磨不算穿越
    (這是既有盤中掃描 BREAKOUT_BUFFER 的同一個道理)。
    第一次輪詢沒有 prev_price → 回空(不猜,寧可漏不可錯)。
    """
    if prev_price is None or price is None or price <= 0 or prev_price <= 0:
        return []
    out = []
    for lv in levels:
        up = lv.price * (1 + buffer_pct)
        dn = lv.price * (1 - buffer_pct)
        if prev_price <= lv.price and price >= up:
            out.append((lv, "long"))
        elif prev_price >= lv.price and price <= dn:
            out.append((lv, "short"))
    return out


def score_signal(*, kind: str, edge_ratio: float | None, volume_ratio: float | None,
                 cost_pct: float, change_pct: float | None, side: str) -> tuple[float, list[str]]:
    """0~100 分。**這是本層最重要的東西** —— 決定哪 2~3 筆值得打斷你。

    四個因子,全部都是「這筆做起來划不划算」而不是「它漲得多兇」:
      1. 水位品質(kind)        —— 開盤區間 > 昨高低 > VWAP > 整數關卡
      2. 空間 edge_ratio       —— ATR% ÷ 成本%,一天的波動能覆蓋幾次來回成本
      3. 量能 volume_ratio     —— 沒量的穿越是假突破
      4. 位階 change_pct       —— 已經漲太多再追,期望值最差(既有台帳驗過的事)
    """
    reasons: list[str] = []
    w = _KIND_WEIGHT.get(kind, 0.5)
    reasons.append(f"水位品質 {kind}({w:.2f})")

    edge = min((edge_ratio or 0) / 8.0, 1.0)
    # 「空間」不在這裡重複講 —— 標的池的理由已經有較完整的
    # 「波動是來回成本的 X 倍(操作空間)」,兩句併在同一張卡上是冗詞。


    vol = 0.5
    if volume_ratio is not None:
        vol = max(0.0, min(volume_ratio / 2.0, 1.0))
        reasons.append(f"量比 {volume_ratio:.2f}")

    # 位階:多方漲太多、空方跌太多都扣分(追高殺低的期望值最差)
    pos = 1.0
    if change_pct is not None:
        move = change_pct if side == "long" else -change_pct
        if move > 6:
            pos, _ = 0.25, reasons.append(f"已{'漲' if side == 'long' else '跌'} {abs(move):.1f}%,追價風險高")
        elif move > 4:
            pos = 0.55
        elif move < 0.3:
            pos, _ = 0.6, reasons.append("尚未表態")

    cheap = max(0.0, min((1.3 - cost_pct) / (1.3 - 0.5), 1.0))
    score = (w * 0.30 + edge * 0.30 + vol * 0.20 + pos * 0.10 + cheap * 0.10) * 100
    return round(score, 1), reasons


def rank_and_cap(signals: list[Signal], *, push_top_n: int = 3,
                 min_score: float = 55.0) -> tuple[list[Signal], list[Signal]]:
    """回傳 (要推播的, 只進網頁與台帳的)。

    多空**各自**取前 N —— 不然強勢盤會被多方洗版、空方永遠推不出來。
    """
    keep = [s for s in signals if s.score >= min_score]
    dropped = [s for s in signals if s.score < min_score]
    push: list[Signal] = []
    for side in ("long", "short"):
        side_sigs = sorted([s for s in keep if s.side == side], key=lambda s: -s.score)
        push.extend(side_sigs[:push_top_n])
        dropped.extend(side_sigs[push_top_n:])
    push.sort(key=lambda s: -s.score)
    if dropped:
        log.info(f"當沖訊號分級:推播 {len(push)} 筆、僅記錄 {len(dropped)} 筆"
                 f"(成本門檻讓「少而準」比「多而勤」重要)")
    return push, dropped


def suggest_stop(*, price: float, side: str, atr: float | None,
                 atr_mult: float = 1.0) -> float | None:
    """建議停損價。ATR 為 None 時不給 —— 不要用猜的數字讓人拿去下單。"""
    if not price or price <= 0 or not atr or atr <= 0:
        return None
    d = atr * atr_mult
    return round(price - d, 2) if side == "long" else round(price + d, 2)
