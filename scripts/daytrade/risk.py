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
# 時間點可由 config/daytrade.yaml 的 risk.forced_close_times 覆寫 ——
# 原本那個設定**完全沒被讀**,只是剛好與這裡的常數一致所以沒出事。
# (同一類的坑:risk.atr_stop_mult 設 1.0 但程式用 0.4,那次就真的發散了。)
_DEFAULT_FORCED_TIMES = ["13:00", "13:15", "13:25"]
_URGENCY = ["info", "warn", "critical"]
_WHY = ["還有 30 分鐘收盤", "剩 15 分鐘,建議開始平倉",
        "剩 5 分鐘 —— 沒平掉就會變成券差/交割"]


def _build_levels(times: list[str] | None = None) -> list[tuple[str, str, str]]:
    """把時間點清單配上急迫度與說明。最後一個一律 critical(那是最後通牒)。"""
    ts = [str(t) for t in (times or _DEFAULT_FORCED_TIMES) if str(t).count(":") == 1]
    if not ts:
        ts = list(_DEFAULT_FORCED_TIMES)
    ts = sorted(set(ts))
    out = []
    for i, t in enumerate(ts):
        last = (i == len(ts) - 1)
        urg = "critical" if last else (_URGENCY[min(i, 1)])
        why = _WHY[min(i, len(_WHY) - 1)] if len(ts) == len(_WHY) else (
            "收盤前必須平倉" if last else f"距收盤剩 {t} 之後的時間")
        out.append((t, urg, why))
    return out


def _load_forced_times() -> list[str] | None:
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(DATA_DIR).parent / "config" / "daytrade.yaml")
                             .read_text(encoding="utf-8")) or {}
        return (cfg.get("risk", {}) or {}).get("forced_close_times")
    except Exception:
        return None


FORCED_CLOSE_LEVELS = _build_levels(_load_forced_times())


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


def quota_usage(positions: list[Position], quota: float,
                per_trade_pct: float = 50.0) -> dict:
    """當日額度使用狀況。

    ## 為什麼是「當日累計」而不是「每筆」(使用者 2026-09-09 說明)

    > 我的額度為 25 萬,**一天內買賣超過 25 萬會無法成立**

    所以額度是**當日累計成交金額上限**,不是單筆上限 ——
    第一版把每筆都 sizing 到吃滿整個額度,你只要照著做第二筆就會被券商擋下來。
    而且**已平倉的部位一樣算進去**(它今天成交過了),所以這裡把 open 與 closed
    全部加總。

    `per_trade_pct`:單筆最多用掉剩餘額度的百分之多少。預設 50% 表示
    「留一半給下一個機會」,想單押就設 100。
    """
    used = sum((p.entry_price or 0) * (p.lots or 0) * 1000 for p in positions)
    remaining = max(0.0, (quota or 0) - used)
    return {
        "quota": round(quota or 0),
        "used": round(used),
        "used_pct": round(used / quota * 100, 1) if quota else None,
        "remaining": round(remaining),
        "per_trade_budget": round(remaining * (per_trade_pct / 100.0)),
        "per_trade_pct": per_trade_pct,
        "n_positions": len(positions),
        "note": ("當沖額度是**當日累計成交金額**上限,已平倉的也算 —— "
                 "所以剩餘額度會隨著今天做過的每一筆遞減。"),
    }


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
