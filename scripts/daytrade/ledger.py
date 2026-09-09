"""當沖台帳 —— 每筆推播記下觸發價與後續走勢,一個月後才知道這套到底有沒有用。

## 為什麼這一層要跟第一版一起上線,不能拖

既有系統的績效台帳已經驗出「動能是 beta 不是 alpha、線上台帳是負 alpha」。
當沖的成本是波段的 3 倍(每筆來回 0.52%),所以**先驗證再相信**不是保守,是必要。
沒有台帳的話,一個月後只會剩下印象,而印象一定會偏向記得賺的那幾筆。

## 與既有 performance.json 分開記

當沖的勝率/持有期/成本結構跟波段選股完全不同,混在一起算會兩邊都失真。
(這也是使用者「當沖功能要獨立出來測試」要求的一部分。)

## 記什麼

每筆推播記:觸發當下的價、分數、成本、水位種類、多空,
以及**推播後 5/15/30/60 分鐘的價格**。有了這四個點就能回答三個問題:
  1. 訊號發出後有沒有往預期方向走?(方向對不對)
  2. 走的幅度有沒有超過來回成本?(有沒有可執行的邊際)
  3. 分數高的那幾筆是不是真的比較好?(分級有沒有效)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from pathlib import Path

from ..config import DATA_DIR
from ..utils import log

LEDGER_DIR = DATA_DIR / "daytrade"


def _load_followups() -> tuple:
    """追蹤時點。可由 config/daytrade.yaml 的 ledger.followup_minutes 覆寫 ——
    原本那個設定沒被讀,只是剛好與這裡的預設一致。"""
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(DATA_DIR).parent / "config" / "daytrade.yaml")
                             .read_text(encoding="utf-8")) or {}
        v = (cfg.get("ledger", {}) or {}).get("followup_minutes")
        got = tuple(sorted({int(x) for x in v if int(x) > 0}))
        if got:
            return got
    except Exception:
        pass
    return (5, 15, 30, 60)


FOLLOWUP_MINUTES = _load_followups()


@dataclass
class Entry:
    """一筆被推播(或被記錄但未推播)的訊號 + 其後續走勢。"""
    stock_id: str
    name: str
    side: str
    kind: str
    fired_at: str            # HH:MM
    price: float             # 觸發價
    level: float
    score: float
    cost_pct: float
    edge_ratio: float | None = None
    volume_ratio: float | None = None
    atr_pct: float | None = None
    pushed: bool = True      # False = 分級後只記錄、沒推播(對照組!)
    degraded: bool = False
    borrow_fee_pct: float | None = None
    followup: dict = field(default_factory=dict)   # {"5": 101.2, "15": ...}

    def to_dict(self) -> dict:
        return asdict(self)

    def excess_pct(self, minutes: int) -> float | None:
        """該時點相對觸發價的報酬(已依多空調向),**扣掉來回成本**。

        扣成本才是真正的問題:方向對但走不到 0.52% 等於白做。
        """
        px = self.followup.get(str(minutes))
        if px is None or not self.price:
            return None
        raw = (px / self.price - 1) * 100
        directional = -raw if self.side == "short" else raw
        return round(directional - self.cost_pct, 3)


def path_for(day: date) -> Path:
    return LEDGER_DIR / f"ledger-{day.isoformat()}.json"


def load(day: date | None = None) -> list[Entry]:
    day = day or date.today()
    p = path_for(day)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = Entry.__dataclass_fields__
        return [Entry(**{k: v for k, v in row.items() if k in fields})
                for row in raw.get("entries", [])]
    except Exception as e:
        log.warning(f"當沖台帳讀取失敗:{e}")
        return []


def save(entries: list[Entry], day: date | None = None) -> Path:
    day = day or date.today()
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    p = path_for(day)
    p.write_text(json.dumps(
        {"date": day.isoformat(),
         "updated_at": datetime.now().isoformat(timespec="seconds"),
         "entries": [e.to_dict() for e in entries]}, ensure_ascii=False), encoding="utf-8")
    return p


def record(entries: list[Entry], signal, *, pushed: bool) -> list[Entry]:
    """把一筆訊號寫進台帳。同一檔同一水位同一方向當日只記一次。"""
    key = (signal.stock_id, signal.kind, signal.side)
    if any((e.stock_id, e.kind, e.side) == key for e in entries):
        return entries
    entries.append(Entry(
        stock_id=signal.stock_id, name=signal.name, side=signal.side, kind=signal.kind,
        fired_at=signal.fired_at, price=signal.price, level=signal.level,
        score=signal.score, cost_pct=signal.cost_pct, edge_ratio=signal.edge_ratio,
        volume_ratio=signal.volume_ratio, atr_pct=signal.atr_pct, pushed=pushed,
        degraded=signal.degraded, borrow_fee_pct=signal.borrow_fee_pct,
    ))
    return entries


def _mins_between(hhmm_a: str, hhmm_b: str) -> int | None:
    try:
        ha, ma = (int(x) for x in hhmm_a.split(":"))
        hb, mb = (int(x) for x in hhmm_b.split(":"))
        return (hb * 60 + mb) - (ha * 60 + ma)
    except Exception:
        return None


def update_followups(entries: list[Entry], now_hhmm: str,
                     quotes: dict[str, float]) -> int:
    """對每筆已到期的追蹤時點補上價格。回傳補了幾格。

    只在「剛好到達或剛過」該時點時寫入,且不覆蓋已有值 ——
    否則盤中每輪都會把 5 分鐘那格改成最新價,追蹤就失去意義。
    """
    filled = 0
    for e in entries:
        px = quotes.get(e.stock_id)
        if px is None:
            continue
        elapsed = _mins_between(e.fired_at, now_hhmm)
        if elapsed is None or elapsed < 0:
            continue
        for m in FOLLOWUP_MINUTES:
            if str(m) in e.followup:
                continue
            if elapsed >= m:
                e.followup[str(m)] = round(float(px), 2)
                filled += 1
    return filled


def summarise(entries: list[Entry], minutes: int = 30) -> dict:
    """當日/區間彙總。**分開算 pushed 與未推播的**,才知道分級有沒有效。"""
    def stats(rows: list[Entry]) -> dict:
        vals = [e.excess_pct(minutes) for e in rows]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"n": len(rows), "measured": 0}
        wins = sum(1 for v in vals if v > 0)
        return {
            "n": len(rows), "measured": len(vals),
            "win_rate": round(wins / len(vals) * 100, 1),
            "avg_excess_pct": round(sum(vals) / len(vals), 3),
            "best": round(max(vals), 3), "worst": round(min(vals), 3),
        }

    pushed = [e for e in entries if e.pushed]
    held = [e for e in entries if not e.pushed]
    return {
        "minutes": minutes,
        "note": ("excess_pct 已扣除來回成本 —— 正值才代表這筆真的有賺頭。"
                 "pushed vs not_pushed 的差距就是訊號分級的價值。"),
        "pushed": stats(pushed),
        "not_pushed": stats(held),
        "long": stats([e for e in entries if e.side == "long"]),
        "short": stats([e for e in entries if e.side == "short"]),
    }
