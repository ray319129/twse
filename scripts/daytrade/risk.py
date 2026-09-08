"""當沖風控 —— 停損穿越與**強制回補提醒**。

## 為什麼強制回補提醒是整個當沖層最重要的東西

當沖最大的災難不是少賺,是**忘記平倉**:
現股當沖先賣後買若收盤沒買回 → 券差 → 券商代為借券 → **當日 15:30 前沒補款
就報違約交割**。那不是虧錢,是信用問題。

而且這一層的價值**完全不需要預測任何東西**,也**不受延遲影響** ——
13:00 提醒你手上還有 3 筆沒平,晚 30 秒毫無差別。
所以它是本層唯一「一定要推、不准被 Discord 合併視窗吃掉」的通知。

## 部位從哪來

使用者在網頁標記「我做了這筆」(復用既有 my_marks 的雲端同步機制),
寫進 `data/daytrade/positions-YYYY-MM-DD.json`。沒有標記就沒有部位 ——
系統不會、也不該去猜你實際下了什麼單。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

from ..config import DATA_DIR
from ..utils import log
from . import cost as C

POS_DIR = DATA_DIR / "daytrade"

# 強制回補提醒的時間點與急迫度。13:30 收盤,所以 13:25 是最後一次友善提醒。
FORCED_CLOSE_LEVELS = [
    ("13:00", "info", "還有 30 分鐘收盤"),
    ("13:15", "warn", "剩 15 分鐘,建議開始平倉"),
    ("13:25", "critical", "剩 5 分鐘 —— 沒平掉就會變成券差/交割"),
]


@dataclass
class Position:
    stock_id: str
    name: str
    side: str              # long / short
    entry_price: float
    lots: int
    stop_price: float | None = None
    opened_at: str = ""
    closed: bool = False

    @property
    def is_short(self) -> bool:
        return self.side == "short"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskAlert:
    kind: str              # stop_hit / forced_close
    urgency: str           # info / warn / critical
    stock_id: str
    name: str
    side: str
    message: str
    price: float | None = None
    unrealised_pct: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def positions_path(day: date) -> Path:
    return POS_DIR / f"positions-{day.isoformat()}.json"


def load_positions(day: date | None = None) -> list[Position]:
    day = day or date.today()
    p = positions_path(day)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = Position.__dataclass_fields__
        return [Position(**{k: v for k, v in row.items() if k in fields})
                for row in raw.get("positions", [])]
    except Exception as e:
        log.warning(f"當沖部位讀取失敗:{e}")
        return []


def save_positions(positions: list[Position], day: date | None = None) -> Path:
    day = day or date.today()
    POS_DIR.mkdir(parents=True, exist_ok=True)
    p = positions_path(day)
    p.write_text(json.dumps(
        {"date": day.isoformat(), "updated_at": datetime.now().isoformat(timespec="seconds"),
         "positions": [x.to_dict() for x in positions]}, ensure_ascii=False), encoding="utf-8")
    return p


def unrealised_pct(pos: Position, price: float) -> float | None:
    """未實現損益(%)。做空方向要反過來 —— 這裡寫錯會讓停損判斷整個顛倒。"""
    if not pos.entry_price or pos.entry_price <= 0 or not price or price <= 0:
        return None
    raw = (price / pos.entry_price - 1) * 100
    return round(-raw if pos.is_short else raw, 2)


def stop_hit(pos: Position, price: float) -> bool:
    """停損是否被觸及。多方跌破、空方漲破。"""
    if pos.stop_price is None or not price or price <= 0:
        return False
    return price <= pos.stop_price if not pos.is_short else price >= pos.stop_price


def check_stops(positions: list[Position], quotes: dict[str, float]) -> list[RiskAlert]:
    """對照即時價,挑出停損被觸及的部位。"""
    out: list[RiskAlert] = []
    for pos in positions:
        if pos.closed:
            continue
        px = quotes.get(pos.stock_id)
        if px is None:
            continue
        if stop_hit(pos, px):
            up = unrealised_pct(pos, px)
            out.append(RiskAlert(
                kind="stop_hit", urgency="critical", stock_id=pos.stock_id, name=pos.name,
                side=pos.side, price=px, unrealised_pct=up,
                message=(f"停損觸及:{pos.name}({pos.stock_id}) "
                         f"{'做多' if not pos.is_short else '做空'} "
                         f"進場 {pos.entry_price:g} → 現價 {px:g}"
                         f"({up:+.2f}%),停損設 {pos.stop_price:g}"),
            ))
    return out


def due_forced_close_level(now_hhmm: str, already_sent: set[str]) -> tuple[str, str, str] | None:
    """現在是否該送強制回補提醒。回傳 (時間點, 急迫度, 說明) 或 None。

    只送「已到時間且今天還沒送過」的最後一個 —— 排程延遲或重啟時不會一次補送三則。
    """
    due = [x for x in FORCED_CLOSE_LEVELS if x[0] <= now_hhmm and x[0] not in already_sent]
    return due[-1] if due else None


def forced_close_alert(positions: list[Position], level: tuple[str, str, str],
                       quotes: dict[str, float] | None = None) -> RiskAlert | None:
    """組出強制回補提醒。沒有未平倉部位就不吵人(回 None)。"""
    live = [p for p in positions if not p.closed]
    if not live:
        return None
    hhmm, urgency, why = level
    shorts = [p for p in live if p.is_short]
    quotes = quotes or {}
    lines = []
    for p in live:
        px = quotes.get(p.stock_id)
        up = unrealised_pct(p, px) if px else None
        lines.append(f"{p.name}({p.stock_id}) {'空' if p.is_short else '多'} "
                     f"{p.lots} 張 @{p.entry_price:g}"
                     + (f" 現 {px:g}({up:+.2f}%)" if px and up is not None else ""))
    msg = f"⏰ {hhmm} {why}｜未平倉 {len(live)} 筆\n" + "\n".join(lines)
    if shorts:
        # 空方漏平的後果比多方嚴重得多,講白。
        msg += (f"\n\n⚠️ 其中 {len(shorts)} 筆是**先賣後買**:收盤沒買回會變成券差,"
                f"券商代為借券後當日 15:30 前沒補款會**報違約交割**。")
    return RiskAlert(kind="forced_close", urgency=urgency, stock_id="", name="",
                     side="", message=msg)


def position_risk_summary(pos: Position, quota: float) -> dict:
    """單筆部位佔額度多少、風險金額多少。給網頁與推播卡用。"""
    notional = pos.entry_price * pos.lots * 1000
    risk = None
    if pos.stop_price:
        per_lot = C.risk_per_lot(pos.entry_price, pos.stop_price)
        risk = per_lot * pos.lots if per_lot else None
    return {
        "notional": round(notional),
        "quota_pct": round(notional / quota * 100, 1) if quota else None,
        "risk_amount": round(risk) if risk else None,
        "risk_pct_of_quota": round(risk / quota * 100, 2) if (risk and quota) else None,
    }
