"""盤前自動看盤 — 不碰盤後選股流程,獨立兩條早盤排程。

phase=preopen(約 08:45 台北):
  讀最近一次盤後核心10 + 各自 plan(決策卡價位線)→ 抓個股『試撮/預估開盤價』(TWSE MIS)
  + 美股隔夜閘門(SOX/NQ/VIX)+ ADR 佐證 → 把每檔分類為 A平盤 / B開高 / C開低 /
  ❌棄單(跳空過進場上限)/ ❌作廢(開盤即破停損),寄『盤前快報』。

phase=orb(約 09:25 台北):
  對(依真實開盤判定為)A 平盤 的股抓開盤 15 分 1分K → 算開盤區間高 ORH → 判斷 09:15 後
  是否出現『帶量突破 ORH』→ 寄『開盤15分 ORB 報告』,告訴你哪幾檔已可進場。

用 yfinance 歷史 1分K(非即時快照)做 ORB,Actions 延遲也能正確重建區間。
"""
from __future__ import annotations
import argparse
import json
import math

import pandas as pd

from .config import load_screeners, SIGNALS_DIR, DATA_DIR, now_tpe, assert_env
from .fetchers import (
    fetch_stock_info, fetch_mis_quotes, fetch_intraday_1m,
    fetch_market_gate, fetch_adr_changes,
)
from .notify import render_email, send_email
from .utils import log


# ---------- 共用 ----------

def _load_latest_picks() -> dict:
    """讀最近一次盤後選股(data/signals/<最新>.json)。核心已含 plan。"""
    files = sorted(SIGNALS_DIR.glob("*.json"))
    if not files:
        return {}
    with open(files[-1], "r", encoding="utf-8") as f:
        return json.load(f)


def _json_safe(o):
    """NaN/Inf → None(網頁 fetch().json() 會被裸字 NaN 整包搞死,沿用主流程的教訓)。"""
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return o


def _write_web_snapshot(phase: str, payload: dict, pick_date: str) -> None:
    """把該 phase 的結果寫進 docs/premarket.json(供網頁『盤中即時』分頁讀)。
    同一選股日的兩個 phase 各自更新自己那一段;換日則重置(丟掉昨天的另一段)。"""
    docs = DATA_DIR.parent / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "premarket.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict) or data.get("date") != pick_date:
        data = {"date": pick_date}
    data[phase] = payload
    path.write_text(json.dumps(_json_safe(data), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    log.info(f"docs/premarket.json 已更新({phase})")


def _market_map() -> dict[str, str]:
    """stock_id → 'twse'/'tpex'(MIS 頻道 tse_/otc_ 與 yfinance .TW/.TWO 都需要)。"""
    try:
        info = fetch_stock_info()
        if "type" in info.columns:
            return dict(zip(info["stock_id"].astype(str), info["type"]))
    except Exception as e:
        log.warning(f"stock_info 取市場別失敗,預設 twse:{e}")
    return {}


# ---------- 大盤閘門 ----------

def compute_gate(gate_data: dict, cfg_gate: dict) -> dict:
    """美股隔夜投票 → risk-on / 中性 / risk-off。"""
    sox = (gate_data.get("sox") or {}).get("pct")
    nq = (gate_data.get("nasdaq") or {}).get("pct")
    vix = (gate_data.get("vix") or {}).get("last")
    score = 0
    if sox is not None:
        score += 1 if sox >= cfg_gate.get("sox_riskon", 1.0) else (-1 if sox <= cfg_gate.get("sox_riskoff", -2.0) else 0)
    if nq is not None:
        score += 1 if nq >= cfg_gate.get("nq_riskon", 1.0) else (-1 if nq <= cfg_gate.get("nq_riskoff", -1.5) else 0)
    if vix is not None and vix >= cfg_gate.get("vix_high", 25):
        score -= 1
    label = "risk-on" if score >= 1 else ("risk-off" if score <= -1 else "中性")
    return {"label": label, "score": score,
            "sox": gate_data.get("sox"), "nasdaq": gate_data.get("nasdaq"),
            "sp500": gate_data.get("sp500"), "vix": gate_data.get("vix")}


# ---------- 個股分類(用試撮/開盤價套既有決策卡 plan)----------

def _scenario(price: float, plan: dict) -> str:
    """以價格對照 plan 的價位線判斷劇本。"""
    if price > plan["max_entry"]:
        return "skip_up"            # 跳空開高過進場上限 → 棄單
    if price <= plan["init_stop"]:
        return "skip_dn"            # 開盤即在停損下 → 作廢
    if price >= plan["gap_up_line"]:
        return "B"                  # 開高
    if price < plan["gap_dn_line"]:
        return "C"                  # 開低
    return "A"                      # 平盤


_SCEN_LABEL = {
    "A": "A 平盤", "B": "B 開高", "C": "C 開低",
    "skip_up": "❌ 棄單", "skip_dn": "❌ 作廢", "unknown": "？ 無資料",
}


def _action_text(scenario: str, plan: dict, gate_label: str) -> str:
    n = lambda v: "-" if v is None else f"{v:g}"
    if scenario == "A":
        return f"等開盤後 15 分,突破開盤區間高再進(交給 ORB 判)。停損 {n(plan.get('init_stop'))} / TP1 {n(plan.get('tp1'))}"
    if scenario == "B":
        return f"別市價追:回測昨收 {n(plan.get('ref'))} 不破再進(上限 {n(plan.get('max_entry'))});不回頭就放棄。"
    if scenario == "C":
        if gate_label == "risk-off":
            return f"大盤偏空:開低不接,今日放棄(破 {n(plan.get('init_stop'))} 更不用試)。"
        return f"別接刀:需站回開盤價 + 帶量吞噬、大盤沒同步崩才試;破 {n(plan.get('init_stop'))} 則放棄。"
    if scenario == "skip_up":
        return f"開盤估 {n(plan.get('ref'))} 之上跳空過進場上限 {n(plan.get('max_entry'))},R:R 破壞,今日不追。"
    if scenario == "skip_dn":
        return f"開盤估已在初始停損 {n(plan.get('init_stop'))} 下方,今日作廢。"
    return "盤前無報價,開盤後自行確認。"


def classify_preopen(pick: dict, quote: dict | None, gate_label: str) -> dict:
    """單檔盤前分類(純函式,可離線測)。"""
    plan = pick.get("plan") or {}
    price = (quote or {}).get("price")
    res = {
        "stock_id": pick.get("stock_id"), "name": pick.get("name", ""),
        "score": pick.get("score"), "profile": pick.get("profile"),
        "price": price, "src": (quote or {}).get("src"),
        "ref": plan.get("ref"), "plan": plan,
    }
    if not plan or price is None:
        res["scenario"] = "unknown"
    else:
        res["scenario"] = _scenario(float(price), plan)
        res["gap_pct"] = round((price / plan["ref"] - 1) * 100, 2) if plan.get("ref") else None
    res["scenario_label"] = _SCEN_LABEL[res["scenario"]]
    res["action"] = _action_text(res["scenario"], plan, gate_label)
    res["valid"] = res["scenario"] in ("A", "B", "C")
    return res


# ---------- 開盤 15 分 ORB ----------

def orb_decide(bars: pd.DataFrame, cfg_orb: dict) -> dict:
    """從當日 1分K 重建開盤區間(09:00–09:15)並判斷是否帶量突破區間高。純函式,可離線測。
    bars: index 為台北時區(或 naive 視為台北)、欄位 open/high/low/close/volume。"""
    rng_min = int(cfg_orb.get("range_minutes", 15))
    until = str(cfg_orb.get("confirm_until", "09:30"))
    vfilter = bool(cfg_orb.get("volume_filter", True))
    vmult = float(cfg_orb.get("volume_mult", 1.0))
    if bars is None or bars.empty or "close" not in bars.columns:
        return {"status": "no_data"}

    mins = [(ts.hour * 60 + ts.minute) for ts in bars.index]
    base = 9 * 60
    uh, um = (int(x) for x in until.split(":"))
    conf_end = uh * 60 + um

    rng = bars[[base <= m < base + rng_min for m in mins]]
    if rng.empty:
        return {"status": "no_data"}
    orh = float(rng["high"].max()); orl = float(rng["low"].min())
    or_vol = float(rng["volume"].mean()) if "volume" in rng.columns else None
    open_px = float(rng["open"].iloc[0])

    post = bars[[base + rng_min <= m <= conf_end for m in mins]]
    breakout = None
    for ts, row in post.iterrows():
        if float(row["close"]) > orh:
            vol = float(row.get("volume", 0) or 0)
            if vfilter and or_vol and vol < vmult * or_vol:
                continue                       # 突破但量不足 → 視為假突破,略過
            breakout = {"time": ts.strftime("%H:%M"), "price": round(float(row["close"]), 2),
                        "vol": int(vol)}
            break

    last_px = float(bars["close"].iloc[-1])
    res = {"orh": round(orh, 2), "orl": round(orl, 2), "open": round(open_px, 2),
           "or_vol": int(or_vol) if or_vol else None, "last": round(last_px, 2)}
    if breakout:
        res["status"] = "breakout"; res["breakout"] = breakout
    elif last_px < orl:
        res["status"] = "below_orl"
    else:
        res["status"] = "pending"
    return res


# ---------- 執行入口 ----------

def _picks_and_market(cfg: dict):
    snap = _load_latest_picks()
    core = snap.get("core") or []
    pm = (cfg.get("premarket") or {})
    if not core:
        return None, None, None, snap, pm
    mkt = _market_map()
    symbols = [(s["stock_id"], mkt.get(str(s["stock_id"]), "twse")) for s in core]
    return core, mkt, symbols, snap, pm


def run_preopen(test_mode: bool = False) -> None:
    cfg = load_screeners()
    core, mkt, symbols, snap, pm = _picks_and_market(cfg)
    if not core:
        log.info("盤前:找不到最近核心選股(data/signals 為空),略過。")
        return

    gate_data = fetch_market_gate()
    gate = compute_gate(gate_data, pm.get("gate", {}))
    adr = fetch_adr_changes(pm.get("adr", {}))
    quotes = fetch_mis_quotes(symbols)
    if not quotes:
        log.info("盤前:MIS 試撮全無資料(可能休市或 API 異常),略過寄信。")
        return

    rows = []
    for s in core:
        r = classify_preopen(s, quotes.get(str(s["stock_id"])), gate["label"])
        a = adr.get(str(s["stock_id"]))
        if a:
            r["adr"] = a
        rows.append(r)
    # 排序:有效的(A/B/C)在前,再依信心分
    order = {"A": 0, "B": 1, "C": 2, "skip_up": 3, "skip_dn": 4, "unknown": 5}
    rows.sort(key=lambda r: (order.get(r["scenario"], 9), -(r.get("score") or 0)))
    valid = [r for r in rows if r["valid"]]

    today = now_tpe()
    ctx = {
        "phase": "preopen", "test_mode": test_mode,
        "date_str": today.strftime("%Y-%m-%d (%a) %H:%M"),
        "pick_date": snap.get("date", ""),
        "gate": gate, "rows": rows,
        "valid_count": len(valid), "total": len(rows),
    }
    _write_web_snapshot("preopen", {
        "generated_at": today.strftime("%Y-%m-%d %H:%M"),
        "gate": gate, "rows": rows, "valid_count": len(valid), "total": len(rows),
    }, snap.get("date", ""))
    html = render_email("premarket_email.html", ctx)
    prefix = "[測試] " if test_mode else ""
    subject = f"{prefix}🌅 盤前快報 {today.strftime('%m/%d')} · 大盤{gate['label']} · 有效 {len(valid)}/{len(rows)} 檔"
    send_email(subject, html)
    log.info(f"盤前快報已寄:{gate['label']} · 有效 {len(valid)}/{len(rows)}")


def run_orb(test_mode: bool = False) -> None:
    cfg = load_screeners()
    core, mkt, symbols, snap, pm = _picks_and_market(cfg)
    if not core:
        log.info("ORB:找不到最近核心選股,略過。")
        return
    orb_cfg = pm.get("orb", {})

    results = []
    for s in core:
        plan = s.get("plan") or {}
        sid = str(s["stock_id"])
        bars = fetch_intraday_1m(sid, mkt.get(sid, "twse"))
        row = {"stock_id": sid, "name": s.get("name", ""), "score": s.get("score"),
               "profile": s.get("profile"), "plan": plan}
        if bars is None or bars.empty or not plan:
            row.update({"scenario": "unknown", "status": "no_data"})
            results.append(row)
            continue
        d = orb_decide(bars, orb_cfg)
        open_px = d.get("open")
        scenario = _scenario(float(open_px), plan) if (open_px is not None and plan) else "unknown"
        row["scenario"] = scenario
        row["scenario_label"] = _SCEN_LABEL.get(scenario, scenario)
        row.update(d)
        results.append(row)

    if all(r.get("status") == "no_data" for r in results):
        log.info("ORB:所有標的都抓不到 1分K(可能休市或 yfinance 未供),略過寄信。")
        return

    # 排序:已突破 > A 待突破 > 其他
    rank = {"breakout": 0, "pending": 1, "below_orl": 2, "no_data": 3}
    results.sort(key=lambda r: (rank.get(r.get("status"), 9), -(r.get("score") or 0)))
    fired = [r for r in results if r.get("status") == "breakout"]

    today = now_tpe()
    ctx = {
        "phase": "orb", "test_mode": test_mode,
        "date_str": today.strftime("%Y-%m-%d (%a) %H:%M"),
        "pick_date": snap.get("date", ""),
        "rows": results, "fired_count": len(fired),
        "orb_cfg": orb_cfg,
    }
    _write_web_snapshot("orb", {
        "generated_at": today.strftime("%Y-%m-%d %H:%M"),
        "rows": results, "fired_count": len(fired), "orb_cfg": orb_cfg,
    }, snap.get("date", ""))
    html = render_email("premarket_email.html", ctx)
    prefix = "[測試] " if test_mode else ""
    subject = f"{prefix}🔔 開盤15分 ORB {today.strftime('%m/%d')} · 已突破 {len(fired)} 檔"
    send_email(subject, html)
    log.info(f"ORB 報告已寄:已突破 {len(fired)} 檔")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--phase", choices=["preopen", "orb"], default="preopen",
                   help="preopen=盤前試撮分類;orb=開盤15分突破判斷")
    p.add_argument("--test", action="store_true", help="主旨加 [測試] 前綴")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    assert_env(require_finmind=False)   # 盤前不需要 FinMind,只需 Gmail
    if args.phase == "orb":
        run_orb(test_mode=args.test)
    else:
        run_preopen(test_mode=args.test)
