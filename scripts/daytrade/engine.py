"""當沖層 orchestrator —— 盤前建池、盤中盯水位、風控提醒、台帳追蹤。

## 執行方式

    python -m scripts.daytrade.engine --pool            # 盤前:建今日標的池(多空兩欄)
    python -m scripts.daytrade.engine --watch           # 盤中:常駐盯水位 + 風控
    python -m scripts.daytrade.engine --scan            # 盤中:單次掃描(除錯用)
    python -m scripts.daytrade.engine --summary         # 收盤後:台帳彙總

## 與既有系統的關係

**完全隔離**。這支不 import `scoring` / `screener` / `main`,也不寫入
`docs/data.json` / `data/performance.json`。關掉 `config/daytrade.yaml` 的
`enabled` 就整層停用,既有盤後選股與盤中掃描一行都不受影響。

報價走 `scripts.quotes.get_quotes()`(三級降級:Sponsor 全市場 → MIS 逐檔 → 昨收),
所以**沒有訂閱時一樣能跑**,只是量比/均價是估計值,訊號會標記 degraded。
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path

import yaml

from ..config import DATA_DIR, now_tpe
from ..notify import send_discord
from ..quotes import get_quotes, in_trading_session, sponsor_status
from ..utils import log
from . import cost as C
from . import ledger as L
from . import risk as R
from . import signals as S
from . import universe as U

CFG_PATH = Path(DATA_DIR).parent / "config" / "daytrade.yaml"
OUT_DIR = DATA_DIR / "daytrade"
DOCS_DIR = Path(DATA_DIR).parent / "docs"

_COLOR = {"long": 0x22C55E, "short": 0xEF4444, "risk": 0xF59E0B, "critical": 0xDC2626}


def load_cfg() -> dict:
    try:
        return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"daytrade.yaml 讀取失敗,使用預設:{e}")
        return {}


# ─────────────────────────── 盤前:標的池 ───────────────────────────

def run_pool(today: date | None = None) -> dict:
    today = today or now_tpe().date()
    pool = U.build(today=today)
    U.save(pool, today)
    _write_web(pool)
    log.info(f"當沖標的池已產生:多 {len(pool['long'])} / 空 {len(pool['short'])}")
    return pool


def _write_web(pool: dict) -> None:
    """給網頁的當沖分頁。與 docs/data.json 分開,不污染既有前端資料。"""
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "daytrade.json").write_text(
            json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"docs/daytrade.json 寫檔失敗:{e}")


# ─────────────────────────── 盤中:水位與訊號 ───────────────────────────

def _levels_for(cand: dict, quote, cfg: dict, opening: dict) -> list[S.Level]:
    lv = cfg.get("levels", {}) or {}
    orng = opening.get(cand["stock_id"], {})
    return S.build_levels(
        prev_high=quote.high if quote else None,
        prev_low=quote.low if quote else None,
        open_range_high=orng.get("high"),
        open_range_low=orng.get("low"),
        vwap=(quote.vwap if (quote and lv.get("use_vwap", True)) else None),
        price=(quote.price if quote else 0) or 0,
        stock_id=cand["stock_id"],
        use_round=lv.get("use_round_numbers", True),
    )


def scan_once(pool: dict, state: dict, cfg: dict) -> dict:
    """一輪掃描。state 保存 prev_price / 開盤區間 / 已觸發過的 key。"""
    now = now_tpe()
    hhmm = now.strftime("%H:%M")
    cands = {c["stock_id"]: c for c in pool.get("long", []) + pool.get("short", [])}
    if not cands:
        return {"ok": False, "reason": "empty_pool"}

    ids = list(cands)
    quotes = get_quotes(ids)
    degraded = not sponsor_status().get("active")

    # 開盤區間:09:00~09:15 期間持續更新高低
    orm = int((cfg.get("levels", {}) or {}).get("opening_range_minutes", 15))
    opening = state.setdefault("opening", {})
    in_or = "09:00" <= hhmm <= f"09:{orm:02d}"
    for sid, q in quotes.items():
        if q.price is None:
            continue
        if in_or:
            o = opening.setdefault(sid, {"high": q.price, "low": q.price})
            o["high"] = max(o["high"], q.price)
            o["low"] = min(o["low"], q.price)

    buf = float((cfg.get("levels", {}) or {}).get("breakout_buffer", 0.002))
    prev = state.setdefault("prev_price", {})
    fired = state.setdefault("fired", {})
    cooldown = int((cfg.get("ranking", {}) or {}).get("cooldown_minutes", 20))
    last_push = state.setdefault("last_push", {})

    new_signals: list[S.Signal] = []
    for sid, cand in cands.items():
        q = quotes.get(sid)
        if not q or q.price is None:
            continue
        levels = _levels_for(cand, q, cfg, opening)
        crosses = S.detect_cross(prev_price=prev.get(sid), price=q.price,
                                 levels=levels, buffer_pct=buf)
        for lv, side in crosses:
            key = f"{sid}|{lv.kind}|{side}"
            if key in fired:
                continue                       # 當日去重
            # 同一檔冷卻:避免一檔在多條水位間反覆觸發洗版(價格在水位附近來回時,
            # 上穿觸發多、回落又觸發空,key 不同所以當日去重擋不住 —— 靠冷卻擋)。
            # ⚠️ 不能寫成 `(_mins(...) or 99)`:同一分鐘內 _mins 回 0,而 0 是 falsy,
            # `0 or 99` = 99 會讓冷卻整個失效(整合測試抓到的)。
            lp = last_push.get(sid)
            if lp is not None:
                gap = _mins(lp, hhmm)
                if gap is not None and gap < cooldown:
                    continue
            # 該方向在池子裡嗎?(空方要過 short_ok 閘門,池子已篩過)
            side_pool = pool.get(side, [])
            row = next((c for c in side_pool if c["stock_id"] == sid), None)
            if row is None:
                continue
            score, reasons = S.score_signal(
                kind=lv.kind, edge_ratio=row.get("edge_ratio"),
                volume_ratio=q.volume_ratio, cost_pct=row["cost_pct"],
                change_pct=q.change_pct, side=side)
            atr_abs = (row.get("atr_pct") or 0) / 100 * q.price
            new_signals.append(S.Signal(
                stock_id=sid, name=row.get("name") or q.name, side=side, kind=lv.kind,
                label=lv.label, price=q.price, level=lv.price,
                change_pct=q.change_pct, volume_ratio=q.volume_ratio,
                atr_pct=row.get("atr_pct"), cost_pct=row["cost_pct"],
                edge_ratio=row.get("edge_ratio"), score=score, reasons=reasons,
                degraded=degraded, fired_at=hhmm,
                borrow_fee_pct=row.get("borrow_fee_pct"),
                lots_affordable=row.get("lots_affordable", 0),
                suggested_stop=S.suggest_stop(price=q.price, side=side, atr=atr_abs),
                risk_per_lot=C.risk_per_lot(q.price, S.suggest_stop(
                    price=q.price, side=side, atr=atr_abs) or q.price),
            ))
            fired[key] = hhmm

    for sid, q in quotes.items():
        if q.price is not None:
            prev[sid] = q.price

    rk = cfg.get("ranking", {}) or {}
    push, held = S.rank_and_cap(new_signals,
                                push_top_n=int(rk.get("push_top_n", 3)),
                                min_score=float(rk.get("min_score", 55)))
    for s in push:
        last_push[s.stock_id] = hhmm

    # 台帳:推播與未推播都記(未推播的是對照組)
    day = now.date()
    entries = L.load(day)
    for s in push:
        entries = L.record(entries, s, pushed=True)
    for s in held:
        entries = L.record(entries, s, pushed=False)
    px_map = {sid: q.price for sid, q in quotes.items() if q.price is not None}
    L.update_followups(entries, hhmm, px_map)
    L.save(entries, day)

    if push:
        _push_signals(push)
    _risk_pass(state, cfg, px_map, day, hhmm)

    return {"ok": True, "checked": len(cands), "pushed": len(push),
            "held": len(held), "degraded": degraded}


def _mins(a: str, b: str) -> int | None:
    try:
        ha, ma = (int(x) for x in a.split(":"))
        hb, mb = (int(x) for x in b.split(":"))
        return (hb * 60 + mb) - (ha * 60 + ma)
    except Exception:
        return None


# ─────────────────────────── 風控 ───────────────────────────

def _risk_pass(state: dict, cfg: dict, quotes: dict, day: date, hhmm: str) -> None:
    positions = R.load_positions(day)
    if not positions:
        return
    for a in R.check_stops(positions, quotes):
        _push_risk(a)
    sent = set(state.setdefault("forced_sent", []))
    lvl = R.due_forced_close_level(hhmm, sent)
    if lvl:
        alert = R.forced_close_alert(positions, lvl, quotes)
        if alert:
            _push_risk(alert)
        # ⚠️ 送出 13:15 之後,要把 13:00 也一併標成已送 —— 否則下一輪會發現
        # 13:00「還沒送過」而倒退送一次比較不急的提醒(整合測試抓到的)。
        # 語意是「已經提醒到這個急迫度了」,不是「這一格送過了」。
        sent |= {t for t, _u, _w in R.FORCED_CLOSE_LEVELS if t <= lvl[0]}
        state["forced_sent"] = sorted(sent)


# ─────────────────────────── Discord ───────────────────────────

def _push_signals(sigs: list[S.Signal]) -> None:
    embeds = []
    for s in sigs:
        arrow = "▲ 做多" if s.side == "long" else "▼ 做空"
        fields = [
            {"name": "觸發", "value": f"{s.label} {s.level:g}", "inline": True},
            {"name": "現價", "value": f"**{s.price:g}**"
             + (f" ({s.change_pct:+.2f}%)" if s.change_pct is not None else ""), "inline": True},
            {"name": "分數", "value": f"{s.score:.0f}", "inline": True},
            {"name": "來回成本", "value": f"{s.cost_pct:.3f}%"
             + (f"(含借券 {s.borrow_fee_pct:g}%)" if s.borrow_fee_pct else ""), "inline": True},
            {"name": "空間", "value": (f"{s.edge_ratio:.1f}x 成本" if s.edge_ratio else "—"),
             "inline": True},
            {"name": "額度可買", "value": f"{s.lots_affordable} 張", "inline": True},
        ]
        if s.suggested_stop:
            fields.append({"name": "建議停損", "value":
                           f"{s.suggested_stop:g}(每張風險 {s.risk_per_lot:,.0f} 元)",
                           "inline": False})
        foot = "・".join(s.reasons[:3])
        if s.degraded:
            foot += " ｜⚠ 降級模式:量比/均價為估計值"
        embeds.append({
            "title": f"{arrow}｜{s.name}({s.stock_id})",
            "color": _COLOR[s.side],
            "fields": fields,
            "footer": {"text": f"{s.fired_at} ・ {foot}"[:2048]},
        })
    try:
        send_discord(embeds, content="**當沖訊號**")
    except Exception as e:
        log.warning(f"當沖訊號推播失敗:{e}")


def _push_risk(a: R.RiskAlert) -> None:
    """風控通知**一律單獨發一則** —— 不能被機會提醒的合併視窗吃掉。"""
    color = _COLOR["critical"] if a.urgency == "critical" else _COLOR["risk"]
    title = "🛑 停損觸及" if a.kind == "stop_hit" else "⏰ 當沖回補提醒"
    try:
        send_discord([{"title": title, "color": color,
                       "description": a.message[:4000]}],
                     content="@here" if a.urgency == "critical" else "")
    except Exception as e:
        log.warning(f"當沖風控推播失敗:{e}")


# ─────────────────────────── 盯盤迴圈 ───────────────────────────

def run_watch(until: str = "13:35") -> dict:
    cfg = load_cfg()
    if not cfg.get("enabled", True):
        log.info("當沖層在 config/daytrade.yaml 被關閉(enabled=false)。")
        return {"ok": False, "reason": "disabled"}
    today = now_tpe().date()
    pool_path = OUT_DIR / f"pool-{today.isoformat()}.json"
    pool = json.loads(pool_path.read_text(encoding="utf-8")) if pool_path.exists() else run_pool(today)

    sub = sponsor_status().get("active")
    sc = cfg.get("scan", {}) or {}
    interval = float(sc.get("interval_seconds_with_subscription" if sub
                            else "interval_seconds_no_subscription", 30))
    state: dict = {}
    polls = pushed = 0
    log.info(f"當沖盯盤啟動:每 {interval:g} 秒、到 {until}、"
             f"池 {len(pool.get('long', []))}多/{len(pool.get('short', []))}空")

    # 開盤後先量一次 MIS 真實延遲 —— 只有交易時段量得到,而且它直接決定
    # 「價位穿越推播」這一層值不值得做(見 latency.py)。量完寫進
    # docs/daytrade_latency.json,結論會顯示在網頁上。失敗不影響盯盤。
    measured = False
    while now_tpe().strftime("%H:%M") < until:
        t0 = time.time()
        try:
            if in_trading_session():
                if not measured:
                    measured = True
                    try:
                        from .latency import measure_from_pool
                        measure_from_pool(seconds=90, interval=3.0)
                    except Exception as e:
                        log.warning(f"MIS 延遲量測失敗(不影響盯盤):{e}")
                r = scan_once(pool, state, cfg)
                polls += 1
                pushed += r.get("pushed", 0)
        except Exception as e:
            log.warning(f"當沖單輪失敗(繼續):{e}")
        time.sleep(max(0.0, interval - (time.time() - t0)))
    log.info(f"當沖盯盤結束:輪詢 {polls} 次、推播 {pushed} 筆")
    return {"ok": True, "polls": polls, "pushed": pushed}


def run_summary(day: date | None = None) -> dict:
    day = day or now_tpe().date()
    entries = L.load(day)
    s = L.summarise(entries, minutes=30)
    log.info(f"當沖台帳 {day}:{json.dumps(s, ensure_ascii=False)}")
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="當沖層(獨立於既有選股系統)")
    ap.add_argument("--pool", action="store_true", help="盤前:建今日標的池")
    ap.add_argument("--watch", action="store_true", help="盤中:常駐盯水位")
    ap.add_argument("--scan", action="store_true", help="盤中:單次掃描(除錯)")
    ap.add_argument("--summary", action="store_true", help="收盤後:台帳彙總")
    ap.add_argument("--until", default="13:35")
    a = ap.parse_args()
    if a.pool:
        p = run_pool()
        print(json.dumps({"long": len(p["long"]), "short": len(p["short"]),
                          "stats": p["stats"]}, ensure_ascii=False))
    elif a.watch:
        print(json.dumps(run_watch(a.until), ensure_ascii=False))
    elif a.scan:
        today = now_tpe().date()
        pp = OUT_DIR / f"pool-{today.isoformat()}.json"
        pool = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else run_pool(today)
        print(json.dumps(scan_once(pool, {}, load_cfg()), ensure_ascii=False))
    elif a.summary:
        print(json.dumps(run_summary(), ensure_ascii=False))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
