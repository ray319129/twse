"""當沖交易計畫 —— 把「候選標的」變成「可執行的建議」。

## 為什麼要有這一支(2026-09-09)

第一版的標的池只回答「今天哪些檔可以做」,但使用者要的是
**進場價 / 出場價 / 停損價 / 預計獲利 / 預計成本 / 為什麼推薦**。
少了這些,那份清單就只是 watchlist,還是得自己算 —— 而當沖沒時間算。

## 設計原則

**1. 停損先決定,其他都從它推出來。**
當沖的唯一硬約束是「一天內一定要平掉」,所以風險必須先框死。
停損距離 = ATR(14) × `atr_stop_mult`,再用 tick 對齊到可掛的價位。
目標價 = 進場 ± 停損距離 × `rr_target`(預設 1.5R)。

**2. 預計獲利一律扣成本,而且扣完才顯示。**
毛獲利沒有意義 —— 來回成本 0.52%~1.3%,不扣的話 1R 的單看起來會像賺錢實際打平。
所以卡片上的「預計獲利」是**淨額**,並且同時列出成本金額讓人對照。

**3. 算得出來才給,算不出來就說沒有。**
ATR 缺、價格為 0、停損距離小於一個 tick → 回 `None`,不要用猜的數字讓人拿去下單。

**4. 目標價要被壓力/支撐修正。**
純用 1.5R 推出來的目標可能落在前高上方一大截,那種單不會到。
所以若 R 目標超過最近壓力(多方)/支撐(空方),就改用那個水位並標記出來。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from . import cost as C

# 預設參數(可被 config/daytrade.yaml 的 risk 區塊覆寫)
#
# ⚠️ `atr_stop_mult` 為什麼是 0.4 而不是 1.0:
# ATR(14) 是**整天**的平均真實區間,但當沖只持有幾小時、通常只吃到其中一段。
# 第一版用 1.0 的實測結果:台半停損距離 5.56%、最大虧損 10,766 元 —— 那是 25 萬額度的
# **4.3%**,一天做兩筆就可能賠掉近一成本金;而且目標價要走 8.28% 才到,盤中根本不會發生。
# 0.4 倍讓停損落在日內合理波動內,目標 1.5R ≈ 0.6 倍 ATR,是一天走得到的距離。
DEFAULT_ATR_STOP_MULT = 0.4
DEFAULT_RR_TARGET = 1.5
MIN_STOP_TICKS = 2          # 停損至少要離 2 個 tick,否則等於一跳就出場
# 停損距離至少要是**來回成本的這個倍數**(config risk.min_stop_cost_mult)。
# 2026-09-10 排錯抓到:兆豐金/彰銀/元大金/永豐金這些低波動金融股,
# ATR 小 × atr_stop_mult 0.4 → 停損只有 0.92~1.12%,而來回成本 0.55~0.72%,
# 倍數只有 1.38~1.68。那種停損**整條都在雜訊帶裡** —— 隨便一個買賣價差來回就掃到,
# 而且被掃掉時虧的錢跟成本同一個量級,等於在付手續費玩擲硬幣。
# 它們能通過 edge_ratio 閘門是因為成本也低,比值看起來沒問題 ——
# 但比值不能取代絕對距離。2.0 倍是下限,低於此就不給計畫。
MIN_STOP_COST_MULT = 2.0
DEFAULT_MAX_RISK_PCT = 2.0  # 單筆最大虧損不超過額度的 %(config risk.max_risk_pct_of_quota)


def align_to_tick(price: float, stock_id: str, *, mode: str = "nearest") -> float | None:
    """把價格對齊到可掛單的 tick 刻度。

    不對齊的話卡片會出現 74.37 這種**掛不進去**的價格,使用者得自己換算 ——
    當沖沒有這個時間。mode: nearest / up(保守買進) / down(保守賣出)。
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    t = C.tick_size(p, stock_id)
    n = p / t
    if mode == "up":
        n = -(-n // 1)          # ceil
    elif mode == "down":
        n = n // 1
    else:
        n = round(n)
    out = round(n * t, 4)
    return out if out > 0 else None


@dataclass
class TradePlan:
    side: str
    entry: float
    stop: float
    target: float
    stop_pct: float            # 停損距離佔進場價 %
    target_pct: float
    rr: float                  # 實際風報比(可能因壓力修正而不等於設定值)
    lots: int
    notional: float            # 進場總金額
    cost_amount: float         # 來回成本(元,含借券費)
    gross_profit: float        # 到目標價的毛利
    net_profit: float          # **扣掉成本後**的預期獲利
    max_loss: float            # 打到停損的虧損(含成本)
    target_capped_by: str | None = None   # 目標價被哪個水位壓下來
    tick: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def build_plan(*, stock_id: str, side: str, ref_price: float,
               atr: float | None, quota: float, risk_quota: float | None = None,
               cost_pct: float, borrow_fee_pct: float | None = None,
               resistance: float | None = None, support: float | None = None,
               atr_stop_mult: float = DEFAULT_ATR_STOP_MULT,
               rr_target: float = DEFAULT_RR_TARGET,
               max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
               min_stop_cost_mult: float = MIN_STOP_COST_MULT,
               max_lots: int | None = None) -> TradePlan | None:
    """算一份可以直接照著下單的計畫。任何一個必要輸入缺失就回 None。"""
    try:
        entry_raw = float(ref_price)
    except (TypeError, ValueError):
        return None
    if entry_raw <= 0 or side not in ("long", "short") or not atr or atr <= 0:
        return None

    tick = C.tick_size(entry_raw, stock_id)
    # 進場價保守取整:做多往上、做空往下 —— 寧可算出來的預期獲利略低,
    # 也不要用一個你其實搶不到的價位去算出漂亮的數字。
    entry = align_to_tick(entry_raw, stock_id, mode="up" if side == "long" else "down")
    if entry is None:
        return None

    # 三個下限取大者:ATR 比例、最少 2 個 tick、來回成本的 N 倍。
    cost_floor = entry * (cost_pct or 0.0) / 100.0 * min_stop_cost_mult
    stop_dist = max(atr * atr_stop_mult, tick * MIN_STOP_TICKS, cost_floor)
    stop = align_to_tick(entry - stop_dist if side == "long" else entry + stop_dist,
                         stock_id, mode="down" if side == "long" else "up")
    if stop is None or stop <= 0 or stop == entry:
        return None
    real_stop_dist = abs(entry - stop)
    if real_stop_dist < tick:
        return None

    # 目標價:R 倍數推出來,再被壓力/支撐修正
    target_raw = (entry + real_stop_dist * rr_target if side == "long"
                  else entry - real_stop_dist * rr_target)
    capped = None
    # ⚠️ 壓力/支撐只有落在「進場與目標之間」才是有效的修正。
    # 原本做多那條只檢查 `0 < resistance < target_raw`,**漏了 `resistance > entry`** ——
    # 強勢突破日(股價已站上 20 日高)時 resistance 會低於進場價,目標就被壓到進場之下。
    # 2026-09-09 實際發生:台塑化 6505 現價 87.45、20日高 83.1 → 做多目標算成 83,
    # 比進場 87.5 還低,卻因為下面用 abs() 算距離而顯示成「+5.14%、預計獲利 +7,976 元」。
    # (做空那條本來就有 `support < entry`,是不對稱的疏漏。)
    if side == "long" and resistance and entry < resistance < target_raw:
        target_raw, capped = resistance, "最近壓力"
    elif side == "short" and support and target_raw < support < entry:
        target_raw, capped = support, "最近支撐"
    target = align_to_tick(target_raw, stock_id,
                           mode="down" if side == "long" else "up")
    if target is None or target <= 0:
        return None

    # ── 不變式:做多必須 停損 < 進場 < 目標;做空必須 目標 < 進場 < 停損 ──
    # 這是**安全網**,不是重複檢查。上面任何一段邏輯寫錯(例如壓力修正漏了方向判斷)
    # 都會在這裡被擋下來,而不是產生一組看起來合理、實際上是虧的價格。
    # 之所以需要它:距離一律用 abs() 算,方向錯了數字仍然「漂亮」——
    # 台塑化那次就是目標比進場低,卻顯示 +5.14%、預計獲利 +7,976 元。
    ok = (stop < entry < target) if side == "long" else (target < entry < stop)
    if not ok:
        return None

    target_dist = abs(target - entry)
    if target_dist < tick:
        return None

    # ── 部位大小:買得起幾張 **和** 風險預算允許幾張,取小的 ──
    # 只看「買得起」會讓停損寬的標的一次押掉太多風險 —— 實測台半用滿額度時
    # 單筆最大虧損 10,766 元 = 25 萬的 4.3%。風險預算把它壓回設定的上限內。
    #
    # ⚠️ `quota` 與 `risk_quota` 是**兩件不同的事**,不能共用同一個數字:
    #   quota      = 這筆最多能動用多少資金(單筆預算 = 總額度 × quota_per_trade_pct)
    #   risk_quota = 風險百分比的基準(**總額度**,因為 max_risk_pct_of_quota 的語意
    #                就是「總額度的百分之幾」)
    # 原本兩者都傳單筆預算 → 風險上限變成 2% × 50% = 總額度的 1%,只有設定值的一半,
    # 而且是**靜默**的:實測 2026-09-10 有 43 檔(佔標的池 36%)因此算不出計畫,
    # 卡片只寫「資料不足」,完全看不出真正原因是風險預算被砍半。
    affordable = C.lots_for_quota(entry, quota)
    rq = risk_quota if risk_quota is not None else quota
    risk_budget = rq * (max_risk_pct / 100.0) if (rq and max_risk_pct) else 0
    risk_lots = affordable
    if risk_budget > 0:
        per_lot_risk = real_stop_dist * 1000
        risk_lots = int(risk_budget // per_lot_risk) if per_lot_risk > 0 else 0
    lots = min(affordable, risk_lots)
    if max_lots is not None:
        lots = min(lots, max_lots)
    if lots < 1:
        return None

    shares = lots * 1000
    notional = entry * shares
    total_cost_pct = cost_pct + (borrow_fee_pct or 0.0 if side == "short" else 0.0)
    cost_amount = notional * total_cost_pct / 100.0
    gross = target_dist * shares
    net = gross - cost_amount
    max_loss = real_stop_dist * shares + cost_amount

    return TradePlan(
        side=side, entry=entry, stop=stop, target=target,
        stop_pct=round(real_stop_dist / entry * 100, 2),
        target_pct=round(target_dist / entry * 100, 2),
        rr=round(target_dist / real_stop_dist, 2),
        lots=lots, notional=round(notional),
        cost_amount=round(cost_amount), gross_profit=round(gross),
        net_profit=round(net), max_loss=round(max_loss),
        target_capped_by=capped, tick=tick,
    )


def plan_quality(plan: TradePlan | None) -> tuple[str, str]:
    """這份計畫值不值得做。回 (等級, 說明)。

    當沖最常見的自欺是「風報比看起來 1.5,但扣完成本淨利是負的」——
    所以這裡的第一道檢查是 net_profit,不是 rr。
    """
    if plan is None:
        return ("none", "資料不足,無法產生計畫")
    if plan.net_profit <= 0:
        return ("bad", f"⚠ 到目標價也是虧的:毛利 {plan.gross_profit:,.0f} "
                       f"扣成本 {plan.cost_amount:,.0f} = {plan.net_profit:,.0f} 元")
    ratio = plan.net_profit / plan.cost_amount if plan.cost_amount else 0
    if plan.rr < 1.0:
        return ("bad", f"風報比僅 {plan.rr}(賺的比賠的少),不建議")
    if ratio < 1.0:
        return ("weak", f"淨利 {plan.net_profit:,.0f} 元只有成本的 {ratio:.1f} 倍,空間偏薄")
    return ("ok", f"風報比 {plan.rr}、淨利是成本的 {ratio:.1f} 倍")


def explain_plan(plan: TradePlan, *, name: str, stock_id: str) -> str:
    """一句話講完怎麼做 —— 給 Discord 與卡片用。"""
    d = "做多" if plan.side == "long" else "做空(先賣後買)"
    return (f"{name}({stock_id}) {d} {plan.lots} 張｜"
            f"進 {plan.entry:g} → 目標 {plan.target:g}(+{plan.target_pct:.1f}%)"
            f"、停損 {plan.stop:g}(−{plan.stop_pct:.1f}%)｜"
            f"淨賺約 {plan.net_profit:,.0f} / 最大賠 {plan.max_loss:,.0f} 元")
