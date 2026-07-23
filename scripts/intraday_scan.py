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
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, now_tpe
from .quotes import fetch_snapshot_all, sponsor_status
from .snapshot_archive import CHECKPOINTS, archive_snapshot
from .storage import load_prices, price_path
from .utils import log

LEVELS_PATH = DATA_DIR / "levels.parquet"
ALERT_DIR = DATA_DIR / "alerts"
DOCS_DIR = DATA_DIR.parent / "docs"

# ---- 訊號參數(想調就改這裡;每一條都有理由,別亂鬆) ----
HIGH_LOOKBACK = 20        # 前高回看天數
BREAKOUT_BUFFER = 0.003   # 要超過前高 0.3% 才算突破(防貼著前高來回磨)
MAX_CHG = 8.0             # 漲超過這個就不提醒了 —— 追不到,而且是過熱區
MIN_CHG = 1.0             # 突破:漲不到 1% 通常只是雜訊
MIN_CHG_PB = 0.5          # 回檔買點:至少要漲 0.5% 才算「翻紅」
MIN_TURNOVER = 20_000_000 # 日均成交額門檻(元):太小的股票買不進也賣不掉
PULLBACK_NEAR = 0.03      # 回檔買點:距月線 3% 內算「回到均線附近」

# 量比門檻改成「**相對當日全市場中位數**」而非固定值(2026-07-20 實測後修正)。
# 原因:7/20 全市場量比中位數只有 0.65(低量日),固定門檻 1.5 幾乎不可能觸發 ——
# 當天 30 筆回檔買點裡有 27 筆量比 < 1.0,等於完全沒有量能過濾,才會一次噴 25 封信。
# 用市場中位數當基準,低量日自動降門檻、爆量日自動升,同一套規則在不同盤況都成立。
# (這與專案既有的「因子驗證要先規模中性化」是同一個原則。)
VOL_MULT_BREAKOUT = 1.5   # 突破:量比 ≥ 市場中位數 × 此值
VOL_MULT_PULLBACK = 1.3   # 回檔:同上(回檔本來就不該爆量,門檻低一點)
VOL_FLOOR = 0.5           # 絕對下限:市場再冷,量比低於此就是真的沒人交易
MAX_ALERTS_PER_POLL = 8   # 單輪上限(見 scan 內註解:防冷啟動一次噴一整天的累積)

# 訊號信裡「開圖表」連結的網站位址。改網域就改這裡。
WEB_BASE = "https://twse-main.vercel.app"


# ---------- 盤前:算好均線/前高 ----------

def _static_extras() -> dict[str, dict]:
    """慢變動欄位:產業別、流通股數(換手率的分母)、月營收 YoY、EPS(近四季)。
    這些一天變不了幾次,盤前算一次存進 levels,`/api/quote` 直接 join —— 即時端點
    就不必為了顯示產業別去打 API,維持「一次呼叫換整份」的速度。

    流通股數用 **市值 ÷ 收盤價** 反推(TaiwanStockMarketValue 可 bulk,2717 檔一次呼叫)。
    2330 實測反推 259.3 億股,與公開股本相符。"""
    out: dict[str, dict] = {}
    # 產業別:用已經建好的 FinMind 產業鏈 primary
    try:
        sm = json.loads((DOCS_DIR / "sector_map.json").read_text(encoding="utf-8"))
        for sid, p in (sm.get("primary") or {}).items():
            out.setdefault(sid, {})["industry"] = f"{p[0]}／{p[1]}"
    except Exception as e:
        log.warning(f"產業別載入失敗:{e}")
    # 流通股數(bulk 一次)。⚠️ 假日/盤前查「今天」會回空 → 往回找最近一個有資料的交易日,
    # 否則週一盤前建檔時換手率會整批算不出來(踩過)。
    try:
        from datetime import timedelta
        from .fetchers import fetch_finmind
        d0 = now_tpe().date()
        for back in range(0, 7):
            rows = fetch_finmind("TaiwanStockMarketValue",
                                 start_date=(d0 - timedelta(days=back)).isoformat()) or []
            if rows:
                for r in rows:
                    sid = str(r.get("stock_id") or "")
                    mv = r.get("market_value")
                    if sid and mv:
                        out.setdefault(sid, {})["market_value"] = float(mv)
                log.info(f"市值取自 {(d0 - timedelta(days=back)).isoformat()}:{len(rows)} 檔")
                break
    except Exception as e:
        log.warning(f"市值載入失敗(換手率將無法計算):{e}")
    # 營收 / EPS:只有補抓過的股票有(核心+自選),沒有就留空,前端顯示「—」
    for sid_dir, key in (("revenue", "rev"), ("eps", "eps")):
        for f in (DATA_DIR / sid_dir).glob("*.parquet"):
            try:
                df = pd.read_parquet(f)
                if df.empty:
                    continue
                last = df.iloc[-1]
                d = out.setdefault(f.stem, {})
                if key == "rev":
                    d["revenue_yoy"] = round(float(last["revenue_yoy"]) * 100, 1) if pd.notna(last.get("revenue_yoy")) else None
                else:
                    d["eps_ttm"] = round(float(df["eps"].tail(4).sum()), 2) if len(df) >= 4 else None
                    d["eps_last"] = round(float(last["eps"]), 2) if pd.notna(last.get("eps")) else None
            except Exception:
                continue
    return out


def build_levels(min_turnover: float = MIN_TURNOVER) -> pd.DataFrame:
    """從本機 parquet 算每檔的均線/前高/均量,存成 data/levels.parquet。
    盤前跑一次即可(約 1~2 分鐘),盤中掃描直接 join,不必重讀 parquet。
    同時併入產業別/流通股數/營收/EPS(見 _static_extras)供即時報價顯示。"""
    extras = _static_extras()
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
        ex = extras.get(sid, {})
        mv = ex.get("market_value")
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
            "industry": ex.get("industry"),
            # 流通股數 = 市值 ÷ 收盤價(股)。換手率 = 成交張數×1000 ÷ 流通股數
            "shares": (mv / float(c.iloc[-1])) if mv else None,
            "revenue_yoy": ex.get("revenue_yoy"),
            "eps_ttm": ex.get("eps_ttm"),
            "eps_last": ex.get("eps_last"),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        log.warning("levels 建立失敗:沒有任何符合條件的股票。")
        return out
    LEVELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(LEVELS_PATH, index=False)

    # ⚠️ 也寫一份 docs/levels.json 給前端 —— **Vercel 的 Python 函式不會自動打包
    # repo 裡的資料檔**,`api/quote.py` 讀 data/levels.parquet 會靜靜讀不到,
    # 於是產業別/換手率/突破/回測/營收EPS 全部變成「—」(2026-07-19 實際踩到)。
    # 改成靜態檔由瀏覽器抓 + 前端 join:沒有打包問題,也不必依賴 Vercel 才能顯示。
    web = {}
    for r in out.to_dict("records"):
        sid = r["stock_id"]
        web[sid] = {k: (None if pd.isna(v) else (round(v, 4) if isinstance(v, float) else v))
                    for k, v in r.items() if k != "stock_id"}
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    p = DOCS_DIR / "levels.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"date": now_tpe().strftime("%Y-%m-%d"), "levels": web}, f, ensure_ascii=False)
    log.info(f"levels 已建立:{len(out)} 檔(成交額門檻 {min_turnover/1e6:.0f}M)"
             f"→ {LEVELS_PATH} + {p}({p.stat().st_size/1024:.0f} KB)")
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

def _f(v):
    """→ float | None。**NaN 在 Python 是 truthy**,`x or None` 擋不掉,
    這個坑在 quotes.py 踩過一次、2026-07-20 又在這裡踩第二次
    (訊號信的個股名稱全變成 "nan")。所有外部欄位一律走這裡。"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f          # NaN != NaN
    except (TypeError, ValueError):
        return None


def _s(v) -> str:
    """→ 乾淨字串。NaN / None / 'nan' 一律回空字串。"""
    if v is None or (isinstance(v, float) and v != v):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _signals(row: dict, vol_base: float) -> list[dict]:
    """單檔 → 觸發的訊號清單。回傳空 list 代表沒事。
    `vol_base` = 當日全市場量比中位數,量能門檻相對它而定(見參數區註解)。
    每個訊號帶 reason(給人看的理由),不要只給一個代號讓使用者猜。"""
    out = []
    px = _f(row.get("close"))
    vwap = _f(row.get("average_price"))
    vr = _f(row.get("volume_ratio"))
    chg = _f(row.get("change_rate"))
    if not px:
        return out

    above_vwap = vwap is not None and vwap > 0 and px > vwap
    thr_bo = max(VOL_FLOOR, vol_base * VOL_MULT_BREAKOUT)
    thr_pb = max(VOL_FLOOR, vol_base * VOL_MULT_PULLBACK)

    # ① 量增突破:突破前高 + 相對放量 + 站上均價 + 漲幅未過熱
    h20 = _f(row.get("high20"))
    if (h20 and px > h20 * (1 + BREAKOUT_BUFFER) and above_vwap
            and vr is not None and vr >= thr_bo
            and chg is not None and MIN_CHG <= chg <= MAX_CHG):
        out.append({
            "type": "breakout",
            "label": "量增突破",
            "reason": (f"突破 {HIGH_LOOKBACK} 日高 {h20:.2f}(現價 {px:.2f},"
                       f"高出 {(px/h20-1)*100:.1f}%)・量比 {vr:.2f}"
                       f"(市場中位 {vol_base:.2f})・站上均價 {vwap:.2f}"),
        })

    # ② 多頭回檔買點:多頭排列 + 回到月線附近 + 當日翻紅站回均價 + 有量
    ma5, ma20, ma60 = _f(row.get("ma5")), _f(row.get("ma20")), _f(row.get("ma60"))
    if ma5 and ma20 and ma60 and ma5 > ma20 > ma60:
        if (abs(px / ma20 - 1) <= PULLBACK_NEAR and above_vwap
                and chg is not None and chg >= MIN_CHG_PB
                and vr is not None and vr >= thr_pb):
            out.append({
                "type": "pullback",
                "label": "回檔買點",
                "reason": (f"多頭排列(5>20>60)・回到月線 {ma20:.2f} 附近"
                           f"(距 {(px/ma20-1)*100:+.1f}%)・翻紅 {chg:+.2f}% 站回均價"
                           f"・量比 {vr:.2f}(市場中位 {vol_base:.2f})"),
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

    stamp = snapshot_date(snap)
    if stamp and stamp != day:
        log.info(f"快照時間戳 {stamp} ≠ 今天 {day}(非交易日或未開盤),掃描跳過。")
        return {"ok": False, "reason": f"stale:{stamp}", "checked": 0, "new_alerts": 0, "alerts": []}

    levels = load_levels()
    if levels.empty:
        log.warning("尚未建立 levels(先跑 --build-levels),盤中掃描跳過。")
        return {"ok": False, "reason": "no_levels", "checked": 0, "new_alerts": 0, "alerts": []}

    df = snap.merge(levels, on="stock_id", how="inner")
    # 當日全市場量比中位數:量能門檻的基準(見參數區)。抓不到就退回 1.0(等同固定門檻)。
    try:
        vol_base = float(pd.to_numeric(df["volume_ratio"], errors="coerce").median())
        if vol_base != vol_base or vol_base <= 0:
            vol_base = 1.0
    except Exception:
        vol_base = 1.0

    # 大盤即時:用 merge 前的原始快照(merge 是 inner join levels,會把指數列濾掉)。
    # 每輪都算、每輪都寫檔 —— 這樣網頁上的大盤條跟訊號是同一個時間點的。
    if not dry_run:
        try:
            _write_pulse(day, market_pulse(snap))
        except Exception as e:
            log.warning(f"大盤即時寫檔失敗(不影響盯盤):{e}")

    names = _name_map()
    fired = load_alerts(day)
    first_poll = not fired            # 今天還沒有任何紀錄 = 這是第一輪
    new = []
    for row in df.to_dict("records"):
        for sig in _signals(row, vol_base):
            key = f"{row['stock_id']}|{sig['type']}"
            if key in fired:
                continue                       # ← 當日去重:同一檔同一種訊號只提醒一次
            sid = str(row["stock_id"])
            rec = {
                "stock_id": sid,
                # ⚠️ 快照的 name 欄實測只有 3.2% 有值,其餘是 **NaN(truthy!)** ——
                # 原本 `row.get("name") or ""` 擋不掉,7/20 信裡個股名稱全變 "nan"。
                "name": _s(row.get("name")) or names.get(sid, ""),
                "type": sig["type"], "label": sig["label"], "reason": sig["reason"],
                "price": _f(row.get("close")), "change_rate": _f(row.get("change_rate")),
                "volume_ratio": _f(row.get("volume_ratio")), "vwap": _f(row.get("average_price")),
                "fired_at": now.strftime("%H:%M"),
            }
            fired[key] = rec
            new.append(rec)

    # 冷啟動保護:job 若在盤中才啟動(排程延遲、手動觸發、中途重啟),第一輪會把
    # 「整個上午累積下來、當下仍符合條件」的股票一次全部觸發 —— 7/20 就一次寄了 25 筆。
    # 那不是 25 個新機會,是 3 小時的存量。所以只留當下最強的幾檔(依漲幅),
    # 其餘仍寫入去重表(避免稍後又逐筆補寄),但不通知。
    skipped = 0
    if len(new) > MAX_ALERTS_PER_POLL:
        new.sort(key=lambda r: -(r.get("change_rate") or -99))
        skipped = len(new) - MAX_ALERTS_PER_POLL
        new = new[:MAX_ALERTS_PER_POLL]
        log.info(f"單輪觸發 {skipped + len(new)} 筆超過上限"
                 f"{'(冷啟動:累積存量)' if first_poll else ''},只通知漲幅最強的 {len(new)} 筆")

    if new and not dry_run:
        save_alerts(day, fired)
        _write_web(day, fired)
        if notify:
            _notify(day, new, len(fired))

    log.info(f"盤中掃描 {now.strftime('%H:%M')}:比對 {len(df)} 檔,新觸發 {len(new)} 筆"
             f"(當日累計 {len(fired)} 筆)")
    return {"ok": True, "checked": len(df), "new_alerts": len(new),
            "alerts": new, "total_today": len(fired), "reason": ""}


# ---------- 盤中大盤即時(2026-07-21 加) ----------
#
# 在這之前**盤中完全沒有大盤資訊** —— 網頁上的市場氛圍/位階全來自 21:30 的盤後批次,
# 盤中看到的是昨天的。但台帳早就證明「動能是 beta 不是 alpha」
# (見專案記憶 abc-shipped-and-ceiling),所以盤中最該先看的其實是大盤方向與廣度。
#
# **關鍵:這一切零額外 API 呼叫。** 全市場快照本來每輪就抓回來了,
# ① FinMind 文件寫明 `data_id` 除了 4 碼個股也支援 **91 個 3 碼指數代號**
#    (`001`=加權指數、`101`=櫃買加權),不帶 data_id 的全市場回應裡本來就該有這些列;
# ② 漲跌家數/成交金額/量比中位數都是同一份 DataFrame 直接算得出來的。
INDEX_IDS = {"001": "加權指數", "101": "櫃買指數"}
_pulse_warned = [False]


def market_pulse(snap) -> dict:
    """大盤即時脈搏:指數 + 全市場廣度。純從既有快照算,不打任何 API。

    ⚠️ **指數列是否真的出現在「不帶 data_id」的回應裡,尚未實測**(2026-07-21 收盤後
    寫的,本機無 token)。沒有就只是 `indices` 為空、廣度照常顯示 —— 不會壞,
    但會 log 一次警告,方便第一個交易日確認。真的沒有的話改用免費的
    `TaiwanVariousIndicators5Seconds`(加權指數 5 秒級,免 Sponsor)。
    """
    out: dict = {"indices": {}, "breadth": {}}
    if snap is None or getattr(snap, "empty", True):
        return out
    try:
        ids = snap["stock_id"].astype(str)
        for code, label in INDEX_IDS.items():
            rows = snap[ids == code]
            if rows.empty:
                continue
            r = rows.iloc[0].to_dict()
            out["indices"][code] = {
                "name": label,
                "price": _f(r.get("close")),
                "change_rate": _f(r.get("change_rate")),
                "change_price": _f(r.get("change_price")),
                "total_amount": _f(r.get("total_amount")),
            }
        if not out["indices"] and not _pulse_warned[0]:
            _pulse_warned[0] = True
            log.warning("全市場快照裡找不到指數列(001/101)—— 大盤即時只會有廣度,"
                        "沒有指數。若要指數請改接 TaiwanVariousIndicators5Seconds。")
        # 廣度只算個股(4 碼),把指數列排除掉才不會污染家數
        stocks = snap[ids.str.len() >= 4]
        chg = pd.to_numeric(stocks.get("change_rate"), errors="coerce")
        up = int((chg > 0).sum())
        down = int((chg < 0).sum())
        flat = int((chg == 0).sum())
        vr = pd.to_numeric(stocks.get("volume_ratio"), errors="coerce").median()
        amt = pd.to_numeric(stocks.get("total_amount"), errors="coerce").sum()
        strong = int((chg >= 5).sum())
        weak = int((chg <= -5).sum())
        out["breadth"] = {
            "up": up, "down": down, "flat": flat, "total": up + down + flat,
            # 上漲家數占比:>55% 偏多、<45% 偏空。比指數漲跌更能看出「有沒有普漲」——
            # 權值股拉指數但多數股票在跌是很常見的陷阱。
            "up_pct": round(up / (up + down) * 100, 1) if (up + down) else None,
            "strong": strong,           # 漲逾 5%
            "weak": weak,               # 跌逾 5%
            "vol_ratio_median": None if vr != vr else round(float(vr), 2),
            "amount_100m": None if amt != amt else round(float(amt) / 1e8, 1),   # 億元
        }
    except Exception as e:
        log.warning(f"大盤即時計算失敗(不影響盯盤):{e}")
    return out


def _write_pulse(day: str, pulse: dict) -> None:
    """docs/pulse.json —— 網頁「盤中即時」頂端的大盤條讀這一包。
    每輪都寫(算它幾乎不花時間),但發布仍走 _git_publish 的 3 分鐘節流。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with open(DOCS_DIR / "pulse.json", "w", encoding="utf-8") as f:
        json.dump({"date": day, "updated": now_tpe().strftime("%H:%M:%S"), **pulse},
                  f, ensure_ascii=False)


def _write_web(day: str, fired: dict) -> None:
    """docs/alerts.json —— 網頁「今日提醒」讀這一包。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    items = sorted(fired.values(), key=lambda r: r.get("fired_at") or "")
    with open(DOCS_DIR / "alerts.json", "w", encoding="utf-8") as f:
        json.dump({"date": day, "updated": now_tpe().strftime("%H:%M"),
                   "alerts": items}, f, ensure_ascii=False)


# ---------- Discord 盤中通知(2026-07-21) ----------
#
# ⚠️ **編輯訊息不會推播。** 這是設計這段時最重要的一件事:Discord 的 PATCH 會把畫面
# 更新掉,但**不會發手機通知**。所以「整天只維護一則就地更新的訊息」聽起來很美,
# 實際上等於訊號全部靜音 —— 對盯盤來說是致命的。
#
# 所以採**合併視窗**:
#   · 新訊號距離上一則訊息超過 COALESCE_S → **發新訊息**(會推播,這是通知本身)
#   · 在視窗內 → **就地編輯上一則**,把新的併進去(不推播,但你剛剛才被通知過)
# 這樣既保住通知,又不會 12:31/12:32/12:33 連噴三則。
#
# 也刻意**不做「今日總覽」那則常駐訊息** —— 全天清單網頁已經有了(docs/alerts.json),
# 在 Discord 再維護一份只是兩處會不一致。
COALESCE_S = 150.0          # 合併視窗:2.5 分鐘內的新訊號併進上一則
DISCORD_MAX_CARDS = 8       # 一則訊息最多幾張卡(Discord 硬上限 10)
_COLORS = {"breakout": 0x22C55E, "pullback": 0x3B82F6}
_DEFAULT_COLOR = 0x6366F1
ALERT_NEWS_DAYS = 14
# 日K圖:一張約 40 KB,Discord 單則訊息附件上限 8 MB,理論上 8 張綽綽有餘。
# 真正的限制是**時間** —— 每張約 0.3~0.6 秒,8 張就是 5 秒,而輪詢間隔只有 10 秒。
# 所以只給前 CHART_TOP 檔(依漲幅排序後的最強幾檔)畫圖,其餘只有文字。
CHART_TOP = 4


def _state_path(day: str) -> Path:
    """Discord 訊息狀態。**不能塞進 alerts/{day}.json** —— 那個檔是
    `{訊號key: 紀錄}` 的扁平 dict,`.values()` 會被 _write_web 直接迭代,
    混一個 _meta 進去會變成一張壞掉的卡片。"""
    return ALERT_DIR / f"state-{day}.json"


def _load_state(day: str) -> dict:
    p = _state_path(day)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(day: str, st: dict) -> None:
    p = _state_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _fmt_cap(v) -> str:
    """市值:破千億就用「兆」,否則「億」。1234.5 億讀起來不如 1.23 兆直觀。"""
    if v is None:
        return ""
    return f"{v/10000:.2f} 兆" if v >= 10000 else f"{v:,.0f} 億"


def _embed(r: dict, ex: dict | None = None, chart_name: str | None = None) -> dict:
    """一筆訊號 → 一張 Discord embed 卡。左側色條編碼訊號類型。

    `ex` 是 alert_enrich.enrich() 的產出(產業/基本面/新聞),沒有就只顯示訊號本身。
    `chart_name` 是同一則訊息裡的日K附件檔名,用 `attachment://` 引用。
    """
    sid = r.get("stock_id") or ""
    chg = r.get("change_rate")
    px = r.get("price")
    ex = ex or {}
    fields = []
    if px is not None:
        fields.append({"name": "現價", "value": f"**{px:.2f}**"
                       + (f"  ({chg:+.2f}%)" if chg is not None else ""), "inline": True})
    if r.get("volume_ratio") is not None:
        fields.append({"name": "量比", "value": f"{r['volume_ratio']:.2f}", "inline": True})
    if r.get("vwap") is not None:
        fields.append({"name": "均價", "value": f"{r['vwap']:.2f}", "inline": True})

    # 估值:三個數擠一格,省 embed 的欄位配額(一列最多 3 個 inline field)
    val = []
    if ex.get("pe"):
        val.append(f"本益比 {ex['pe']:.1f}")
    if ex.get("pb"):
        val.append(f"股價淨值比 {ex['pb']:.2f}")
    if ex.get("yield"):
        val.append(f"殖利率 {ex['yield']:.2f}%")
    if val:
        fields.append({"name": "估值", "value": " · ".join(val), "inline": False})

    grow = []
    if ex.get("eps_ttm") is not None:
        grow.append(f"EPS(近四季) {ex['eps_ttm']:.2f}")
    if ex.get("revenue_yoy") is not None:
        grow.append(f"月營收年增 {ex['revenue_yoy']:+.1f}%")
    cap = _fmt_cap(ex.get("market_cap"))
    if cap:
        grow.append(f"市值 {cap}")
    if grow:
        fields.append({"name": "基本面", "value": " · ".join(grow), "inline": False})

    # 技術狀態(昨收):指標訊號標籤 + 共識K線型態。只顯示不進分(tech_signals.py 檔頭)。
    # ⚠️ 標明「昨收」—— 盤中看到時今天這根還沒收,別誤以為是現在的型態。
    _mark = {"bull": "▲", "bear": "▼", "warn": "◆"}
    ttags = [f"{_mark.get(x.get('s'), '')}{x.get('t')}"
             for x in (ex.get("tech_i") or []) + (ex.get("tech_p") or [])][:6]
    if ttags:
        fields.append({"name": "技術狀態(昨收)", "value": " · ".join(ttags)[:1024], "inline": False})

    news = ex.get("news") or []
    if news:
        # ⚠️ **標題不做超連結。** Google News RSS 的 link 是 base64 轉址網址,
        # 一條就 ~380 字 —— 三條新聞光網址就 1150 字,8 張卡直接撞爆 Discord 的
        # 6000 字總量上限(2026-07-21 實測 8583 字被回 400)。
        # 改成「純標題 + 一條短的搜尋連結」:標題本來就是用來掃的,想看內文再點進去。
        lines = [f"· {(n.get('title') or '')[:52]}"
                 + (f"　`{(n.get('date') or '')[5:]}`" if n.get("date") else "")
                 for n in news]
        lines.append(f"[更多 {sid} 新聞 →](https://news.google.com/search?q={sid}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant)")
        fields.append({"name": f"近 {ALERT_NEWS_DAYS} 日新聞", "value": "\n".join(lines)[:1024],
                       "inline": False})

    desc = r.get("reason") or ""
    if ex.get("business"):
        # 「這家在做什麼」放最前面 —— 使用者反映看到代號還要自己去查是誰
        desc = f"**{ex['business']}**\n{desc}"
    out = {
        "title": f"{r.get('name') or sid}  {sid}",
        "url": f"{WEB_BASE}/#stock={sid}",
        "description": desc[:900],
        "color": _COLORS.get(r.get("type"), _DEFAULT_COLOR),
        "author": {"name": r.get("label") or "訊號"},
        "fields": fields,
        "footer": {"text": f"首次觸發 {r.get('fired_at') or ''}"},
    }
    if chart_name:
        out["image"] = {"url": f"attachment://{chart_name}"}
    return out


def _build_cards(batch: list[dict]) -> tuple[list[dict], list[tuple[str, bytes]]]:
    """整批訊號 → (embeds, 附件檔案)。

    補背景資料與畫圖都可能失敗(沒網路 / 沒 matplotlib / 缺 parquet),
    **任何一步壞掉都只是少一段內容**,訊號本身照發 —— 這是通知,不是交易指令。
    """
    embeds, files = [], []
    # 只有最強的幾檔畫圖(見 CHART_TOP)。這裡排序只為決定「誰有圖」,
    # 卡片本身的顯示順序仍照觸發順序,不要打亂使用者的時間感。
    ranked = sorted(batch, key=lambda r: -(r.get("change_rate") or -99))
    with_chart = {id(r) for r in ranked[:CHART_TOP]}

    # ⚠️ **補資料一定要平行跑。** 每檔要打一次 Google News + 一次估值,序列跑 8 檔實測
    # 7.5 秒 —— 而輪詢間隔只有 10 秒,等於一個批次就吃掉一整輪。改成執行緒池後降到
    # 1~2 秒。這裡是純 I/O 等待,GIL 不影響。
    exmap: dict[str, dict] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        from .alert_enrich import enrich
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(batch)))) as pool:
            futs = {str(r.get("stock_id") or ""):
                    pool.submit(enrich, str(r.get("stock_id") or ""),
                                r.get("name") or "", r.get("price"))
                    for r in batch}
            for sid, f in futs.items():
                try:
                    exmap[sid] = f.result(timeout=12)
                except Exception as e:
                    log.info(f"訊號背景資料 {sid} 略過:{e}")
    except Exception as e:
        log.info(f"訊號背景資料整批略過:{e}")

    for r in batch:
        sid = str(r.get("stock_id") or "")
        ex = exmap.get(sid) or {}
        name = None
        if id(r) in with_chart:
            try:
                from .alert_chart import daily_k_png
                png = daily_k_png(sid, r.get("name") or "", live_price=r.get("price"),
                                  high20=ex.get("high20"))
                if png:
                    name = f"k_{sid}.png"
                    files.append((name, png))
            except Exception as e:
                log.info(f"日K圖 {sid} 略過:{e}")
        embeds.append(_embed(r, ex, name))
    # 最後一道保險:壓進 Discord 的 6000 字總量上限。超標時先砍新聞欄位
    # (訊號本身的價格/量比/理由才是決策要的),真的還是超標才整張卡不送。
    from .notify import fit_embeds
    embeds = fit_embeds(embeds, drop_fields=("近 ",))
    # 卡片被砍掉的話,對應的圖也不要上傳(附件對不到 embed 只是白佔頻寬)
    kept = {e.get("image", {}).get("url", "").replace("attachment://", "")
            for e in embeds if e.get("image")}
    files = [f for f in files if f[0] in kept]
    return embeds, files


def _discord_notify(day: str, new: list[dict], total_today: int) -> bool:
    """回傳 True 代表已由 Discord 通知(呼叫端就不必再寄 Email)。"""
    from .notify import send_discord, edit_discord, link_buttons, discord_enabled
    if not discord_enabled() or not new:
        return False
    st = _load_state(day)
    now_s = time.time()
    within = (st.get("msg_id") and (now_s - float(st.get("msg_ts") or 0)) < COALESCE_S)

    # 合併視窗內:把上一則的內容一起帶出來重畫(Discord 的 PATCH 是整包覆蓋,不是追加)
    batch = (st.get("batch") or []) + new if within else list(new)
    batch = batch[-DISCORD_MAX_CARDS:]
    embeds, files = _build_cards(batch)
    more = len(new) + len(st.get("batch") or []) - len(batch) if within else len(new) - len(batch)
    head = (f"**盤中訊號 · {now_tpe().strftime('%H:%M')}** —— 新觸發 **{len(new)}** 筆"
            f"(今日累計 {total_today} 筆)")
    if more > 0:
        head += f"　_另有 {more} 筆未顯示,見網頁_"
    head += "\n-# 條件觸發 ≠ 買進建議。動能型訊號在本系統台帳的歷史超額為負。"
    btns = link_buttons([("📊 開網頁看全部", f"{WEB_BASE}/#live")])

    if within:
        # 編輯時把整批的圖重新上傳,並用 attachments 宣告「現在只有這些」——
        # 舊附件會被移除,正好對應「整批重畫」的語意。
        if edit_discord(st["msg_id"], embeds, head, btns, files=files):
            # ⚠️ 刻意不更新 msg_ts —— 否則連續有訊號時視窗會無限延後,
            # 變成整個下午都在編輯同一則、完全不再推播。
            st["batch"] = batch
            _save_state(day, st)
            log.info(f"Discord 訊號已併入上一則({len(new)} 筆,不重複推播)")
            return True
        # 編輯失敗(訊息被刪)→ 掉下去發新的
    mid = send_discord(embeds, head, btns, files=files)
    if not mid:
        # 有設 webhook 卻送不出去 = 真的有問題,不能只是安靜地改寄 Email。
        # 上面 _post 已經把 Discord 回應的 body 印出來了,對照那行就知道是哪個欄位。
        log.warning(f"Discord 送出失敗({len(embeds)} 張卡、{len(files)} 張圖),改用 Email。")
        return False
    _save_state(day, {"msg_id": mid, "msg_ts": now_s, "batch": batch})
    log.info(f"Discord 已推送盤中訊號({len(new)} 筆)")
    return True


def _notify(day: str, new: list[dict], total_today: int = 0) -> None:
    """通知。**優先走 Discord**(2026-07-21 起),沒設 webhook 才退回 Email。
    只通知「這一輪新觸發」的,不重複整份清單 —— 否則等於又在洗版。"""
    try:
        from .notify import send_email
    except Exception as e:
        log.warning(f"通知模組載入失敗:{e}")
        return
    # Discord 成功就結束;失敗(沒設 webhook / API 掛掉)自動掉到下面的 Email。
    # 通知管道是「有比沒有好」,不該因為換管道就變成完全收不到。
    try:
        if _discord_notify(day, new, total_today or len(new)):
            return
    except Exception as e:
        log.warning(f"Discord 通知失敗,改用 Email:{e}")
    # 每檔補上「決策要用到的背景」:產業、量能、法人/分點、基本面、與均線的距離。
    # 使用者反映原本的信「資訊不太夠」—— 只給代號和理由,還要自己去查是誰、做什麼、貴不貴。
    lv = load_levels()
    lvmap = {str(r["stock_id"]): r for r in lv.to_dict("records")} if not lv.empty else {}
    streaks = {}
    try:
        streaks = json.loads((DOCS_DIR / "branch_streak.json").read_text(encoding="utf-8")).get("streaks", {})
    except Exception:
        pass

    def _card(r: dict) -> str:
        sid = r["stock_id"]
        L = lvmap.get(sid, {})
        px = r.get("price")
        up = (r.get("change_rate") or 0) >= 0
        col = "#0b7a44" if up else "#c42a30"
        bits = []
        ind = L.get("industry")
        if ind and ind == ind:
            bits.append(f"<b>{ind}</b>")
        for lab, key, fmt in (("營收YoY", "revenue_yoy", "{:+.1f}%"),
                              ("EPS近四季", "eps_ttm", "{:.2f}")):
            v = L.get(key)
            if v is not None and v == v:
                bits.append(f"{lab} {fmt.format(v)}")
        st = streaks.get(sid)
        if st and st.get("streak"):
            bits.append(f"分點連{'買' if st['dir']=='buy' else '賣'} {st['streak']} 日")
        # 與均線的相對位置 —— 判斷「這是起漲還是追高」的關鍵脈絡
        mas = []
        for lab, key in (("月線", "ma20"), ("季線", "ma60")):
            m = L.get(key)
            if m and m == m and px:
                mas.append(f"{lab} {m:.2f}({px/m-1:+.1%})")
        h20 = L.get("high20")
        if h20 and h20 == h20 and px:
            mas.append(f"20日高 {h20:.2f}({px/h20-1:+.1%})")
        vwap, vr = r.get("vwap"), r.get("volume_ratio")
        vol = []
        if vwap:
            vol.append(f"均價 {vwap:.2f}")
        if vr:
            vol.append(f"量比 {vr:.2f}")
        return (
            f"<div style='border:1px solid #e6eaf0;border-left:3px solid {col};"
            f"border-radius:10px;padding:12px 14px;margin:10px 0'>"
            f"<div style='font-size:15px;font-weight:800'>{r['name'] or sid} "
            f"<span style='color:#888;font-weight:500;font-size:13px'>{sid}</span>"
            f"<span style='background:#eef0ff;color:#4a52e6;border-radius:20px;"
            f"padding:2px 9px;font-size:11px;margin-left:8px'>{r['label']}</span></div>"
            f"<div style='font-size:20px;font-weight:800;color:{col};margin:5px 0'>"
            f"{px} <span style='font-size:14px'>{r['change_rate']:+.2f}%</span></div>"
            + (f"<div style='font-size:12px;color:#555'>{' ・ '.join(bits)}</div>" if bits else "")
            + (f"<div style='font-size:12px;color:#555;margin-top:3px'>{' ・ '.join(mas)}</div>" if mas else "")
            + (f"<div style='font-size:12px;color:#555;margin-top:3px'>{' ・ '.join(vol)}</div>" if vol else "")
            + f"<div style='font-size:12px;color:#888;margin-top:5px'>{r['reason']}</div>"
            f"<div style='margin-top:8px;font-size:12px'>"
            f"<a href='{WEB_BASE}/#stock={sid}' style='color:#4a52e6;text-decoration:none'>"
            f"開圖表 / 分K →</a>"
            f"<a href='https://tw.stock.yahoo.com/quote/{sid}' style='color:#888;"
            f"text-decoration:none;margin-left:12px'>Yahoo 股市 ↗</a></div></div>")

    html = (f"<div style='font-family:-apple-system,\"Segoe UI\",sans-serif;max-width:620px'>"
            f"<p style='font-size:13px;color:#555'>盤中訊號 {day} {now_tpe().strftime('%H:%M')}"
            f" —— 新觸發 <b>{len(new)}</b> 筆(同一檔同一種訊號當日只通知一次)</p>"
            + "".join(_card(r) for r in new)
            + f"<p style='color:#888;font-size:11.5px;line-height:1.6'>提醒:這是條件觸發,"
            f"不是買進建議。動能型訊號在本系統台帳的歷史超額為負(見 HANDOFF 第 26 節),"
            f"請自行判斷後再決定。</p></div>")
    try:
        send_email(f"[盤中訊號] {len(new)} 筆 · {now_tpe().strftime('%H:%M')}", html)
        log.info(f"已寄出盤中訊號通知({len(new)} 筆)")
    except Exception as e:
        log.warning(f"盤中訊號通知寄送失敗(不影響掃描):{e}")


# ---------- 常駐輪詢(取代「每 5 分鐘開一個新 job」) ----------
#
# 為什麼要常駐:每 5 分鐘開一個 GitHub Actions run,光 checkout + pip install 就要
# 40~90 秒,**大部分時間在裝環境不是在掃描**,而且開太密會排隊。改成一個 job 從
# 08:55 跑到 13:35(4.5 小時 < Actions 單一 job 6 小時上限),環境只裝一次,
# 之後純粹輪詢 —— 間隔想多短就多短。
#
# ⚠️ 真正的上限不是 API 額度,是**上游快照多久更新一次**。這個還沒量過
# (見 `--measure-freshness`)。上游若 60 秒才換一次,你每 5 秒問一次也只是拿到
# 同一份資料 —— 快的是你問的頻率,不是資料。量完再定 interval。

def intraday_series(stock_ids: list[str], day: str | None = None,
                    step: int = 3, max_pts: int = 100) -> dict:
    """當日走勢線資料(給卡片上的迷你走勢圖)。

    用 `TaiwanStockKBar` 1 分 K —— 2330 實測一天 266 筆完整 OHLCV,一檔一次呼叫。
    **刻意不用「自己每 20 秒累積」**:盯盤 job 中途重啟或晚開,自累的線就會缺一段;
    KBar 是回溯完整的,任何時候抓都拿得到 09:00 到現在的全部。

    輸出每檔 `{t:[分鐘], c:[收盤], v:[均價], prev:昨收}`,降頻到每 `step` 分鐘、
    最多 `max_pts` 點 —— 卡片上的圖只有 40px 高,270 個點畫上去是浪費也看不出差別。

    均價線 v 用累計成交額 ÷ 累計量算(等同當日 VWAP),用來判斷「站上均價沒」。
    """
    from .fetchers import fetch_finmind
    day = day or now_tpe().strftime("%Y-%m-%d")
    out = {}
    for sid in stock_ids:
        try:
            rows = fetch_finmind("TaiwanStockKBar", data_id=sid,
                                 start_date=day, end_date=day) or []
            if len(rows) < 2:
                continue
            rows.sort(key=lambda r: str(r.get("minute") or ""))
            ts, cs, vs = [], [], []
            cum_amt = cum_vol = 0.0
            for i, r in enumerate(rows):
                c = float(r.get("close") or 0)
                vol = float(r.get("volume") or 0)
                if c <= 0:
                    continue
                cum_amt += c * vol
                cum_vol += vol
                if i % step and i != len(rows) - 1:
                    continue                      # 降頻,但最後一點一定保留(=最新價)
                ts.append(str(r.get("minute") or "")[:5])
                cs.append(round(c, 2))
                vs.append(round(cum_amt / cum_vol, 2) if cum_vol else None)
            if len(cs) < 2:
                continue
            if len(cs) > max_pts:                 # 太長就等距抽樣,保留頭尾
                idx = [round(i * (len(cs) - 1) / (max_pts - 1)) for i in range(max_pts)]
                ts, cs, vs = [ts[i] for i in idx], [cs[i] for i in idx], [vs[i] for i in idx]
            out[sid] = {"t": ts, "c": cs, "v": vs}
        except Exception as e:
            log.warning(f"走勢線 {sid} 失敗(略過):{e}")
    # ⚠️ **KBar 在盤中到底更不更新,尚未實測**(2026-07-21)。
    # FinMind 官方文件把 TaiwanStockKBar 的「更新時間」寫成**平日 15:50**(收盤後),
    # 若真的是收盤後才更新,盤中的走勢線會整條停在前一天 —— 而網頁上完全看不出來,
    # 只會覺得「線很短」。所以這裡自己驗:最後一根 K 的時間比現在落後超過 15 分鐘
    # 就記警告,並把落後秒數寫進 series.json 給前端標示。
    lag_min = None
    if out:
        try:
            last = max((v["t"][-1] for v in out.values() if v.get("t")), default="")
            if last:
                hh, mm = (int(x) for x in last.split(":")[:2])
                n = now_tpe()
                lag_min = (n.hour * 60 + n.minute) - (hh * 60 + mm)
                if 0 <= (n.hour * 60 + n.minute) - 540 and lag_min > 15:
                    log.warning(f"⚠️ 走勢線最後一根 K 是 {last},落後 {lag_min} 分鐘 —— "
                                f"KBar 很可能盤中不更新(官方文件寫更新時間 15:50)。"
                                f"若確認如此,走勢線要改用自己累積快照的方式。")
        except Exception:
            pass
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "series.json").write_text(
            json.dumps({"date": day, "updated": now_tpe().strftime("%H:%M"),
                        "lag_min": lag_min, "series": out},
                       ensure_ascii=False), encoding="utf-8")
        log.info(f"走勢線已更新:{len(out)} 檔")
    return out


_deep_last = [0.0]      # 內外盤上次計算時間(list 是為了在 loop 裡可變)
_pub_last = [0.0]       # 上次 git push 時間(發布節流用)
PUBLISH_EVERY = 180.0   # 最快每 3 分鐘 push 一次(每次 push = 一次 Vercel 部署)


def deep_metrics(stock_ids: list[str], day: str | None = None) -> dict:
    """**真正的內外盤比** —— 只有逐筆資料算得出來,快照給不了。

    快照的 `TickType` 只是「最後一筆」的方向,不是全日累計;要算內外盤比得把
    `TaiwanStockPriceTick` 的每一筆按方向加總(TickType 1=內盤/賣方成交、2=外盤/買方成交)。

    ⚠️ 成本很不一樣:一檔一天 2 萬多筆(2330 實測 20,922),**不能塞進 20 秒的輪詢**,
    所以只對「你真的在看的股票」每隔幾分鐘算一次。

    ⚠️⚠️ **方向定義(2026-07-20 修正,原本是反的)**:
    `TickType=1 → 外盤(買方主動)`、`TickType=2 → 內盤(賣方主動)`。

    驗證方法:取 7/17(全面下跌日)6 檔股票,比對「TickType 佔比」與「價格上行成交量佔比」——
    6 檔**全部**是 TickType=2 佔多數(57~75%),若 2 是外盤就等於「全市場下跌但買方主導」,
    不合理;且 TickType=1 的佔比與價漲量佔比明顯同向(2330 28.4% vs 25.3%、
    2603 42.7% vs 42.0%)。原本寫反會讓外盤比整個顛倒,使用者盤中看到的都是錯的。
    """
    from .fetchers import fetch_finmind
    day = day or now_tpe().strftime("%Y-%m-%d")
    out = {}
    for sid in stock_ids:
        try:
            rows = fetch_finmind("TaiwanStockPriceTick", data_id=sid,
                                 start_date=day, end_date=day) or []
            if not rows:
                continue
            outer = sum(float(r.get("volume") or 0) for r in rows if str(r.get("TickType")) == "1")
            inner = sum(float(r.get("volume") or 0) for r in rows if str(r.get("TickType")) == "2")
            tot = outer + inner
            if tot <= 0:
                continue
            out[sid] = {"outer": outer, "inner": inner,
                        "outer_ratio": round(outer / tot * 100, 1), "ticks": len(rows)}
        except Exception as e:
            log.warning(f"內外盤 {sid} 失敗(略過):{e}")
    if out:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "deep.json").write_text(
            json.dumps({"date": day, "updated": now_tpe().strftime("%H:%M"), "metrics": out},
                       ensure_ascii=False), encoding="utf-8")
        log.info(f"內外盤比已更新:{len(out)} 檔")
    return out


def _deep_targets() -> list[str]:
    """要算內外盤的股票 = 自選池 + 今日核心。刻意不是全市場 —— 逐筆資料太重。"""
    ids = set()
    try:
        wl = json.loads((DATA_DIR.parent / "config" / "watchlist.json").read_text(encoding="utf-8"))
        ids |= set((wl.get("stocks") or {}).keys())
    except Exception:
        pass
    try:
        data = json.loads((DOCS_DIR / "data.json").read_text(encoding="utf-8"))
        for s in (data.get("core") or []):
            if s.get("stock_id"):
                ids.add(str(s["stock_id"]))
    except Exception:
        pass
    return sorted(ids)


def _git_publish(msg: str) -> None:
    """把 alerts / 走勢線 / 內外盤 / freshness 推上去(網頁才看得到)。失敗只記 log ——
    推不上去不該中斷盯盤,Email 才是主要通知管道。

    ⚠️ **每次 push 都會觸發一次 Vercel 重新部署**。7/20 實測 20 分鐘內推了 8 次
    = 8 次部署,既浪費也可能撞到平台限制 → 改由呼叫端節流(見 loop 的 _pub_last)。

    ⚠️ **`data/snapshots` 一定要在這份清單裡**(2026-07-21 修)。原本只有 workflow
    的最後一步會 commit 它 —— 意思是 job 被取消 / timeout / runner 掛掉,**整天 7 個
    檢查點的快照就永遠沒了**。而鐵則三說得很清楚:這種資料訂閱到期後沒有任何 API
    補得回來,今天沒存就是沒有。所以改成「存一個推一個」,不等 job 結束。
    `docs/levels.json` 同理:盤中自動重建的 levels 不推上去,網頁整天拿不到
    產業別/換手率/突破判斷。
    """
    # ⚠️ **絕對不要用 `git pull --rebase`**(2026-07-22 修,踩了一整個上午)。
    # 舊版:commit → pull --rebase → push,每步 check=False。當**別的來源**在盤中
    # 推了 main(我自己的開發 push、premarket 的 snapshot commit),watch 的 rebase 會
    # 撞到衝突並**停在 conflicted 狀態**。之後每一次 `git commit` 都會失敗
    # (「cannot commit during a rebase」)→ 從第一次衝突起整個 job 就 git-死了,
    # 還在掃描但再也發不出去。7/22 實測:freshness.json 整天 404、網頁全程停在昨天。
    #
    # 新版用「軟重置到遠端頂點再重新 parent」的機器人附加寫法,**結構上不可能衝突**:
    #   fetch → reset --soft origin/main(把分支指標移到遠端最新,working tree 不動)
    #   → add → commit → push。我們的資料檔永遠是單一 commit 疊在最新 main 上,
    #   即使遠端在這期間又前進,下一輪 fetch 會再接上,不累積、不卡死。
    # ⚠️ **絕不用 capture_output 把 git 輸出吞掉**(2026-07-22 第二次踩,教訓同 Discord 400)。
    # 上一版把 git stdout/stderr 都 capture 起來、只在失敗時 log —— 結果 7/22 整天沒發布
    # 卻**一行錯誤都沒有**,完全無法診斷。這版每一步的 returncode + stderr 都主動 log 出來
    # (errors='replace' 防 locale 解碼炸掉),寧可吵一點也不要再瞎。
    import subprocess

    def _git(*args):
        r = subprocess.run(["git", *args], check=False, capture_output=True,
                           text=True, errors="replace")
        if r.returncode != 0 and args[0] not in ("rebase", "merge", "diff"):
            # rebase/merge --abort 沒東西可 abort 會非零,是預期的;diff --quiet 用 returncode 當訊號
            log.info(f"git {args[0]} rc={r.returncode}: {(r.stderr or r.stdout or '').strip()[:180]}")
        return r

    try:
        # ⚠️ **只 add 存在的路徑**(2026-07-22 這就是整天沒發布的真兇)。
        # `git add A B C` 是**原子**的:只要有任何一個 pathspec 對不到檔案(例如
        # data/snapshots 在第一個檢查點前還不存在、docs/deep.json 在第一次內外盤計算前
        # 還沒生成),整個 git add 會 fatal rc=128 且**一個檔都不 stage** → diff 空 → 靜默略過。
        # 這個坑在 7/21 把 data/snapshots 加進清單那刻就埋下了(7/20 能發是因為當時清單裡
        # 每個路徑都存在)。所以每輪先過濾掉不存在的路徑。
        _all = ["data/alerts", "data/snapshots", "docs/alerts.json", "docs/levels.json",
                "docs/pulse.json", "docs/deep.json", "docs/series.json", "docs/freshness.json"]
        _root = DATA_DIR.parent
        files = [f for f in _all if (_root / f).exists()]
        if not files:
            return                                          # 還沒有任何可發布的檔
        # 萬一上一輪殘留了 rebase/merge 狀態,先清乾淨(防禦性)
        _git("rebase", "--abort")
        _git("merge", "--abort")
        for attempt in range(3):
            f = _git("fetch", "origin", "main")
            if f.returncode != 0:
                log.warning(f"發布 fetch 失敗(第 {attempt+1} 次):{(f.stderr or '').strip()[:180]}")
                continue
            # --mixed 移動分支指標到遠端最新、保留 working tree 的資料檔改動。
            # 用 FETCH_HEAD:`git fetch origin main` 一定設它,但不保證更新 origin/main 追蹤參照。
            _git("reset", "--mixed", "FETCH_HEAD")
            _git("add", *files)
            if _git("diff", "--cached", "--quiet").returncode == 0:
                log.info("發布:相對遠端最新沒有新內容,略過(正常)。")
                return
            c = _git("commit", "-m", msg)
            if c.returncode != 0:
                log.warning(f"發布 commit 失敗(第 {attempt+1} 次):{(c.stderr or c.stdout or '').strip()[:180]}")
                _git("merge", "--abort"); _git("rebase", "--abort")
                continue
            p = _git("push", "origin", "HEAD:main")
            if p.returncode == 0:
                log.info(f"發布成功:docs 已更新到 main（{msg}）")
                return
            # push 失敗多半是這幾秒內遠端又前進了(race)→ 下一輪 fetch 重接再推
            log.warning(f"發布 push 未成功(第 {attempt+1} 次):{(p.stderr or '').strip()[:180]}")
        log.warning("發布連續 3 次未成功,這輪先跳過(不影響盯盤,下輪再試)。")
    except Exception as e:
        log.warning(f"alerts 發布失敗(不影響盯盤):{e}")


def snapshot_date(snap) -> str:
    """快照的「資料日期」。

    ⚠️ **不能用 `snap['date'].iloc[0]`** —— 每檔股票的時間戳是它自己的**最後成交時間**,
    冷門股可能好幾小時沒成交。2026-07-20 12:35 實測:第一列是 11:00(那檔冷門股),
    但全市場有 1321 種不同時間戳,最多數落在 12:36(落後真實時間僅 6~12 秒)。
    我因此一度誤判「全市場快照延遲 95 分鐘」。取眾數才是對的。
    """
    if snap is None or getattr(snap, "empty", True) or "date" not in snap.columns:
        return ""
    try:
        return str(snap["date"].astype(str).str[:10].mode().iloc[0])
    except Exception:
        return str(snap["date"].iloc[0])[:10]


def snapshot_lag_seconds(snap) -> float | None:
    """快照落後現在幾秒(用時間戳眾數)。給網頁顯示「這份資料多新」。"""
    if snap is None or getattr(snap, "empty", True) or "date" not in snap.columns:
        return None
    try:
        m = str(snap["date"].astype(str).str[:19].mode().iloc[0])
        return (now_tpe().replace(tzinfo=None) - datetime.strptime(m, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:
        return None


_NAME_MAP_CACHE: dict = {}


def _name_map() -> dict[str, str]:
    """代號 → 名稱。快照的 name 欄實測只有 3.2% 有值,用本機月快取 stock_info 補。"""
    global _NAME_MAP_CACHE
    if _NAME_MAP_CACHE:
        return _NAME_MAP_CACHE
    try:
        from .fetchers import fetch_stock_info
        info = fetch_stock_info()
        if info is not None and not info.empty:
            _NAME_MAP_CACHE = dict(zip(info["stock_id"].astype(str),
                                       info["stock_name"].astype(str)))
    except Exception as e:
        log.warning(f"名稱對照載入失敗(訊號仍會發,只是沒名稱):{e}")
    return _NAME_MAP_CACHE


def _levels_fresh() -> bool:
    """levels 是不是今天建的。跨日的均線/前高會讓突破判斷整個歪掉。"""
    p = DOCS_DIR / "levels.json"
    if not p.exists():
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("date") == now_tpe().strftime("%Y-%m-%d")
    except Exception:
        return False


def _sleep_until(hhmm: str) -> None:
    """睡到台北時間 hhmm。已經過了就不睡。

    **這是「GitHub 內建 schedule 會延遲 5~30 分」的解法**:與其要求準時觸發,
    不如讓 job 自己等 —— 早觸發就等,晚觸發就直接開始,延遲變成無害。
    這樣使用者完全不必手動點,也不必去 cron-job.org 設定。
    """
    import time
    h, m = (int(x) for x in hhmm.split(":"))
    while True:
        now = now_tpe()
        if (now.hour, now.minute) >= (h, m):
            return
        wait = min(60.0, ((h * 60 + m) - (now.hour * 60 + now.minute)) * 60 - now.second)
        if wait <= 0:
            return
        log.info(f"等待開盤:現在 {now.strftime('%H:%M:%S')},{hhmm} 才開始")
        time.sleep(max(1.0, wait))


def loop(interval: float, until: str, notify: bool = True, publish: bool = False,
         start: str | None = None, archive: bool = True) -> dict:
    """常駐輪詢到 until(HH:MM,台北時間)。任何單次失敗都吞掉繼續跑 ——
    盯盤中途掛掉比慢一點嚴重得多。

    `start` 有給就先睡到那個時間(見 `_sleep_until`)。
    levels 不是今天的會**自動重建** —— 這樣整個盤中只需要一個排程,
    不必再另外設一條 08:50 的 build-levels。
    `archive=True` 時順便在 7 個檢查點存全市場快照(取代獨立的 snapshot 排程)。
    """
    import time
    if start:
        # 先建 levels 再等開盤:建檔要 1~2 分鐘,放在等待前面才不會吃掉開盤後的時間
        if not _levels_fresh():
            log.info("levels 不是今天的,先重建。")
            try:
                build_levels()
            except Exception as e:
                log.warning(f"levels 自動重建失敗:{e}")
        _sleep_until(start)
    elif not _levels_fresh():
        log.warning("⚠️ levels 不是今天的,突破判斷會失準;建議加 --start 讓它自動重建。")

    end_h, end_m = (int(x) for x in until.split(":"))
    polls = fired_total = errors = pending = 0
    done_tags: set[str] = set()
    _pub_last[0] = 0.0            # 讓第一批訊號立刻發布,不必等節流窗
    log.info(f"常駐盯盤啟動:每 {interval:g} 秒掃一次,到 {until} 為止"
             + ("(含快照存檔)" if archive else "")
             + f" · 發布={'開' if publish else '關'} · 節流 {PUBLISH_EVERY:g}s")

    # 開盤後先自動量一次上游更新頻率 —— 使用者不必記得手動跑,而且這是唯一能量的時機
    # (只有盤中資料才會變)。量完寫進 docs/freshness.json,網頁與下次調 interval 的依據。
    try:
        fr = measure_freshness(seconds=90, interval=3.0)
        fr["measured_at"] = now_tpe().strftime("%Y-%m-%d %H:%M")
        fr["interval_used"] = interval
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "freshness.json").write_text(
            json.dumps(fr, ensure_ascii=False), encoding="utf-8")
        med = fr.get("median_gap_s")
        if med and interval < med * 0.8:
            log.warning(f"⚠️ interval={interval:g}s 比上游更新間隔 {med}s 還密 —— "
                        f"多問的那幾次拿到的是同一份資料,建議調到 {med:g}s 左右")
    except Exception as e:
        log.warning(f"上游更新頻率量測失敗(不影響盯盤):{e}")
    while True:
        now = now_tpe()
        if (now.hour, now.minute) >= (end_h, end_m):
            break
        t0 = time.time()
        try:
            r = scan(notify=notify)
            polls += 1
            # 心跳:寫進 docs/freshness.json。沒有這個就分不出「今天沒訊號」和「job 根本沒跑」——
            # 2026-07-21 就是因為沒有痕跡,只能靠「有沒有 commit」猜,猜不準。
            try:
                hb = {}
                fp = DOCS_DIR / "freshness.json"
                if fp.exists():
                    hb = json.loads(fp.read_text(encoding="utf-8"))
                hb.update({"heartbeat": now.strftime("%Y-%m-%d %H:%M:%S"),
                           "polls": polls, "fired_today": fired_total,
                           "interval": interval, "checked": r.get("checked", 0)})
                fp.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            # 內外盤比走逐筆資料(一檔 2 萬多筆),太重不能每輪跑 → 每 10 分鐘一次
            if polls == 1 or (time.time() - _deep_last[0]) >= 600:
                _deep_last[0] = time.time()
                tgt = _deep_targets()
                try:
                    deep_metrics(tgt)
                except Exception as e:
                    log.warning(f"內外盤比更新失敗(不影響盯盤):{e}")
                try:
                    intraday_series(tgt)
                except Exception as e:
                    log.warning(f"走勢線更新失敗(不影響盯盤):{e}")
            # 檢查點快照存檔:併進盯盤迴圈,這樣整天只需要一個排程
            if archive:
                tag = now.strftime("%H%M")
                due = [t for t in CHECKPOINTS if t <= tag and t not in done_tags]
                if due:
                    t = due[-1]                      # 只補最後一個,不要一次補一串
                    done_tags.update(due)
                    try:
                        archive_snapshot(tag=t)
                        # 快照是**不可重建**的資料(鐵則三),不能讓它在 runner 的
                        # 磁碟上等 3 分鐘節流窗 —— job 這時候掛掉就永遠沒了。
                        # 歸零節流計時器,讓下面那段當場推上去。
                        _pub_last[0] = 0.0
                    except Exception as e:
                        log.warning(f"快照存檔 {t} 失敗(不影響盯盤):{e}")
            if r.get("new_alerts"):
                fired_total += r["new_alerts"]
                pending += r["new_alerts"]
            # 發布節流:每次 push 都會觸發一次 Vercel 重新部署,7/20 實測 20 分鐘內
            # 推了 8 次。Email 已經即時送達,網頁晚 3 分鐘完全可以接受。
            # ⚠️ 發布條件**不能綁在「有新訊號」上**(2026-07-21 修):
            # 走勢線 / 內外盤 / freshness 是每 10 分鐘算好的,但原本只有在有訊號時才 push
            # → 沒訊號的日子這些檔案整天不會上去,網頁永遠看不到走勢線。
            # 而且「job 到底有沒有在跑」也完全沒有痕跡,只能猜。
            if publish and (time.time() - _pub_last[0]) >= PUBLISH_EVERY:
                _pub_last[0] = time.time()
                msg = (f"intraday: {pending} signals {now.strftime('%H:%M')}" if pending
                       else f"intraday: live data {now.strftime('%H:%M')}")
                _git_publish(msg)
                pending = 0
            elif not r.get("ok") and r.get("reason") in ("no_sponsor", "no_levels"):
                log.warning(f"停止盯盤:{r['reason']}")   # 這兩種再輪也不會好
                break
        except Exception as e:
            errors += 1
            log.warning(f"單次掃描失敗(繼續):{e}")
        time.sleep(max(0.0, interval - (time.time() - t0)))
    if publish and pending:
        _git_publish(f"intraday: {pending} signals (final)")   # 收盤前補推殘留的
    log.info(f"盯盤結束:輪詢 {polls} 次、觸發 {fired_total} 筆、錯誤 {errors} 次")
    return {"polls": polls, "fired": fired_total, "errors": errors}


def measure_freshness(seconds: int = 120, interval: float = 2.0) -> dict:
    """量上游快照到底多久更新一次 —— 決定 interval 該設多少的唯一依據。
    盤中跑。回傳 {samples, distinct, median_gap_s, stamps}。"""
    import time
    seen, order = {}, []
    t_end = time.time() + seconds
    while time.time() < t_end:
        df = fetch_snapshot_all(force=True)
        if not df.empty and "date" in df.columns:
            s = str(df["date"].iloc[0])
            if s not in seen:
                seen[s] = time.time()
                order.append(s)
        time.sleep(interval)
    gaps = [round(seen[order[i + 1]] - seen[order[i]], 1) for i in range(len(order) - 1)]
    med = sorted(gaps)[len(gaps) // 2] if gaps else None
    out = {"samples": int(seconds / interval), "distinct": len(order),
           "gaps_s": gaps, "median_gap_s": med, "stamps": order[:10]}
    log.info(f"上游更新頻率:{seconds}s 內看到 {len(order)} 個不同時間戳,"
             f"中位間隔 {med}s → interval 設得比這個小沒有意義")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="盤中全市場掃描 + 訊號提醒")
    ap.add_argument("--build-levels", action="store_true", help="盤前建立均線/前高快取")
    ap.add_argument("--dry-run", action="store_true", help="只顯示不寫檔不寄信")
    ap.add_argument("--no-notify", action="store_true", help="寫檔但不寄信")
    ap.add_argument("--loop", action="store_true", help="常駐輪詢(建議用法)")
    # 預設 5 秒:實測上游約 11 秒才換一份資料、單次呼叫 0.63 秒,低於 5 秒只是重複拿同一份;
    # 額度 6000/hr 換算 interval < 2.7 秒會在盤中用滿。
    ap.add_argument("--interval", type=float, default=5.0, help="輪詢間隔秒數(預設 5)")
    ap.add_argument("--until", default="13:35", help="跑到幾點停(HH:MM 台北時間)")
    ap.add_argument("--publish", action="store_true", help="有新訊號就 git commit/push")
    ap.add_argument("--start", help="睡到這個時間才開始(HH:MM 台北);同時自動重建過期的 levels")
    ap.add_argument("--no-archive", action="store_true", help="不要在檢查點存全市場快照")
    ap.add_argument("--measure-freshness", type=int, metavar="SEC",
                    help="量上游快照更新頻率(秒),決定 interval 用")
    ap.add_argument("--test-discord", action="store_true",
                    help="發一則假訊號到 Discord 驗證 webhook(休市也能跑,不寫任何檔)")
    ap.add_argument("--test-publish", action="store_true",
                    help="寫一個測試 heartbeat 到 freshness.json 並跑 _git_publish 一次(休市也能跑,驗證發布管道)")
    a = ap.parse_args()
    if a.test_publish:
        # 收盤後驗「發布到網頁」這條管道 —— 7/22 盤中它整天靜默失敗、又被 capture 吞掉輸出。
        # 寫一個真實但無害的 heartbeat(下次真掃描會覆蓋),跑一次 _git_publish,看它到底
        # 卡在哪一步(fetch/commit/push/early-return)。新版每步都會 log。
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        fp = DOCS_DIR / "freshness.json"
        hb = {}
        try:
            if fp.exists():
                hb = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
        hb.update({"heartbeat": now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
                   "publish_test": True, "note": "test-publish 驗證用,下次真掃描會覆蓋"})
        fp.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
        print(f"已寫 {fp};呼叫 _git_publish …")
        _git_publish(f"intraday: publish-channel test {now_tpe().strftime('%H:%M')}")
        print("done — 看上面的 git 步驟 log 判斷成功與否。")
        raise SystemExit(0)
    if a.test_discord:
        # 收盤後也能驗管道 —— 不碰快照、不碰 alerts 檔,純粹確認 webhook 通不通。
        from .notify import discord_enabled, send_discord, link_buttons
        if not discord_enabled():
            print("未設定 DISCORD_WEBHOOK_URL —— 通知仍會走 Email。")
            raise SystemExit(1)
        # ⚠️ 價位**由真實資料推導**,不要寫死(2026-07-21 修)。
        # 原本寫死 1125,但本機 2330 昨收是 2320 —— 圖上就多出一根從 2320 直插 1125
        # 的假崩盤 K 棒,測試卡看起來像出了大事。測試卡要長得像真的訊號才有意義。
        _lv = {}
        try:
            _lv = (json.loads((DOCS_DIR / "levels.json").read_text(encoding="utf-8"))
                   .get("levels") or {}).get("2330") or {}
        except Exception:
            pass
        _prev = _lv.get("prev_close") or 1000.0
        _px = round(_prev * 1.0321, 2)
        _h20 = _lv.get("high20") or _prev
        demo = {"stock_id": "2330", "name": "台積電", "type": "breakout",
                "label": "量增突破(測試)", "price": _px, "change_rate": 3.21,
                "volume_ratio": 2.31, "vwap": round(_prev * 1.024, 2),
                "fired_at": now_tpe().strftime("%H:%M"),
                "reason": f"這是一則測試訊息,用來確認 Discord webhook 設定正確。"
                          f"收到這則就表示盤中訊號會送到這個頻道。"
                          f"(價位取自本機最新資料:昨收 {_prev}、20日高 {_h20})"}
        # 走**完整的**卡片組裝路徑(補產業/基本面/新聞 + 畫日K),不是只發一張空卡 ——
        # 這樣這個測試才真的能驗到「使用者盤中會看到什麼」。
        embeds, files = _build_cards([demo])
        mid = send_discord(embeds, "**Discord 通知測試**",
                           link_buttons([("📊 開網頁", f"{WEB_BASE}/#live")]), files=files)
        print(f"已送出,message_id={mid}" if mid else "送出失敗 —— 檢查 webhook URL 是否正確。")
        raise SystemExit(0 if mid else 1)
    if a.build_levels:
        build_levels()
    elif a.measure_freshness:
        print(json.dumps(measure_freshness(a.measure_freshness), ensure_ascii=False, indent=1))
    elif a.loop:
        print(json.dumps(loop(a.interval, a.until, not a.no_notify, a.publish,
                              start=a.start, archive=not a.no_archive), ensure_ascii=False))
    else:
        r = scan(dry_run=a.dry_run, notify=not a.no_notify)
        print(json.dumps({k: v for k, v in r.items() if k != "alerts"}, ensure_ascii=False))
        for x in r.get("alerts", []):
            print(f"  {x['fired_at']} {x['label']} {x['name']}({x['stock_id']}) "
                  f"{x['price']} {x['change_rate']:+.2f}% — {x['reason']}")
