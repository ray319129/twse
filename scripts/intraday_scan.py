"""盤中全市場掃描 + 訊號提醒(2026-07-19)。

對標「起漲K線」那類軟體的核心:全市場自動盯盤、發動時通知。差別是我們用
FinMind Sponsor 的全市場即時快照(2852 檔 / 一次呼叫),而且規則全部寫在這裡看得見。

## 使用者最在意的一點:同一個訊號只提醒一次

「價格會上下來回波動,所以若是有的話提醒一次就好。」——
突破價位附近價格會反覆穿越,天真的實作會在 5 分鐘一次的輪詢裡連噴十幾封信。
這裡用三層防抖:

  1. **當日去重**:key = (日期, 股票, 訊號類型) 存進 `data/alerts/YYYY-MM-DD.json`,
     觸發過就永不再觸發。狀態存檔案不存記憶體 —— 每次輪詢都是新的 process。
  2. **突破緩衝**:要**超過**前高 `breakout_buffer`(預設 0.3%)才算,
     不是碰到就算。剛好貼著前高來回磨的不會觸發。
  3. **站上均價確認**:VWAP 是全日累計均價、移動很慢,
     `close > average_price` 過濾掉「衝一下就被打下來」的假突破。

## 訊號種類

**breakout(量增突破)** —— 起漲K線那類「飆股發動」:
    突破 20 日高 × 量比放大 × 站上均價 × 漲幅未過熱

**pullback(多頭回檔買點)** —— 你偏好的不追高型進場:
    多頭排列 × 回到月線附近 × 當日翻紅站回均價

⚠️ **這兩個都是動能型訊號,而台帳實測動能 profile 平均超額 -11.5pp(最差)。**
所以每一筆觸發都會寫進 `data/alerts/`,之後要能回頭算「這些提醒到底準不準」。
不要因為它會叫就以為它會賺 —— 見 HANDOFF 第 26 節。

## 為什麼要先建 levels

即時快照只有當下的價量,沒有均線/前高。每次輪詢重讀 860 個 parquet 太慢,
所以盤前跑一次 `--build-levels` 算好存成 `data/levels.parquet`,
盤中掃描只做一次 join。
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, now_tpe
from .quotes import fetch_snapshot_all, sponsor_status
from .storage import load_prices, price_path
from .utils import log

LEVELS_PATH = DATA_DIR / "levels.parquet"
ALERT_DIR = DATA_DIR / "alerts"
DOCS_DIR = DATA_DIR.parent / "docs"

# ---- 訊號參數(想調就改這裡;每一條都有理由,別亂鬆) ----
HIGH_LOOKBACK = 20        # 前高回看天數
BREAKOUT_BUFFER = 0.003   # 要超過前高 0.3% 才算突破(防貼著前高來回磨)
MIN_VOL_RATIO = 1.5       # 量比門檻:沒量的突破多半是假的
MAX_CHG = 8.0             # 漲超過這個就不提醒了 —— 追不到,而且是過熱區
MIN_CHG = 1.0             # 漲不到 1% 的「突破」通常只是雜訊
MIN_TURNOVER = 20_000_000 # 日均成交額門檻(元):太小的股票買不進也賣不掉
PULLBACK_NEAR = 0.03      # 回檔買點:距月線 3% 內算「回到均線附近」


# ---------- 盤前:算好均線/前高 ----------

def build_levels(min_turnover: float = MIN_TURNOVER) -> pd.DataFrame:
    """從本機 parquet 算每檔的均線/前高/均量,存成 data/levels.parquet。
    盤前跑一次即可(約 1~2 分鐘),盤中掃描直接 join,不必重讀 parquet。"""
    rows = []
    files = sorted(DATA_DIR.glob("prices/*.parquet"))
    for f in files:
        sid = f.stem
        try:
            df = load_prices(sid)
        except Exception:
            continue
        if df is None or len(df) < 60:
            continue
        c = df["close"]
        vol = df["volume"] if "volume" in df else None
        turnover = float((c.iloc[-20:] * vol.iloc[-20:]).mean()) if vol is not None else 0.0
        if turnover < min_turnover:
            continue
        rows.append({
            "stock_id": sid,
            "prev_close": float(c.iloc[-1]),
            "ma5": float(c.iloc[-5:].mean()),
            "ma10": float(c.iloc[-10:].mean()),
            "ma20": float(c.iloc[-20:].mean()),
            "ma60": float(c.iloc[-60:].mean()),
            # 前高不含今天(今天還在動),所以取到 -1 為止
            "high20": float(df["high"].iloc[-HIGH_LOOKBACK:].max()),
            "avg_turnover": turnover,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        log.warning("levels 建立失敗:沒有任何符合條件的股票。")
        return out
    LEVELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(LEVELS_PATH, index=False)
    log.info(f"levels 已建立:{len(out)} 檔(成交額門檻 {min_turnover/1e6:.0f}M)→ {LEVELS_PATH}")
    return out


def load_levels() -> pd.DataFrame:
    if not LEVELS_PATH.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(LEVELS_PATH)
    except Exception as e:
        log.warning(f"levels 讀取失敗:{e}")
        return pd.DataFrame()


# ---------- 已提醒狀態(當日去重) ----------

def _alert_path(day: str) -> Path:
    return ALERT_DIR / f"{day}.json"


def load_alerts(day: str) -> dict:
    p = _alert_path(day)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_alerts(day: str, data: dict) -> None:
    p = _alert_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 訊號判定 ----------

def _signals(row: dict) -> list[dict]:
    """單檔 → 觸發的訊號清單。回傳空 list 代表沒事。
    每個訊號帶 reason(給人看的理由),不要只給一個代號讓使用者猜。"""
    out = []
    px = row.get("close")
    vwap = row.get("average_price")
    vr = row.get("volume_ratio")
    chg = row.get("change_rate")
    if px is None or not px:
        return out

    above_vwap = vwap is not None and vwap > 0 and px > vwap
    vol_ok = vr is not None and vr >= MIN_VOL_RATIO
    chg_ok = chg is not None and MIN_CHG <= chg <= MAX_CHG

    # ① 量增突破:突破前高 + 量增 + 站上均價 + 漲幅未過熱
    h20 = row.get("high20")
    if h20 and px > h20 * (1 + BREAKOUT_BUFFER) and vol_ok and above_vwap and chg_ok:
        out.append({
            "type": "breakout",
            "label": "量增突破",
            "reason": (f"突破 {HIGH_LOOKBACK} 日高 {h20:.2f}(現價 {px:.2f},"
                       f"高出 {(px/h20-1)*100:.1f}%)・量比 {vr:.1f} 倍・站上均價 {vwap:.2f}"),
        })

    # ② 多頭回檔買點:多頭排列 + 回到月線附近 + 當日翻紅站回均價
    ma5, ma20, ma60 = row.get("ma5"), row.get("ma20"), row.get("ma60")
    if all(v for v in (ma5, ma20, ma60)) and ma5 > ma20 > ma60:
        near_ma20 = abs(px / ma20 - 1) <= PULLBACK_NEAR
        if near_ma20 and above_vwap and chg is not None and chg > 0:
            out.append({
                "type": "pullback",
                "label": "回檔買點",
                "reason": (f"多頭排列(5>20>60)・回到月線 {ma20:.2f} 附近"
                           f"(距 {(px/ma20-1)*100:+.1f}%)・當日翻紅站回均價"),
            })
    return out


def scan(dry_run: bool = False, notify: bool = True) -> dict:
    """跑一次盤中掃描。回傳 {ok, checked, new_alerts, alerts, reason}。
    **不 raise** —— 這是每 5 分鐘跑一次的背景工作,壞掉不該把 workflow 弄紅。"""
    now = now_tpe()
    day = now.strftime("%Y-%m-%d")

    st = sponsor_status()
    if not st.get("active"):
        log.info("非 Sponsor / 訂閱到期 —— 沒有全市場即時快照,盤中掃描跳過。")
        return {"ok": False, "reason": "no_sponsor", "checked": 0, "new_alerts": 0, "alerts": []}

    snap = fetch_snapshot_all(force=True)
    if snap.empty:
        return {"ok": False, "reason": "no_snapshot", "checked": 0, "new_alerts": 0, "alerts": []}

    stamp = str(snap["date"].iloc[0])[:10] if "date" in snap.columns else ""
    if stamp and stamp != day:
        log.info(f"快照時間戳 {stamp} ≠ 今天 {day}(非交易日或未開盤),掃描跳過。")
        return {"ok": False, "reason": f"stale:{stamp}", "checked": 0, "new_alerts": 0, "alerts": []}

    levels = load_levels()
    if levels.empty:
        log.warning("尚未建立 levels(先跑 --build-levels),盤中掃描跳過。")
        return {"ok": False, "reason": "no_levels", "checked": 0, "new_alerts": 0, "alerts": []}

    df = snap.merge(levels, on="stock_id", how="inner")
    fired = load_alerts(day)
    new = []
    for row in df.to_dict("records"):
        for sig in _signals(row):
            key = f"{row['stock_id']}|{sig['type']}"
            if key in fired:
                continue                       # ← 當日去重:同一檔同一種訊號只提醒一次
            rec = {
                "stock_id": row["stock_id"],
                "name": row.get("name") or "",
                "type": sig["type"], "label": sig["label"], "reason": sig["reason"],
                "price": row.get("close"), "change_rate": row.get("change_rate"),
                "volume_ratio": row.get("volume_ratio"), "vwap": row.get("average_price"),
                "fired_at": now.strftime("%H:%M"),
            }
            fired[key] = rec
            new.append(rec)

    if new and not dry_run:
        save_alerts(day, fired)
        _write_web(day, fired)
        if notify:
            _notify(day, new)

    log.info(f"盤中掃描 {now.strftime('%H:%M')}:比對 {len(df)} 檔,新觸發 {len(new)} 筆"
             f"(當日累計 {len(fired)} 筆)")
    return {"ok": True, "checked": len(df), "new_alerts": len(new),
            "alerts": new, "total_today": len(fired), "reason": ""}


def _write_web(day: str, fired: dict) -> None:
    """docs/alerts.json —— 網頁「今日提醒」讀這一包。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    items = sorted(fired.values(), key=lambda r: r.get("fired_at") or "")
    with open(DOCS_DIR / "alerts.json", "w", encoding="utf-8") as f:
        json.dump({"date": day, "updated": now_tpe().strftime("%H:%M"),
                   "alerts": items}, f, ensure_ascii=False)


def _notify(day: str, new: list[dict]) -> None:
    """寄信。只寄「這一輪新觸發」的,不重複整份清單 —— 否則等於又在洗版。"""
    try:
        from .notify import send_email
    except Exception as e:
        log.warning(f"通知模組載入失敗:{e}")
        return
    rows = "".join(
        f"<tr><td><b>{r['name']}</b> {r['stock_id']}</td>"
        f"<td>{r['label']}</td>"
        f"<td align='right'>{r['price']}</td>"
        f"<td align='right'>{r['change_rate']:+.2f}%</td>"
        f"<td style='color:#666'>{r['reason']}</td></tr>"
        for r in new
    )
    html = (f"<p>盤中訊號 {day} {now_tpe().strftime('%H:%M')} —— 新觸發 {len(new)} 筆"
            f"(同一檔同一種訊號當日只通知一次)</p>"
            f"<table cellpadding='6' style='border-collapse:collapse;font-size:14px'>"
            f"<tr style='background:#f0f0f0'><th>個股</th><th>訊號</th><th>現價</th>"
            f"<th>漲幅</th><th>理由</th></tr>{rows}</table>"
            f"<p style='color:#888;font-size:12px'>提醒:這是條件觸發,不是買進建議。"
            f"動能型訊號在本系統台帳的歷史超額為負(見 HANDOFF 第 26 節),"
            f"請自行判斷後再決定。</p>")
    try:
        send_email(f"[盤中訊號] {len(new)} 筆 · {now_tpe().strftime('%H:%M')}", html)
        log.info(f"已寄出盤中訊號通知({len(new)} 筆)")
    except Exception as e:
        log.warning(f"盤中訊號通知寄送失敗(不影響掃描):{e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="盤中全市場掃描 + 訊號提醒")
    ap.add_argument("--build-levels", action="store_true", help="盤前建立均線/前高快取")
    ap.add_argument("--dry-run", action="store_true", help="只顯示不寫檔不寄信")
    ap.add_argument("--no-notify", action="store_true", help="寫檔但不寄信")
    a = ap.parse_args()
    if a.build_levels:
        build_levels()
    else:
        r = scan(dry_run=a.dry_run, notify=not a.no_notify)
        print(json.dumps({k: v for k, v in r.items() if k != "alerts"}, ensure_ascii=False))
        for x in r.get("alerts", []):
            print(f"  {x['fired_at']} {x['label']} {x['name']}({x['stock_id']}) "
                  f"{x['price']} {x['change_rate']:+.2f}% — {x['reason']}")
