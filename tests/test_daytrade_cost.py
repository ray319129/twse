"""當沖成本計算的單元測試。

這是本專案第一組自動化測試(2026-09-08)。挑這裡起頭的理由:
成本計算是純函數、沒有 I/O、而且**算錯不會報錯只會靜靜地讓每筆交易少賺**
—— 正是最需要被釘住的那種程式碼。

跑法:  python -m pytest tests/ -q      或   python tests/test_daytrade_cost.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade import cost as C


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_tick_size_bands():
    """六級距的邊界要精確 —— 邊界值歸屬錯誤會讓成本算錯一整級。"""
    cases = [
        (5.0, 0.01), (9.99, 0.01),
        (10.0, 0.05), (49.99, 0.05),
        (50.0, 0.10), (99.99, 0.10),
        (100.0, 0.50), (499.99, 0.50),
        (500.0, 1.00), (999.99, 1.00),
        (1000.0, 5.00), (2500.0, 5.00),
    ]
    for price, expect in cases:
        got = C.tick_size(price)
        assert _close(got, expect), f"tick_size({price}) = {got}, 應為 {expect}"


def test_tick_size_etf_is_two_tier():
    """ETF 是兩級制,不能套一般股票的六級 —— 套錯會把 0050 的成本算高 5 倍。"""
    assert _close(C.tick_size(30.0, "0050"), 0.01)
    assert _close(C.tick_size(49.99, "006208"), 0.01)
    assert _close(C.tick_size(50.0, "0050"), 0.05)
    assert _close(C.tick_size(250.0, "00878"), 0.05)
    # 同價位的一般股票 tick 完全不同
    assert _close(C.tick_size(250.0, "2330"), 0.50)


def test_is_etf_does_not_misclassify_normal_stocks():
    assert C.is_etf("0050") and C.is_etf("00878") and C.is_etf("006208")
    for sid in ("2330", "1101", "6290", "2449", ""):
        assert not C.is_etf(sid), f"{sid} 不該被當成 ETF"


def test_invalid_price_never_raises():
    """背景計算不准丟例外 —— 停牌/未開盤的 0 或 None 會一路傳進來。"""
    for bad in (0, -5, None, "abc", float("nan")):
        assert C.tick_size(bad) > 0
    assert C.tick_pct(0) is None
    assert C.round_trip_cost_pct(0) is None
    assert C.breakeven_ticks(0) is None


def test_fee_tax_is_fixed_portion():
    """手續費×2 + 當沖稅 = 0.321%,與價格無關。這個數字變了代表稅制或折扣改了。"""
    assert _close(C.fee_tax_pct(), 0.3210, tol=1e-4)


def test_the_2_4x_cost_gap_across_tick_boundary():
    """本模組存在的理由:99 元 vs 105 元,同策略成本差約 2.4 倍。
    這條測試釘住的是「價格區間會決定成本」這個結論本身。"""
    cheap = C.round_trip_cost_pct(99.0)
    pricey = C.round_trip_cost_pct(105.0)
    assert _close(cheap, 0.5230, tol=1e-3), cheap
    assert _close(pricey, 1.2731, tol=1e-3), pricey
    assert pricey / cheap > 2.3, f"成本落差只有 {pricey/cheap:.2f} 倍,與實測不符"


def test_cost_rating_flags_the_dangerous_band():
    """tick 換檔正上方要被標 expensive,正下方要被標 cheap。"""
    assert C.cost_rating(105.0)[0] == "expensive"
    assert C.cost_rating(120.0)[0] == "expensive"
    assert C.cost_rating(1200.0)[0] == "expensive"
    for good in (49.0, 99.0, 480.0, 990.0):
        assert C.cost_rating(good)[0] == "cheap", f"{good} 應為 cheap"


def test_ticks_crossed_assumption_matters():
    """掛限價等成交(0 檔)與追價掃單(2 檔)成本差很多,預設不能被當成事實。"""
    maker = C.round_trip_cost_pct(105.0, ticks_crossed=0)
    taker = C.round_trip_cost_pct(105.0, ticks_crossed=2)
    assert _close(maker, C.fee_tax_pct(), tol=1e-6)
    assert taker > maker * 3


def test_borrow_fee_is_added_for_shorts():
    """做空的券差借券費必須加進成本,否則空方成本被系統性低估。"""
    base = C.round_trip_cost_pct(99.0)
    with_borrow = C.round_trip_cost_pct(99.0, borrow_fee_pct=1.0)
    assert _close(with_borrow - base, 1.0, tol=1e-9)


def test_breakeven_ticks_is_cost_over_tick():
    for p in (25.0, 99.0, 105.0, 480.0):
        bt = C.breakeven_ticks(p)
        assert _close(bt, C.round_trip_cost_pct(p) / C.tick_pct(p), tol=1e-9)


def test_risk_per_lot_and_quota():
    assert _close(C.risk_per_lot(100.0, 97.0), 3000.0)
    assert _close(C.risk_per_lot(97.0, 100.0), 3000.0)   # 做空方向也要對
    assert C.risk_per_lot(0, 10) is None
    # 25 萬額度
    assert C.lots_for_quota(99.0, 250_000) == 2      # 99*1000=99,000 → 2 張
    assert C.lots_for_quota(300.0, 250_000) == 0     # 買不起一張
    assert C.lots_for_quota(25.0, 250_000) == 10
    assert C.lots_for_quota(0, 250_000) == 0


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
    print(f"\n{'全部通過' if not fails else str(fails) + ' 項失敗'}")
    sys.exit(1 if fails else 0)
