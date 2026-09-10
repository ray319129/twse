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
from dataclasses import dataclass, asdict, field
from datetime import date
from pathlib import Path

import pandas as pd

from ..config import DATA_DIR
from ..utils import log
from . import cost as C
from . import levels_mtf as MTF
from .plan import build_plan, plan_quality
from .rules import Gate, gate_for, load_or_fetch

PREFS_PATH = Path(DATA_DIR).parent / "config" / "daytrade_prefs.json"
LEVELS_PATH = DATA_DIR / "levels.parquet"
OUT_DIR = DATA_DIR / "daytrade"
_RISK: dict = {}   # build() 開始時才載入(見 _risk_cfg)

DEFAULT_PREFS = {
    "quota_twd": 250000,
    "quota_per_trade_pct": 50.0,
    "price_min": 20.0, "price_max": 300.0,
    "min_dollar_volume_m": 200.0,
    "min_atr_pct": 2.0, "max_atr_pct": 12.0,
    "max_cost_pct": 0.90,
    "allow_long": True, "allow_short": True,
    "max_borrow_fee_pct": 0.5,
    "min_edge_ratio": 3.0,
    "min_direction_bias": 0.3,
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
    # ── 以下是 2026-09-09 新增:讓卡片能直接照著下單,不用自己再算一次 ──
    plan: dict | None = None          # 進場/停損/目標/預計獲利/成本(見 plan.py)
    plan_quality: str = ""            # ok / weak / bad / none
    plan_note: str = ""
    direction_bias: float = 0.0   # −1(強空)~+1(強多),決定這檔該進哪一邊
    reasons: list = field(default_factory=list)   # 為什麼推薦(全部可查證)
    indicators: dict = field(default_factory=dict)  # RSI/均線/量比/前高低
    zones: list = field(default_factory=list)   # 多週期支撐壓力區(月線/日線/小時線匯流)

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


def tech_snapshot(stock_id: str) -> dict:
    """一次算齊這檔的技術面快照(ATR / RSI / 均線位階 / 前高低)。

    第一版只算 ATR%,而且是拿 levels 的 high20/ma20 距離當代理 —— 實測**排序出一堆
    金融股**(兆豐金、華南金、第一金),因為那個代理量到的是「距月線多遠」不是
    「一天會走多少」。改成用價格序列算真的,順便把交易計畫需要的
    ATR 絕對值、壓力、支撐,以及卡片要顯示的指標一起算出來 —— 反正只多幾個欄位,
    但可以省掉後面每一層各自再讀一次價格檔。

    只對通過價格/流動性初篩的標的算(約 200 檔),盤前批次跑,不打任何 API。
    抓不到就回 {} —— 呼叫端據此把這檔剔除(當沖沒有波動資料等於不能評估)。
    """
    try:
        from ..storage import load_prices
        from ..indicators import compute_all
        df = load_prices(stock_id)
        if df is None or len(df) < 30:
            return {}
        d = compute_all(df.copy())
        last = d.iloc[-1]
        atr, close = _f(last.get("atr14")), _f(last.get("close"))
        if not atr or not close or close <= 0:
            return {}
        tail = d.tail(20)
        out = {
            "atr": atr,
            "atr_pct": atr / close * 100.0,
            "close": close,
            "rsi": _f(last.get("rsi14")),
            "ma5": _f(last.get("ma5")), "ma20": _f(last.get("ma20")),
            "ma60": _f(last.get("ma60")),
            "vol_ratio": None,
            "high20": _f(tail["high"].max()) if "high" in tail else None,
            "low20": _f(tail["low"].min()) if "low" in tail else None,
            "prev_high": _f(last.get("high")), "prev_low": _f(last.get("low")),
        }
        v5, v20 = _f(last.get("vol_ma5")), _f(last.get("vol_ma20"))
        if v5 and v20 and v20 > 0:
            out["vol_ratio"] = round(v5 / v20, 2)

        # ── 多週期支撐壓力(月線 + 日線,零 API)──────────────────────
        # 使用者 2026-09-10 要求「策略要加入看日線、小時線、月線支撐壓力」。
        # 原本目標價的修正只用「20 日高/低」—— 那只是一個窗口的極值,沒有任何
        # 匯流概念,而且突破日時 20 日高會落在現價下方(2026-09-09 台塑化那次
        # 就是因此把做多目標算到進場之下)。改用多週期匯流區域,穩定得多。
        # 小時線要打網路,留給盤中掃描的小宇宙開(見 levels_mtf.build 的 with_hourly)。
        try:
            zones = MTF.build(stock_id, df, close, with_hourly=False, atr=atr)
            out["zones"] = MTF.summary(zones, top=8)
            r = MTF.nearest(zones, close, "resistance", min_weight=MTF.W_DAY)
            sup = MTF.nearest(zones, close, "support", min_weight=MTF.W_DAY)
            out["mtf_resistance"] = r.price if r else None
            out["mtf_resistance_w"] = r.weight if r else None
            out["mtf_support"] = sup.price if sup else None
            out["mtf_support_w"] = sup.weight if sup else None
        except Exception as e:
            log.info(f"多週期水位計算失敗 {stock_id}(退回 20 日高低):{e}")
        return out
    except Exception:
        return {}


def real_atr_pct(stock_id: str) -> float | None:
    """向後相容的薄包裝(既有測試與呼叫端仍在用)。"""
    return tech_snapshot(stock_id).get("atr_pct")


def direction_bias(t: dict) -> tuple[float, list[str]]:
    """技術面偏多還是偏空。回 (bias −1~+1, 判斷依據)。

    ## 為什麼一定要有這個(2026-09-09 修)

    第一版的標的池**完全沒有方向判斷** —— 只要通過法規閘門與成本/波動篩選,
    同一檔就同時進多方與空方兩份清單,而排序用的 `ATR% ÷ 成本%` 多空一模一樣。
    實測結果:60 檔裡有 **51 檔同時出現在兩邊**。那不是推薦,那是「這檔可以做,
    方向你自己看」—— 等於把最難的部分丟回給使用者。

    這裡用五個**互相獨立**的均線/動能條件投票,每個 ±1,加總後歸一化。
    刻意不用複雜模型:當沖的方向判斷本來就不可能精準,能做的是
    「方向不明的就別推」,而不是硬要猜一邊。
    """
    close, ma5, ma20 = t.get("close"), t.get("ma5"), t.get("ma20")
    ma60, rsi = t.get("ma60"), t.get("rsi")
    votes: list[int] = []
    why: list[str] = []
    if close and ma5:
        v = 1 if close > ma5 else -1
        votes.append(v); why.append(f"收盤{'>' if v > 0 else '<'}5日線")
    if ma5 and ma20:
        v = 1 if ma5 > ma20 else -1
        votes.append(v); why.append(f"5日線{'>' if v > 0 else '<'}月線")
    if close and ma20:
        v = 1 if close > ma20 else -1
        votes.append(v); why.append(f"收盤{'>' if v > 0 else '<'}月線")
    if ma20 and ma60:
        v = 1 if ma20 > ma60 else -1
        votes.append(v); why.append(f"月線{'>' if v > 0 else '<'}季線")
    if rsi is not None:
        # RSI 只在明確偏離中軸時才投票 —— 45~55 之間本來就沒有方向可言
        if rsi >= 55:
            votes.append(1); why.append(f"RSI {rsi:.0f} 偏強")
        elif rsi <= 45:
            votes.append(-1); why.append(f"RSI {rsi:.0f} 偏弱")
    if not votes:
        return (0.0, [])
    return (round(sum(votes) / len(votes), 3), why)


def build_reasons(t: dict, side: str, c_edge: float | None,
                  gate_note: str) -> list[str]:
    """「為什麼推薦這檔」—— 全部是可查證的事實,不是形容詞。

    刻意不寫「強勢」「看好」這種話:當沖卡片上的每一句都要能對回一個數字,
    否則使用者無從判斷該不該信。
    """
    # ⚠️ 這裡**刻意不重複 direction_bias 已經講過的東西**。
    # 2026-09-09 的實際卡片同時出現:
    #   「偏多 100%(收盤>5日線、5日線>月線、收盤>月線、月線>季線、RSI 62 偏強)」
    #   「站上 5 日與月線(多頭排列)」   ← 重複
    #   「RSI 62(中性)」               ← 與上面的「RSI 62 偏強」**互相矛盾**
    #   「波動是來回成本的 7.9 倍」      ← 與訊號層的「空間 7.9x 成本」重複
    # 均線與 RSI 的判讀交給 direction_bias 統一講(那裡才有完整的投票結果),
    # 這裡只補它沒講的:波動幅度、量能、閘門提醒。
    r: list[str] = []
    atrp, close = t.get("atr_pct"), t.get("close")
    if atrp:
        r.append(f"日均波動 ATR {atrp:.1f}%")
    if c_edge:
        # 「空間」留在這裡(池子)而不是訊號層 —— 盤前卡只有池子的理由,
        # 拿掉的話盤前就看不到操作空間了。訊號層那句較短的重複版已移除。
        r.append(f"波動是來回成本的 {c_edge:.1f} 倍(操作空間)")
    ma20 = t.get("ma20")
    if close and ma20:
        r.append(f"距月線 {(close / ma20 - 1) * 100:+.1f}%")
    vr = t.get("vol_ratio")
    if vr:
        r.append(f"5日均量/20日均量 {vr:.2f}")
    # 多週期支撐壓力 —— 這是判斷「目標價到不到得了」最實際的依據
    if side == "long" and t.get("mtf_resistance"):
        r.append(f"上方壓力 {t['mtf_resistance']:g}(多週期匯流,權重 {t.get('mtf_resistance_w', 0):g})")
    elif side == "short" and t.get("mtf_support"):
        r.append(f"下方支撐 {t['mtf_support']:g}(多週期匯流,權重 {t.get('mtf_support_w', 0):g})")
    if gate_note and gate_note != "閘門全過":
        r.append(f"⚠ {gate_note}")
    return r


def _risk_cfg() -> dict:
    """讀 config/daytrade.yaml 的 risk 區塊。

    ⚠️ 一開始 `build_plan` 只用 plan.py 的預設值,`daytrade.yaml` 裡的
    `atr_stop_mult` / `max_risk_pct_of_quota` **完全沒被讀** —— 設定檔看起來可調
    但改了沒有任何效果,那比沒有設定檔更糟。
    """
    try:
        import yaml
        cfg = yaml.safe_load(
            (Path(DATA_DIR).parent / "config" / "daytrade.yaml").read_text(encoding="utf-8")) or {}
        return cfg.get("risk", {}) or {}
    except Exception:
        return {}


def _plan_fields(sid: str, side: str, price: float, tech: dict, prefs: dict,
                 cost_pct: float, borrow_fee: float | None,
                 edge: float | None, gate_note: str,
                 bias: float = 0.0, bias_why: list | None = None) -> dict:
    """組出卡片需要的 plan / reasons / indicators 三塊。

    盤前用昨收當參考價 —— 開盤後盯盤層會用即時價重算(見 engine.scan_once)。
    這裡先算是為了讓**盤前就看得到完整的可執行計畫**,而不是只有一張候選清單。
    """
    min_rr = float(_RISK.get("min_rr", 1.0))
    p = build_plan(
        stock_id=sid, side=side, ref_price=price, atr=tech.get("atr"),
        # ⚠️ 用**單筆預算**而不是整個額度:當沖額度是當日累計上限,
        # 每筆都 sizing 到吃滿的話,第二筆就會被券商擋下來(使用者 2026-09-09 說明)。
        quota=float(prefs.get("quota_twd") or 0) * float(prefs.get("quota_per_trade_pct", 50)) / 100.0,
        risk_quota=float(prefs.get("quota_twd") or 0),   # 風險 % 的基準是**總額度**
        cost_pct=cost_pct,
        borrow_fee_pct=borrow_fee,
        # 壓力/支撐優先用多週期匯流區域,沒有才退回 20 日高低。
        resistance=tech.get("mtf_resistance") or tech.get("high20"),
        support=tech.get("mtf_support") or tech.get("low20"),
        atr_stop_mult=float(_RISK.get("atr_stop_mult", 0.4)),
        rr_target=float(_RISK.get("rr_target", 1.5)),
        max_risk_pct=float(_RISK.get("max_risk_pct_of_quota", 2.0)),
        min_stop_cost_mult=float(_RISK.get("min_stop_cost_mult", 2.0)),
    )
    # 風報比低於門檻的計畫等於「賠的比賺的多」,不該當成推薦。
    # 實測 2026-09-10:目標被壓力壓下來後出現 RR 0.66 的卡片,卻照樣進池。
    if p is not None and p.rr < min_rr:
        p = None
    lvl, note = plan_quality(p)
    if lvl == "none" and min_rr > 0:
        note = f"無有效計畫(風報比需 ≥ {min_rr:g},或資料不足)"
    return {
        "plan": p.to_dict() if p else None,
        "plan_quality": lvl, "plan_note": note,
        "direction_bias": bias,
        "reasons": (([("偏多" if bias > 0 else "偏空") + f" {abs(bias):.0%}(" + "、".join(bias_why or []) + ")"]
                     if bias_why else [])
                    + build_reasons(tech, side, edge, gate_note)),
        "indicators": {k: (round(v, 2) if isinstance(v, float) else v)
                       for k, v in tech.items() if k not in ("atr", "zones")},
        "zones": tech.get("zones") or [],
    }


def build(prefs: dict | None = None, gates: dict[str, Gate] | None = None,
          today: date | None = None) -> dict:
    """產出今日當沖標的池。回傳 {date, prefs, long: [...], short: [...], stats}。"""
    today = today or date.today()
    prefs = prefs or load_prefs()
    global _RISK
    _RISK = _risk_cfg()
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
             "no_direction": 0,
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
        # ⚠️ 要用**單筆預算**判斷,不能用總額度:計畫層是用單筆預算算張數的,
        # 兩邊標準不一致的話會有一批標的「通過池子的買得起檢查、卻算不出計畫」
        # (實測 2026-09-10 有 36 檔卡在這個落差上,卡片只寫「資料不足」)。
        per_trade_budget = (float(prefs.get("quota_twd") or 0)
                            * float(prefs.get("quota_per_trade_pct", 50)) / 100.0)
        lots = C.lots_for_quota(price, per_trade_budget)
        if not forced and lots < 1:
            stats["unaffordable"] += 1
            continue

        # 波動是**硬條件**不是軟權重:當沖沒有波動就只剩成本。
        # (通過前面便宜的篩選後才算,約 200 檔,不會拖太久。順便把交易計畫與
        #  卡片指標需要的 ATR 絕對值/RSI/均線/前高低一次算齊。)
        tech = tech_snapshot(sid)
        atr_pct = tech.get("atr_pct")
        if not forced:
            if atr_pct is None:
                stats["no_atr"] += 1
                continue
            if not (prefs["min_atr_pct"] <= atr_pct <= prefs["max_atr_pct"]):
                stats["volatility_filtered"] += 1
                continue

        # ── 方向判斷:一檔只該出現在它技術面偏向的那一邊 ──
        # 沒有這一段的話,同一檔會同時進多方與空方(實測 60 檔裡 51 檔重複),
        # 那不是推薦,是把最難的部分丟回給使用者。
        bias, bias_why = direction_bias(tech)
        min_bias = float(prefs.get("min_direction_bias", 0.3))
        want_long = bias >= min_bias
        want_short = bias <= -min_bias
        if not forced and not (want_long or want_short):
            stats["no_direction"] += 1
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
        elif prefs.get("allow_long", True) and (want_long or forced):
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
                **_plan_fields(sid, "long", price, tech, prefs, long_cost,
                               None, long_edge, g.explain(), bias, bias_why),
            ))

        # ── 空方:額外閘門 + 借券費進成本 ──
        if not prefs.get("allow_short", True):
            continue
        if not (want_short or forced):
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
            **_plan_fields(sid, "short", price, tech, prefs, short_cost,
                           fee, short_edge, g.explain(), bias, bias_why),
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
    # 沒有可執行計畫的不算推薦 —— 使用者反映「訊號過多」,而其中一大部分是
    # 這種「列在清單上但算不出進場/停損/目標」的標的(實測一度佔 36%)。
    # 它們對當沖沒有任何用處:你沒辦法照著下單,只是佔版面。
    dropped_noplan = sum(1 for c in longs + shorts if not c.plan)
    longs = [c for c in longs if c.plan]
    shorts = [c for c in shorts if c.plan]
    stats["no_plan_dropped"] = dropped_noplan

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
             f"方向不明擋 {stats['no_direction']}、無計畫擋 {stats.get('no_plan_dropped', 0)}、"
             f"空方閘門擋 {stats['short_gate_blocked']})")
    # 建池時間戳:使用者要看得出「這份推薦是什麼時候算的」,只有日期不夠 ——
    # 同一天可能重建好幾次(手動觸發、盤前排程、修完 bug 補跑)。
    from datetime import datetime as _dt
    return {"date": today.isoformat(),
            "built_at": _dt.now().strftime("%Y-%m-%d %H:%M"),
            "prefs": prefs, "stats": stats,
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
