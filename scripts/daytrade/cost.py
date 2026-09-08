"""當沖成本計算 —— 整個當沖層的地基,純函數、零 I/O、可完整單元測試。

## 核心事實:價格區間決定成本,而且差很多

台股升降單位(tick)分六級,**非線性**。掃單當沖的來回成本 =
固定的手續稅 0.321% + 進出各跨一次價差(各 1 個 tick):

    股價  tick   1檔%    來回總成本
     99   0.10  0.101%   0.523%   ← 最便宜
    105   0.50  0.476%   1.273%   ← 最貴(差 2.4 倍)

規律:**tick 換檔的正上方最貴、正下方最便宜**。
危險帶 100~130 / 10~16 / 500~560 / 1000~1300;甜蜜帶 45~49 / 90~99 / 380~499 / 900~990。

這是「價格區間選擇」真正的意義 —— 它不是「我買得起什麼」,是「這個價位帶要求我
每筆多賺多少才回本」。所以 universe 篩選必須把成本算給使用者看,不能只給個輸入框。

## 資料來源
- 升降單位級距:臺灣證券交易所營業細則(2026-09 現行);ETF 另有兩級制。
- 2026-08 主管機關已核准「1000 元以上由 5 元降為 1 元」,**預計 2027-07 生效** ——
  生效後要改 `_STOCK_BANDS` 最後一段並更新測試。
- 當沖證交稅減半(0.15%)延長至 2027-12-31;之後若未再延長要改 `DEFAULT_DAYTRADE_TAX`。
"""
from __future__ import annotations

# (下界, 上界, tick) —— 上界為開區間。一般股票六級。
_STOCK_BANDS: tuple[tuple[float, float, float], ...] = (
    (0.0, 10.0, 0.01),
    (10.0, 50.0, 0.05),
    (50.0, 100.0, 0.10),
    (100.0, 500.0, 0.50),
    (500.0, 1000.0, 1.00),
    (1000.0, float("inf"), 5.00),
)
# ETF 兩級制(與一般股票不同,別套錯 —— 00xx 開頭的代號要走這組)
_ETF_BANDS: tuple[tuple[float, float, float], ...] = (
    (0.0, 50.0, 0.01),
    (50.0, float("inf"), 0.05),
)

DEFAULT_FEE_RATE = 0.001425      # 券商手續費標準費率
DEFAULT_FEE_DISCOUNT = 0.6       # 折扣(與 config/screeners.yaml 的 cost.fee_discount 一致)
DEFAULT_DAYTRADE_TAX = 0.0015    # 當沖證交稅(減半),賣出時課一次


def is_etf(stock_id: str) -> bool:
    """ETF 代號判斷:台股 ETF 以 '00' 開頭,長度 4~6 碼皆有
    (0050、0056 是 4 碼;006208、00878、00929 是 5~6 碼)。
    一般股票代號從 1 開頭(1101~9958),不會以 0 開頭,所以前綴判斷就夠。

    ⚠️ 這個判斷只影響 tick 級距,但判錯的代價很大:0050 在 30 元時
    ETF tick 是 0.01、一般股票 tick 是 0.05 —— 成本會被高估 5 倍。
    (第一版寫成 `len(sid) >= 5`,剛好把 0050 / 0056 這兩支最大的 ETF 排除掉,
     由 tests/test_daytrade_cost.py 抓出來。)"""
    sid = str(stock_id or "").strip()
    return sid.startswith("00") and len(sid) >= 4


def tick_size(price: float, stock_id: str = "") -> float:
    """該價格的最小升降單位。price <= 0 視為無效,回 0.01 保底(不丟例外,這是背景計算)。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.01
    if p <= 0:
        return 0.01
    bands = _ETF_BANDS if is_etf(stock_id) else _STOCK_BANDS
    for lo, hi, t in bands:
        if lo <= p < hi:
            return t
    return bands[-1][2]


def tick_pct(price: float, stock_id: str = "") -> float | None:
    """一個 tick 佔股價的百分比。這是跨價差的直接成本。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return tick_size(p, stock_id) / p * 100.0


def fee_tax_pct(*, fee_rate: float = DEFAULT_FEE_RATE,
                fee_discount: float = DEFAULT_FEE_DISCOUNT,
                tax_rate: float = DEFAULT_DAYTRADE_TAX) -> float:
    """手續費(買+賣)+ 當沖證交稅(賣出一次),單位 %。與價格無關的固定部分。"""
    return (fee_rate * fee_discount * 2 + tax_rate) * 100.0


def round_trip_cost_pct(price: float, stock_id: str = "", *,
                        ticks_crossed: float = 2.0,
                        borrow_fee_pct: float = 0.0,
                        fee_rate: float = DEFAULT_FEE_RATE,
                        fee_discount: float = DEFAULT_FEE_DISCOUNT,
                        tax_rate: float = DEFAULT_DAYTRADE_TAX) -> float | None:
    """一筆當沖來回的總成本(%)。

    ticks_crossed 預設 2 = 進出都用市價/掃單各跨一次價差(當沖追價的常態)。
    若進出都掛限價等成交,可傳 0;一進一出傳 1。**這個參數會大幅改變結論,
    所以呼叫端要講清楚假設的是哪一種下單方式,不要偷偷用預設值當事實。**

    borrow_fee_pct:先賣後買若產生券差的借券費率(%)。實測 0.1%~1.0%+,
    做空時必須加進來 —— 不加的話空方成本會被系統性低估。
    """
    tp = tick_pct(price, stock_id)
    if tp is None:
        return None
    fixed = fee_tax_pct(fee_rate=fee_rate, fee_discount=fee_discount, tax_rate=tax_rate)
    return fixed + tp * float(ticks_crossed) + float(borrow_fee_pct or 0.0)


def breakeven_ticks(price: float, stock_id: str = "", **kw) -> float | None:
    """要賺幾個 tick 才打平(含跨價差)。直覺指標:>3 檔就代表這個價位帶很難做。"""
    tp = tick_pct(price, stock_id)
    cost = round_trip_cost_pct(price, stock_id, **kw)
    if tp is None or cost is None or tp <= 0:
        return None
    return cost / tp


# 成本分級門檻(%)。以實測分布定:最便宜約 0.52%、最貴約 1.27%。
_COST_GOOD = 0.60
_COST_BAD = 0.90


def cost_rating(price: float, stock_id: str = "", **kw) -> tuple[str, str]:
    """(等級, 給人看的說明)。等級 = cheap / normal / expensive。

    這個要顯示在標的池與推播卡上 —— 使用者選價格區間時必須當場看到代價,
    否則「自由選擇」只是把成本問題丟回給他自己踩。
    """
    cost = round_trip_cost_pct(price, stock_id, **kw)
    if cost is None:
        return ("unknown", "價格無效,無法估算成本")
    bt = breakeven_ticks(price, stock_id, **kw)
    t = tick_size(price, stock_id)
    detail = f"tick {t:g} 元、來回成本 {cost:.3f}%、需賺 {bt:.1f} 檔打平"
    if cost <= _COST_GOOD:
        return ("cheap", f"成本帶佳({detail})")
    if cost >= _COST_BAD:
        return ("expensive", f"⚠ 成本帶差({detail})——同策略在此價位帶要多賺一倍才回本")
    return ("normal", f"成本普通({detail})")


def risk_per_lot(price: float, stop_price: float) -> float | None:
    """一張(1000 股)從進場價到停損價的風險金額(元)。不含成本,純價差風險。"""
    try:
        p, s = float(price), float(stop_price)
    except (TypeError, ValueError):
        return None
    if p <= 0 or s <= 0:
        return None
    return abs(p - s) * 1000.0


def lots_for_quota(price: float, quota: float) -> int:
    """額度買得起幾張(無條件捨去)。現股當沖雖是買賣相抵,但券商仍設當沖額度上限,
    而且沒沖掉就要全額交割 —— 所以一律用「買得起幾張」保守估。"""
    try:
        p, q = float(price), float(quota)
    except (TypeError, ValueError):
        return 0
    if p <= 0 or q <= 0:
        return 0
    return int(q // (p * 1000.0))
