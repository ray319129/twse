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
from . import chart as CH
from . import cost as C
from . import ledger as L
from . import levels_mtf as MTF
from . import risk as R
from . import plan as P
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

def run_pool(today: date | None = None, *, notify: bool = True) -> dict:
    today = today or now_tpe().date()
    pool = U.build(today=today)
    _enrich_hourly(pool)
    U.save(pool, today)
    _write_web(pool)
    log.info(f"當沖標的池已產生:多 {len(pool['long'])} / 空 {len(pool['short'])}")
    if notify:
        push_premarket_picks(pool)
    return pool


def _enrich_hourly(pool: dict) -> int:
    """替**已入選**的標的補上小時線水位,重算匯流。

    使用者要的是「日線、小時線、月線」三個週期,但 `universe.tech_snapshot`
    是對約 200 檔跑的,那裡開小時線等於 200 次 yfinance 呼叫 —— 太慢也太容易被擋,
    所以那一層寫死 `with_hourly=False`。結果就是**小時線從頭到尾沒有真的接上**,
    匯流度最高只到 2,`_KIND_WEIGHT["mtf3"]` 形同死碼。

    這一支補在池子建好之後:只剩幾十檔,盤前又不趕時間(08:xx 跑),
    一檔約 1~2 秒是可以接受的。失敗的就維持月線+日線,不影響其他檔。
    """
    from ..storage import load_prices
    n = 0
    for side in ("long", "short"):
        for c in pool.get(side) or []:
            sid = c.get("stock_id")
            px = c.get("price")
            if not sid or not px:
                continue
            try:
                df = load_prices(sid)
                if df is None or len(df) < 30:
                    continue
                atr = (c.get("atr_pct") or 0) / 100.0 * px or None
                zones = MTF.build(sid, df, px, market=c.get("market", "twse"),
                                  with_hourly=True, atr=atr)
                if zones:
                    c["zones"] = MTF.summary(zones, top=8)
                    n += 1
            except Exception as e:
                log.info(f"小時線補強失敗 {sid}(維持月線+日線):{e}")
    if n:
        log.info(f"多週期水位:已對 {n} 檔補上小時線(月線+日線+小時線匯流)")
    return n


def push_premarket_picks(pool: dict, top_n: int | None = None) -> int:
    """盤前把當日精選推到 Discord。

    第一版只在「盤中價位穿越」時才推,結果是**開盤前完全收不到任何東西** ——
    而當沖最需要準備的時間點就是開盤前。這支補上那一段:
    每天建完池就推多空各前 N 檔的完整計畫卡(進場/目標/停損/預計獲利/成本/理由)。

    與盤中訊號分開發:盤前是「今天可以盯這幾檔」,盤中是「現在觸發了」,
    兩者急迫度不同,混在同一則會讓人分不清該不該立刻動作。
    """
    cfg = load_cfg()
    n = top_n if top_n is not None else int((cfg.get("ranking", {}) or {}).get("push_top_n", 3))
    # 先決定要推哪幾檔,再一次把「圖」與「卡」綁在一起產生 ——
    # 分開產生的話檔名對不上,attachment:// 就引用不到。
    picks: list[tuple[dict, str]] = []
    for side, label in (("long", "▲ 做多"), ("short", "▼ 做空(先賣後買)")):
        for c in (pool.get(side) or [])[:n]:
            if c.get("plan"):
                picks.append((c, side))
    files, name_by_key = _charts_for([c for c, _ in picks], top=CHART_TOP_PREMARKET)
    embeds: list[dict] = []
    for c, side in picks:
        label = "▲ 做多" if side == "long" else "▼ 做空(先賣後買)"
        e = _plan_embed(c, side, title_prefix=label,
                        chart_name=name_by_key.get(_chart_key(c["stock_id"], side)))
        if e:
            embeds.append(e)
    if not embeds:
        log.info("盤前推播:今日無符合條件的標的,不發送。")
        return 0
    d = pool.get("date", "")
    head = (f"**當沖盤前精選 {d}**｜多 {len(pool.get('long') or [])} / "
            f"空 {len(pool.get('short') or [])} 檔入選,以下為各方向前 {n} 名\n"
            f"價格以昨收為基準,開盤後請以實際價位為準。條件符合 ≠ 買進建議。")
    try:
        send_discord(embeds, content=head, files=files or None)
        log.info(f"盤前推播已送出:{len(embeds)} 張卡、{len(files)} 張圖")
        return len(embeds)
    except Exception as e:
        log.warning(f"盤前推播失敗:{e}")
        return 0


# 一次最多附幾張圖。每張 5 分K 要打一次 yfinance(約 1~2 秒)。
# 盤前不趕時間 → 推幾張卡就給幾張圖(最多 6),讓每張卡都有圖。
# 盤中訊號是時間敏感的 → 只給前 3 張,多等 5 秒對當沖是實質成本。
CHART_TOP_PREMARKET = 6
CHART_TOP_INTRADAY = 3

# 同方向漲跌超過這個幅度就不再推(台股上下限 ±10%)。
# 漲停買不到、跌停賣不掉,而且隔天跳空風險不對稱。
MAX_CHG_PCT = 8.0


def _chart_key(stock_id: str, side: str) -> str:
    """同一檔可能同時有多方與空方卡(理論上方向判斷後不會,但別假設),
    所以附件檔名要含方向,否則兩張圖會撞名、其中一張被覆蓋。"""
    return f"{stock_id}_{side}"


def _charts_for(cands: list[dict], top: int = CHART_TOP_INTRADAY) -> tuple[list[tuple[str, bytes]], dict]:
    """給前 CHART_TOP 檔畫 5 分K。

    回 (附件清單, {chart_key: 檔名})。第二個回傳值是給 embed 用 `attachment://`
    引用的 —— 沒有它圖片會變成訊息底部的孤兒附件,對不上是哪一檔。
    失敗的略過,不影響通知本身。
    """
    out: list[tuple[str, bytes]] = []
    names: dict[str, str] = {}
    for c in cands[:top]:
        p = c.get("plan") or {}
        sid = str(c.get("stock_id", "x"))
        side = p.get("side", "long")
        png = CH.five_min_k_png(
            sid, c.get("name", ""), c.get("market", "twse"),
            entry=p.get("entry"), target=p.get("target"), stop=p.get("stop"),
            side=side)
        if png:
            fn = f"{sid}_{side}_5m.png"
            out.append((fn, png))
            names[_chart_key(sid, side)] = fn
    return out, names


def _zone_text(zones, price: float | None, top: int = 4) -> str:
    """把多週期匯流區排成一行給卡片用。

    使用者 2026-09-10 要求「策略要加入看日線、小時線、月線支撐壓力」——
    有算沒顯示等於沒做:他要能在卡片上看到這個價位是誰背書的。
    只列有兩個以上週期背書的(單週期的不夠硬,見 levels.min_mtf_confluence)。
    """
    if not zones or not price or price <= 0:
        return ""
    rows = []
    for z in zones:
        try:
            zp, conf = float(z.get("price")), int(z.get("confluence") or 1)
        except (TypeError, ValueError, AttributeError):
            continue
        if zp <= 0 or conf < 2:
            continue
        rows.append((abs(zp - price), zp, conf, "+".join(z.get("timeframes") or [])))
    rows.sort()
    out = []
    for _, zp, conf, tfs in rows[:top]:
        arrow = "壓力" if zp > price else "支撐"
        out.append(f"{arrow} **{zp:g}**({zp / price - 1:+.1%}・{tfs}・{conf}週期)")
    return "\n".join(out)


def _plan_embed(c: dict, side: str, *, title_prefix: str = "",
                chart_name: str | None = None) -> dict | None:
    """把一張標的池卡片轉成 Discord embed。沒有交易計畫就不發 ——
    只有代號和成本的卡片對使用者沒有用,反而佔版面。

    `chart_name` 是同一則訊息裡的 5 分K 附件檔名,用 `attachment://` 引用 ——
    **不加這個的話圖會掉到訊息最下面變成一排孤兒附件**,看不出哪張圖對應哪一檔
    (沿用 intraday_scan._embed 已經驗證過的作法)。
    """
    p = c.get("plan")
    if not p:
        return None
    fields = [
        {"name": "進場", "value": f"**{p['entry']:g}**", "inline": True},
        {"name": "目標", "value": f"{p['target']:g}(+{p['target_pct']}%)", "inline": True},
        {"name": "停損", "value": f"{p['stop']:g}(−{p['stop_pct']}%)", "inline": True},
        {"name": "張數", "value": f"{p['lots']} 張(約 {p['notional'] / 10000:.1f} 萬)", "inline": True},
        {"name": "預計獲利", "value": f"**+{p['net_profit']:,} 元**(已扣成本)", "inline": True},
        {"name": "最大虧損", "value": f"−{p['max_loss']:,} 元", "inline": True},
        {"name": "預計成本", "value": f"{p['cost_amount']:,} 元({c.get('cost_pct')}%)"
                                     + (f"・借券 {c['borrow_fee_pct']}%" if c.get("borrow_fee_pct") else ""),
         "inline": True},
        {"name": "風報比", "value": f"{p['rr']}", "inline": True},
    ]
    if p.get("target_capped_by"):
        fields.append({"name": "備註", "value": f"目標受{p['target_capped_by']}限制", "inline": True})
    reasons = c.get("reasons") or []
    if reasons:
        fields.append({"name": "為什麼推薦", "value": "・".join(reasons)[:1024], "inline": False})
    ind = c.get("indicators") or {}
    ind_txt = "・".join(x for x in [
        f"RSI {ind['rsi']}" if ind.get("rsi") is not None else "",
        f"月線 {ind['ma20']}" if ind.get("ma20") is not None else "",
        f"20日高 {ind['high20']}" if ind.get("high20") is not None else "",
        f"20日低 {ind['low20']}" if ind.get("low20") is not None else "",
        f"量能 {ind['vol_ratio']}" if ind.get("vol_ratio") is not None else "",
    ] if x)
    if ind_txt:
        fields.append({"name": "指標", "value": ind_txt[:1024], "inline": False})
    zone_txt = _zone_text(c.get("zones"), c.get("price"))
    if zone_txt:
        fields.append({"name": "多週期支撐壓力", "value": zone_txt[:1024], "inline": False})
    sid = c.get("stock_id", "")
    fields.append({"name": "參考", "value":
                   f"[線圖](https://www.cmoney.tw/forum/stock/{sid}) ・ "
                   f"[Yahoo 報價](https://tw.stock.yahoo.com/quote/{sid}.TW) ・ "
                   f"[公開資訊](https://mops.twse.com.tw/mops/web/t146sb05?step=1&COMPANY_ID={sid})",
                   "inline": False})
    note = c.get("plan_note") or ""
    gate = c.get("gate_note") or ""
    foot = note + (f"｜{gate}" if gate and gate != "閘門全過" else "")
    out = {
        "title": f"{title_prefix}｜{c.get('name', '')}({sid})",
        "color": _COLOR[side],
        "fields": fields,
        "footer": {"text": foot[:2048]},
    }
    if chart_name:
        out["image"] = {"url": f"attachment://{chart_name}"}
    return out


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
    """組出這一檔盤中要盯的水位。

    ⚠️ **昨日高低一定要取盤前算好的那份,不能用 `quote.high` / `quote.low`。**
    2026-09-10 排錯抓到:`Quote.high/low` 是 **今日盤中**的最高/最低(MIS 的 h/l),
    不是昨日的。把它當「昨日高點」有兩個後果:
      1. 卡片寫「昨日高點」但顯示的是今天的高點 —— 直接是錯的資訊;
      2. **這條水位在功能上是壞的**。今日高點依定義 ≥ 現價且隨現價移動,
         而 `detect_cross` 要 `price >= level × 1.002` 才算上穿 ——
         永遠不可能成立,除非報價的 high 欄位落後 price 欄位超過 0.2%。
         也就是說它只會在「資料不同步」時觸發,那全部是假訊號。
      空方的今日低點同理。而 prev_high/prev_low 權重 0.85 是第二高的,
      等於把第二重要的水位換成了雜訊產生器。
    正確來源是 `tech_snapshot` 從本機日線算的 `indicators.prev_high/prev_low`
    (昨日那根 K 棒的高低),盤前就算好、盤中不會變。
    """
    lv = cfg.get("levels", {}) or {}
    orng = opening.get(cand["stock_id"], {})
    ind = cand.get("indicators") or {}
    return S.build_levels(
        prev_high=ind.get("prev_high"),
        prev_low=ind.get("prev_low"),
        open_range_high=orng.get("high"),
        open_range_low=orng.get("low"),
        vwap=(quote.vwap if (quote and lv.get("use_vwap", True)) else None),
        price=(quote.price if quote else 0) or 0,
        stock_id=cand["stock_id"],
        use_round=lv.get("use_round_numbers", True),
        zones=cand.get("zones") or [],
        max_zones=int(lv.get("max_mtf_zones", 6)),
        min_confluence=int(lv.get("min_mtf_confluence", 2)),
    )


def _zone_bounds(zones, price: float) -> tuple[float | None, float | None]:
    """從標的池存下來的多週期匯流區裡,挑離「現價」最近的壓力與支撐。

    盤前那份 `mtf_resistance` / `mtf_support` 是以**昨收**為基準算的,盤中價格
    走掉之後就不對了(昨收下方的壓力，開盤跳空後可能已經在現價下方)。
    所以盤中要拿原始 zones 依即時價重新挑。只認有兩個以上週期背書的
    (confluence >= 2)—— 單一週期的水位當目標價不夠硬。
    """
    res = sup = None
    for z in zones or []:
        try:
            zp = float(z.get("price"))
            conf = int(z.get("confluence") or 1)
        except (TypeError, ValueError, AttributeError):
            continue
        if zp <= 0 or conf < 2:
            continue
        if zp > price and (res is None or zp < res):
            res = zp
        elif zp < price and (sup is None or zp > sup):
            sup = zp
    return res, sup


def scan_once(pool: dict, state: dict, cfg: dict) -> dict:
    """一輪掃描。state 保存 prev_price / 開盤區間 / 已觸發過的 key。"""
    now = now_tpe()
    hhmm = now.strftime("%H:%M")
    # ⚠️ 掃描宇宙要套上限。`scan.max_universe_*` 原本**完全沒被讀** —— 標的池有
    # 多 59 + 空 60 = 119 檔,整份丟進去等於每輪打 3 批 MIS,而**既有的
    # intraday_scan 盯盤同時也在打 MIS**(降級模式 35 檔 = 1 批)。
    # MIS 是非官方端點,兩支加起來 8 次/分鐘有被 ban 的風險。
    # 依分數各取一半,兩個方向都保留代表性。
    sc = cfg.get("scan", {}) or {}
    cap = int(sc.get("max_universe_with_subscription", 200) if sponsor_status().get("active")
              else sc.get("max_universe_no_subscription", 60))
    half = max(1, cap // 2)
    picked = (sorted(pool.get("long", []), key=lambda c: -(c.get("score") or 0))[:half]
              + sorted(pool.get("short", []), key=lambda c: -(c.get("score") or 0))[:half])
    cands = {c["stock_id"]: c for c in picked}
    if not cands:
        return {"ok": False, "reason": "empty_pool"}

    ids = list(cands)
    raw_quotes = get_quotes(ids)
    # ⚠️ **一定要濾掉 source == "close" 的報價。**
    # get_quotes 的最後一層降級是「本機昨收」,它會回昨天的價格當成 price。
    # 那個值一旦進到 prev_price,下一輪拿到真實價時就會「穿越」昨收與現價之間的
    # **每一條水位** —— 全部是假訊號。
    # 2026-09-09 實際發生:台塑化昨收 75.8、盤中 87.45,系統在 13:17 推出
    # 「觸發 整數關卡 85」,而 85 早在上午就被穿越過了。
    quotes = {k: q for k, q in raw_quotes.items()
              if q and q.price is not None and q.source != "close"}
    if not quotes:
        return {"ok": False, "reason": "no_live_quotes", "checked": 0,
                "pushed": 0, "held": 0}
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
            # 漲跌停附近不推:台股上下限 ±10%,漲停時買不到、跌停時賣不掉,
            # 而且隔天跳空的風險完全不對稱。既有 intraday_scan 有 MAX_CHG=8 的同款保護,
            # 當沖層第一版漏了 —— 2026-09-09 推出台塑化「已漲 9.86%」的做多訊號。
            if q.change_pct is not None:
                move = q.change_pct if side == "long" else -q.change_pct
                if move >= MAX_CHG_PCT:
                    continue

            score, reasons = S.score_signal(
                kind=lv.kind, edge_ratio=row.get("edge_ratio"),
                volume_ratio=q.volume_ratio, cost_pct=row["cost_pct"],
                change_pct=q.change_pct, side=side)
            atr_abs = (row.get("atr_pct") or 0) / 100 * q.price
            # 用觸發當下的即時價重算計畫 —— 盤前那份是以昨收為基準,價格已經動了。
            ind = row.get("indicators") or {}
            rk_cfg = cfg.get("risk", {}) or {}
            # ⚠️ 這裡的參數必須跟 universe._plan_fields 保持一致。
            # 2026-09-10 排錯抓到:盤中這條路徑**繞過了盤前那條的四道防護** ——
            #   1. 壓力/支撐還在用 20 日高低,而不是多週期匯流區。20 日高在
            #      突破日會落在現價**下方**(2026-09-09 台塑化就是這樣把做多目標
            #      算到進場之下),不變式雖然會擋下來,但是是靜默地把計畫變成 None;
            #   2. 沒有 min_rr —— 風報比 0.66 的單盤中照推,盤前卻擋掉;
            #   3. 沒有 atr_stop_mult / rr_target,寫死用 plan.py 的預設;
            #   4. 沒有 min_stop_cost_mult。
            # 「同一件事有兩條路徑」是這個專案反覆出事的地方,兩邊都要改。
            quota = float((pool.get("prefs") or {}).get("quota_twd") or 0)
            per_trade = float((pool.get("prefs") or {}).get("quota_per_trade_pct") or 100.0)
            zr, zs = _zone_bounds(row.get("zones"), q.price)
            live_plan = P.build_plan(
                stock_id=sid, side=side, ref_price=q.price, atr=atr_abs,
                quota=quota * per_trade / 100.0, risk_quota=quota,
                cost_pct=row["cost_pct"], borrow_fee_pct=row.get("borrow_fee_pct"),
                resistance=zr or ind.get("high20"), support=zs or ind.get("low20"),
                atr_stop_mult=float(rk_cfg.get("atr_stop_mult", 0.4)),
                rr_target=float(rk_cfg.get("rr_target", 1.5)),
                max_risk_pct=float(rk_cfg.get("max_risk_pct_of_quota", 2.0)),
                min_stop_cost_mult=float(rk_cfg.get("min_stop_cost_mult", 2.0)),
            )
            min_rr = float(rk_cfg.get("min_rr", 1.0))
            if live_plan is not None and live_plan.rr < min_rr:
                # 風報比不足就整筆不推 —— 沒有計畫的訊號對當沖沒有用,
                # 而「賠的比賺的多」的計畫比沒有更糟。
                continue
            _, live_note = P.plan_quality(live_plan)
            new_signals.append(S.Signal(
                stock_id=sid, name=row.get("name") or q.name, side=side, kind=lv.kind,
                label=lv.label, price=q.price, level=lv.price,
                change_pct=q.change_pct, volume_ratio=q.volume_ratio,
                atr_pct=row.get("atr_pct"), cost_pct=row["cost_pct"],
                edge_ratio=row.get("edge_ratio"), score=score, reasons=reasons,
                degraded=degraded, fired_at=hhmm,
                borrow_fee_pct=row.get("borrow_fee_pct"),
                lots_affordable=row.get("lots_affordable", 0),
                suggested_stop=(live_plan.stop if live_plan else None),
                risk_per_lot=C.risk_per_lot(q.price, live_plan.stop) if live_plan else None,
                plan=(live_plan.to_dict() if live_plan else None),
                plan_note=live_note,
                reasons_pool=row.get("reasons") or [],
                indicators=ind,
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
    """盤中觸發推播。與盤前精選共用同一種卡片結構(進場/目標/停損/獲利/成本/理由/指標),
    差別只在多了「觸發了什麼水位」與「即時價」—— 使用者不該因為是盤中就少拿到資訊。"""
    # 先畫圖:embed 要用 attachment:// 引用檔名,兩者必須一起產生。
    files, name_by_key = _charts_for([{
        "stock_id": s.stock_id, "name": s.name, "market": "twse",
        "plan": s.plan} for s in sigs if s.plan])
    embeds = []
    for s in sigs:
        arrow = "▲ 做多" if s.side == "long" else "▼ 做空(先賣後買)"
        p = s.plan
        fields = [
            {"name": "觸發", "value": f"{s.label} {s.level:g}", "inline": True},
            {"name": "即時價", "value": f"**{s.price:g}**"
             + (f"({s.change_pct:+.2f}%)" if s.change_pct is not None else ""), "inline": True},
            {"name": "訊號分", "value": f"{s.score:.0f}", "inline": True},
        ]
        if p:
            fields += [
                {"name": "進場", "value": f"**{p['entry']:g}**", "inline": True},
                {"name": "目標", "value": f"{p['target']:g}(+{p['target_pct']}%)", "inline": True},
                {"name": "停損", "value": f"{p['stop']:g}(−{p['stop_pct']}%)", "inline": True},
                {"name": "張數", "value": f"{p['lots']} 張(約 {p['notional'] / 10000:.1f} 萬)", "inline": True},
                {"name": "預計獲利", "value": f"**+{p['net_profit']:,} 元**(已扣成本)", "inline": True},
                {"name": "最大虧損", "value": f"−{p['max_loss']:,} 元", "inline": True},
                {"name": "預計成本", "value": f"{p['cost_amount']:,} 元({s.cost_pct}%)"
                 + (f"・借券 {s.borrow_fee_pct}%" if s.borrow_fee_pct else ""), "inline": True},
                {"name": "風報比", "value": f"{p['rr']}", "inline": True},
            ]
        else:
            fields.append({"name": "交易計畫", "value": f"無法產生({s.plan_note or '資料不足'})",
                           "inline": False})
        why = "・".join((s.reasons or []) + (s.reasons_pool or []))
        if why:
            fields.append({"name": "為什麼推薦", "value": why[:1024], "inline": False})
        ind = s.indicators or {}
        ind_txt = "・".join(x for x in [
            f"RSI {ind['rsi']}" if ind.get("rsi") is not None else "",
            f"月線 {ind['ma20']}" if ind.get("ma20") is not None else "",
            f"20日高 {ind['high20']}" if ind.get("high20") is not None else "",
            f"20日低 {ind['low20']}" if ind.get("low20") is not None else "",
        ] if x)
        if ind_txt:
            fields.append({"name": "指標", "value": ind_txt[:1024], "inline": False})
        fields.append({"name": "參考", "value":
                       f"[線圖](https://www.cmoney.tw/forum/stock/{s.stock_id}) ・ "
                       f"[Yahoo 報價](https://tw.stock.yahoo.com/quote/{s.stock_id}.TW) ・ "
                       f"[公開資訊](https://mops.twse.com.tw/mops/web/t146sb05?step=1&COMPANY_ID={s.stock_id})",
                       "inline": False})
        foot = f"{s.fired_at} ・ {s.plan_note or ''}"
        if s.degraded:
            foot += " ｜⚠ 降級模式:量比/均價為估計值"
        emb = {
            "title": f"{arrow}｜{s.name}({s.stock_id})",
            "color": _COLOR[s.side],
            "fields": fields,
            "footer": {"text": foot[:2048]},
        }
        cn = name_by_key.get(_chart_key(s.stock_id, s.side))
        if cn:
            emb["image"] = {"url": f"attachment://{cn}"}
        embeds.append(emb)
    try:
        send_discord(embeds, content="**當沖盤中訊號**", files=files or None)
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
