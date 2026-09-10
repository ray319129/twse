"""多週期支撐壓力(levels_mtf)—— 使用者 2026-09-10 要求加入日線/小時線/月線。

這一支的重點全在「**哪些水位不該出現**」:
月線回看 24 個月,對漲了好幾倍的股票會挖出幾年前的價位;不合併的話同一個價位
會在三個週期各留一條幾乎重疊的線,讓穿越判斷重複觸發。兩者都會直接變成
使用者抱怨的「訊號過多而且不準」。
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.daytrade import levels_mtf as M       # noqa: E402


def _daily(prices, start="2024-01-01"):
    """用一串收盤價造日線;高低各給 ±1%,足夠測擺動點與重採樣。"""
    idx = pd.date_range(start, periods=len(prices), freq="B")
    return pd.DataFrame({"open": prices,
                         "high": [p * 1.01 for p in prices],
                         "low": [p * 0.99 for p in prices],
                         "close": prices}, index=idx)


def test_merge_collapses_near_duplicates_and_sums_weight():
    """同一個價位在三個週期各出現一次 → 要合成**一個** zone,匯流度 3。

    不合併的話會變三條幾乎重疊的線:看不出匯流,穿越還會連觸發三次。
    """
    lv = [(100.0, "月線高"), (100.2, "日線高"), (99.9, "小時線高")]
    z = M.merge_zones(lv, 98.0, tolerance_pct=0.5)
    assert len(z) == 1, z
    assert z[0].weight == M.W_MONTH + M.W_DAY + M.W_HOUR
    assert z[0].to_dict()["confluence"] == 3
    assert z[0].kind == "resistance"


def test_merge_keeps_distinct_prices_apart():
    z = M.merge_zones([(100.0, "日線高"), (110.0, "日線高")], 105.0, tolerance_pct=0.4)
    assert len(z) == 2
    assert {x.kind for x in z} == {"support", "resistance"}


def test_zones_sorted_by_distance_from_reference():
    z = M.merge_zones([(120.0, "日線高"), (101.0, "日線高"), (90.0, "日線低")], 100.0)
    assert [x.price for x in z] == [101.0, 90.0, 120.0]


def test_relevant_range_uses_atr_when_it_is_wider():
    """範圍取 max(6%, 2×ATR) —— ATR 大的股票今天走得遠,不能只用固定百分比。"""
    assert M.relevant_range(100.0, atr=None) == 6.0
    assert M.relevant_range(100.0, atr=1.0) == 6.0          # 2×1 < 6
    assert M.relevant_range(100.0, atr=5.0) == 10.0         # 2×5 > 6


def test_build_drops_levels_from_years_ago():
    """**這是本支最重要的測試。**

    實測 2426 鼎元現價 99.5,月線卻挖出 20.11 / 16.71 / 14.81 的低點 ——
    那是好幾年前的價位,對當沖毫無意義,還會把權重最高的位置佔滿。
    """
    prices = [20.0] * 200 + [round(20 + i * 0.4, 2) for i in range(200)]
    df = _daily(prices)
    ref = prices[-1]
    zones = M.build("2426", df, ref, atr=ref * 0.03)
    assert zones, "近期價位應該還是要有水位"
    rng = M.relevant_range(ref, ref * 0.03)
    for z in zones:
        assert abs(z.price - ref) <= rng + 0.01, f"{z.price} 離現價 {ref} 太遠"
    assert all(z.price > 30 for z in zones), [z.price for z in zones]


def test_nearest_respects_side_and_min_weight():
    zones = [M.Zone(price=105.0, weight=1.0, kind="resistance", sources=["小時線高"]),
             M.Zone(price=110.0, weight=5.0, kind="resistance", sources=["月線高", "日線高"]),
             M.Zone(price=95.0, weight=2.0, kind="support", sources=["日線低"])]
    # 不設門檻 → 最近的壓力是 105
    assert M.nearest(zones, 100.0, "resistance").price == 105.0
    # 要求有日線以上份量 → 105 被排除(它只有小時線)
    assert M.nearest(zones, 100.0, "resistance", min_weight=M.W_DAY).price == 110.0
    assert M.nearest(zones, 100.0, "support").price == 95.0
    assert M.nearest([], 100.0, "support") is None


def test_nearest_never_returns_a_resistance_below_price():
    """壓力必須在現價**之上**。2026-09-09 台塑化就是因為「20 日高」落在現價下方,
    被當成壓力拿去壓目標價,做多目標算到進場之下(見 twse-daytrade-plan-invariants)。"""
    zones = [M.Zone(price=80.0, weight=9.0, kind="resistance", sources=["月線高"])]
    assert M.nearest(zones, 100.0, "resistance") is None


def test_month_levels_resample_is_zero_api():
    df = _daily([50 + i * 0.1 for i in range(300)])
    lv = M.month_levels(df)
    assert lv and all(s.startswith("月線") for _, s in lv)
    assert len(lv) <= M.MONTHS_BACK * 2


def test_day_levels_include_moving_averages():
    df = _daily([50 + (i % 7) for i in range(150)])
    srcs = {s for _, s in M.day_levels(df)}
    assert {"日線MA20", "日線MA60", "日線MA120"} <= srcs


def test_short_history_returns_nothing_rather_than_guessing():
    """資料不足就回空 —— 不要用估的湊數字讓人拿去下單。"""
    assert M.day_levels(_daily([50.0] * 10)) == []
    assert M.month_levels(pd.DataFrame()) == []
    assert M.merge_zones([(100.0, "日線高")], 0) == []


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception:
                fails += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()
    print("全部通過" if not fails else f"{fails} 項失敗")
    sys.exit(1 if fails else 0)
