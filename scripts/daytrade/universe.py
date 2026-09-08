"""當沖標的池 —— 把「法規閘門」「成本經濟」「使用者偏好」三件事合成今天可做的清單。

## 設計重點:成本要在選擇的當下就看得見

使用者要「自選標的範圍,例如成交價格範圍」。但價格區間**不是偏好問題,是成本問題**:
台股 tick 六級非線性,實測 99 元的股票來回成本 0.523%、105 元 1.273%,差 2.4 倍
(見 `cost.py`)。所以這裡不只是套一個 min/max 過濾,而是**每一檔都附上成本評級**,
並且預設就把 `expensive` 的價位帶排除掉 —— 讓「自由選擇」不等於「自己踩坑」。

## 多空分流

多方只要 `long_ok`(可當沖、非處置)。
空方額外要 `short_ok`(非暫停先賣後買)且借券費在門檻內,而且**借券費要加進成本**。
兩邊的成本不同、可做清單也不同,所以輸出是兩個獨立清單而不是一個清單加旗標。

## 資料來源

價格/ATR/成交額全部取自 `data/levels.parquet`(盤前建好的快取,不打任何 API)。
法規閘門取自 `rules.load_or_fetch()`(當日快取)。**這一層完全不碰即時報價** ——
標的池是盤前決定的,盤中只是在這個池子裡等訊號。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

import pandas as pd

from ..config import DATA_DIR
from ..utils import log
from . import cost as C
from .rules import Gate, gate_for, load_or_fetch

PREFS_PATH = Path(DATA_DIR).parent / "config" / "daytrade_prefs.json"
LEVELS_PATH = DATA_DIR / "levels.parquet"
OUT_DIR = DATA_DIR / "daytrade"

DEFAULT_PREFS = {
    "quota_twd": 250000,
    "price_min": 20.0, "price_max": 300.0,
    "min_dollar_volume_m": 200.0,
    "min_atr_pct": 2.0, "max_atr_pct": 12.0,
    "max_cost_pct": 0.90,
    "allow_long": True, "allow_short": True,
    "max_borrow_fee_pct": 0.5,
    "min_edge_ratio": 3.0,
    "exclude": [], "include_always": [],
    "max_universe": 60,
}


def load_prefs(path: Path | None = None) -> dict:
    """讀使用者偏好。缺欄位一律用預設值補 —— 網頁寫回時可能只帶部分欄位。"""
    p = path or PREFS_PATH
    prefs = dict(DEFAULT_PREFS)
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        for k, v in raw.items():
            if k.startswith("_"):
                continue          # _comment / _note 之類的說明欄位
            if k in prefs:
                prefs[k] = v
    except FileNotFoundError:
        log.info(f"當沖偏好檔不存在({p}),使用預設值。")
    except Exception as e:
        log.warning(f"當沖偏好檔讀取失敗,使用預設值:{e}")
    return prefs


@dataclass
class Candidate:
    stock_id: str
    name: str
    market: str
    side: str                 # long / short
    price: float              # 昨收(盤前用來估成本與張數)
    atr_pct: float | None
    edge_ratio: float | None  # ATR% ÷ 來回成本% —— 一天的波動能覆蓋幾次來回成本
    dollar_volume_m: float | None
    tick: float
    cost_pct: float           # 來回成本(含借券費,若做空)
    cost_rating: str          # cheap / normal / expensive
    cost_note: str
    breakeven_ticks: float | None
    borrow_fee_pct: float | None
    lots_affordable: int      # 以使用者額度買得起幾張
    gate_note: str
    score: float = 0.0        # 標的池排序分(不是訊號分)

    def to_dict(self) -> dict:
        return asdict(self)


def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _load_levels() -> pd.DataFrame:
    if not LEVELS_PATH.exists():
        log.warning("找不到 data/levels.parquet —— 當沖標的池需要盤前建好的均線/成交額快取。")
        return pd.DataFrame()
    try:
        return pd.read_parquet(LEVELS_PATH)
    except Exception as e:
        log.warning(f"levels 讀取失敗:{e}")
        return pd.DataFrame()


def real_atr_pct(stock_id: str) -> float | None:
    """用本機價格序列算真正的 ATR(14)%。

    第一版本來想用 levels 裡的 high20/ma20 距離當代理,實測結果是**排序出一堆金融股**
    (兆豐金、華南金、第一金)—— 那個代理量到的是「距月線多遠」不是「一天會走多少」,
    對當沖等於沒有波動過濾。當沖沒有波動就只剩成本,所以這裡寧可多花一點時間算真的。

    只對通過價格/流動性初篩的標的算(約 200 檔),盤前批次跑,不打任何 API。
    """
    try:
        from ..storage import load_prices
        from ..indicators import compute_all
        df = load_prices(stock_id)
        if df is None or len(df) < 30:
            return None
        d = compute_all(df.copy())
        last = d.iloc[-1]
        atr, close = _f(last.get("atr14")), _f(last.get("close"))
        if not atr or not close or close <= 0:
            return None
        return atr / close * 100.0
    except Exception:
        return None


def build(prefs: dict | None = None, gates: dict[str, Gate] | None = None,
          today: date | None = None) -> dict:
    """產出今日當沖標的池。回傳 {date, prefs, long: [...], short: [...], stats}。"""
    today = today or date.today()
    prefs = prefs or load_prefs()
    gates = gates if gates is not None else load_or_fetch(today)
    levels = _load_levels()

    if levels.empty or not gates:
        log.warning("當沖標的池:levels 或閘門資料缺一,回空池(fail closed)。")
        return {"date": today.isoformat(), "prefs": prefs, "long": [], "short": [],
                "stats": {"reason": "no_levels" if levels.empty else "no_gates"}}

    exclude = {str(x) for x in (prefs.get("exclude") or [])}
    always = {str(x) for x in (prefs.get("include_always") or [])}
    ticks_crossed = 2.0

    longs: list[Candidate] = []
    shorts: list[Candidate] = []
    min_edge = float(prefs.get("min_edge_ratio", 3.0))
    stats = {"scanned": 0, "gate_blocked": 0, "price_filtered": 0,
             "liquidity_filtered": 0, "unaffordable": 0, "no_atr": 0,
             "volatility_filtered": 0, "cost_filtered": 0, "edge_filtered": 0,
             "short_gate_blocked": 0}

    for row in levels.to_dict("records"):
        sid = str(row.get("stock_id") or "").strip()
        if not sid or sid in exclude:
            continue
        stats["scanned"] += 1
        forced = sid in always

        g = gate_for(gates, sid)
        if not g.long_ok:
            stats["gate_blocked"] += 1
            if not forced:
                continue

        price = _f(row.get("prev_close"))
        if price is None or price <= 0:
            continue
        if not forced and not (prefs["price_min"] <= price <= prefs["price_max"]):
            stats["price_filtered"] += 1
            continue

        turnover = _f(row.get("avg_turnover"))
        dv_m = (turnover / 1e6) if turnover else None
        if not forced and (dv_m is None or dv_m < prefs["min_dollar_volume_m"]):
            stats["liquidity_filtered"] += 1
            continue

        # 買不起一張就不該進池 —— 顯示「可買 0 張」的標的對使用者沒有意義。
        lots = C.lots_for_quota(price, prefs["quota_twd"])
        if not forced and lots < 1:
            stats["unaffordable"] += 1
            continue

        # 波動是**硬條件**不是軟權重:當沖沒有波動就只剩成本。
        # (通過前面便宜的篩選後才算真 ATR,約 200 檔,不會拖太久。)
        atr_pct = real_atr_pct(sid)
        if not forced:
            if atr_pct is None:
                stats["no_atr"] += 1
                continue
            if not (prefs["min_atr_pct"] <= atr_pct <= prefs["max_atr_pct"]):
                stats["volatility_filtered"] += 1
                continue

        # ── 多方 ──
        long_cost = C.round_trip_cost_pct(price, sid, ticks_crossed=ticks_crossed)
        rating, note = C.cost_rating(price, sid, ticks_crossed=ticks_crossed)
        if long_cost is None:
            continue
        long_edge = (atr_pct / long_cost) if (atr_pct and long_cost > 0) else None
        if not forced and long_cost > prefs["max_cost_pct"]:
            stats["cost_filtered"] += 1
        elif not forced and (long_edge is None or long_edge < min_edge):
            # 一天的波動幅度連來回成本的 min_edge 倍都不到 → 這檔沒有操作空間
            stats["edge_filtered"] += 1
        elif prefs.get("allow_long", True):
            longs.append(Candidate(
                stock_id=sid, name=str(row.get("name") or g.name or ""), market=g.market,
                side="long", price=price,
                atr_pct=round(atr_pct, 2) if atr_pct else None,
                edge_ratio=round(long_edge, 2) if long_edge else None,
                dollar_volume_m=dv_m,
                tick=C.tick_size(price, sid), cost_pct=round(long_cost, 3),
                cost_rating=rating, cost_note=note,
                breakeven_ticks=round(C.breakeven_ticks(price, sid, ticks_crossed=ticks_crossed) or 0, 2),
                borrow_fee_pct=None, lots_affordable=lots, gate_note=g.explain(),
            ))

        # ── 空方:額外閘門 + 借券費進成本 ──
        if not prefs.get("allow_short", True):
            continue
        if not g.short_ok:
            stats["short_gate_blocked"] += 1
            continue
        fee = g.borrow_fee_pct
        if fee is not None and fee > prefs["max_borrow_fee_pct"]:
            stats["short_gate_blocked"] += 1
            continue
        short_cost = C.round_trip_cost_pct(price, sid, ticks_crossed=ticks_crossed,
                                           borrow_fee_pct=fee or 0.0)
        if short_cost is None or (not forced and short_cost > prefs["max_cost_pct"]):
            continue
        short_edge = (atr_pct / short_cost) if (atr_pct and short_cost > 0) else None
        if not forced and (short_edge is None or short_edge < min_edge):
            stats["edge_filtered"] += 1
            continue
        s_rating, s_note = C.cost_rating(price, sid, ticks_crossed=ticks_crossed,
                                         borrow_fee_pct=fee or 0.0)
        shorts.append(Candidate(
            stock_id=sid, name=str(row.get("name") or g.name or ""), market=g.market,
            side="short", price=price,
            atr_pct=round(atr_pct, 2) if atr_pct else None,
            edge_ratio=round(short_edge, 2) if short_edge else None,
            dollar_volume_m=dv_m,
            tick=C.tick_size(price, sid), cost_pct=round(short_cost, 3),
            cost_rating=s_rating, cost_note=s_note,
            breakeven_ticks=round(C.breakeven_ticks(price, sid, ticks_crossed=ticks_crossed,
                                                    borrow_fee_pct=fee or 0.0) or 0, 2),
            borrow_fee_pct=fee, lots_affordable=lots, gate_note=g.explain(),
        ))

    # ── 標的池排序:流動性高、成本低、波動足夠 ──
    # 這**不是**訊號分,只是「今天先盯哪些」。真正的進出場分數在訊號層。
    def rank(c: Candidate) -> float:
        # 主軸是 edge_ratio(一天波動能覆蓋幾次來回成本)—— 這才是當沖有沒有空間的關鍵。
        # 流動性只當「能不能進出」的及格條件,不該主導排序:第一版用流動性 0.4 主導,
        # 結果選出一整排金融股(兆豐金、華南金、第一金)——成交額大、成本低,
        # 但一天走不到 1%,對當沖毫無意義。
        edge = min((c.edge_ratio or 0) / 8.0, 1.0)
        liq = min((c.dollar_volume_m or 0) / 1000.0, 1.0)
        cheap = max(0.0, (1.3 - c.cost_pct) / (1.3 - 0.5))
        return round((edge * 0.6 + liq * 0.2 + cheap * 0.2) * 100, 1)

    for c in longs + shorts:
        c.score = rank(c)
    cap = int(prefs.get("max_universe", 60))
    longs.sort(key=lambda c: -c.score)
    shorts.sort(key=lambda c: -c.score)
    longs, shorts = longs[:cap], shorts[:cap]

    stats.update({"long_count": len(longs), "short_count": len(shorts)})
    log.info(f"當沖標的池 {today}:多方 {len(longs)} 檔、空方 {len(shorts)} 檔"
             f"(掃 {stats['scanned']} 檔;閘門擋 {stats['gate_blocked']}、"
             f"價格帶擋 {stats['price_filtered']}、流動性擋 {stats['liquidity_filtered']}、"
             f"買不起擋 {stats['unaffordable']}、波動擋 {stats['volatility_filtered']}、"
             f"成本帶擋 {stats['cost_filtered']}、空間不足擋 {stats['edge_filtered']}、"
             f"空方閘門擋 {stats['short_gate_blocked']})")
    return {"date": today.isoformat(), "prefs": prefs, "stats": stats,
            "long": [c.to_dict() for c in longs], "short": [c.to_dict() for c in shorts]}


def save(pool: dict, today: date | None = None) -> Path:
    today = today or date.today()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"pool-{today.isoformat()}.json"
    p.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    return p


def scan_ids(pool: dict) -> list[str]:
    """盤中要盯的代號(多空聯集、去重)。給訊號層當掃描宇宙。"""
    ids: list[str] = []
    for side in ("long", "short"):
        for c in pool.get(side, []):
            sid = c.get("stock_id")
            if sid and sid not in ids:
                ids.append(sid)
    return ids


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    pool = build()
    print(f"\n偏好:價格 {pool['prefs']['price_min']}~{pool['prefs']['price_max']} 元、"
          f"額度 {pool['prefs']['quota_twd']:,} 元")
    print(f"統計:{pool['stats']}\n")
    for side, label in (("long", "多方"), ("short", "空方")):
        rows = pool[side][:8]
        print(f"── {label} 前 {len(rows)} 檔 ──")
        for c in rows:
            print(f"  {c['stock_id']:<6}{c['name']:<9}{c['price']:>7.2f} "
                  f"ATR {c['atr_pct'] or 0:>5.2f}% 成本 {c['cost_pct']:.3f}% "
                  f"空間 {c['edge_ratio'] or 0:>5.2f}x 可買 {c['lots_affordable']:>2} 張  "
                  f"{c['gate_note']}")
        print()
    if "--save" in sys.argv:
        print("已存:", save(pool))
