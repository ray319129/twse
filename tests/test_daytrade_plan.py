"""當沖交易計畫測試。

重點:價格要能真的掛得進去(tick 對齊)、做空方向不能反、
**預計獲利一定要是扣完成本的淨額**(這是當沖最常見的自欺)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade.plan import (align_to_tick, build_plan, explain_plan,
                                   plan_quality)


def test_align_to_tick_respects_price_band():
    # 100~500 元 tick=0.5
    assert align_to_tick(123.37, "2330") == 123.5
    assert align_to_tick(123.37, "2330", mode="down") == 123.0
    assert align_to_tick(123.01, "2330", mode="up") == 123.5
    # 50~100 元 tick=0.1
    assert align_to_tick(74.37, "6770") == 74.4
    # ETF 兩級制
    assert align_to_tick(30.123, "0050") == 30.12


def test_align_rejects_invalid():
    for bad in (0, -1, None, "x"):
        assert align_to_tick(bad, "2330") is None


def _long(**kw):
    base = dict(stock_id="6770", side="long", ref_price=74.0, atr=2.2,
                quota=250000, cost_pct=0.61)
    base.update(kw)
    return build_plan(**base)


def test_long_plan_ordering():
    """做多:停損 < 進場 < 目標。順序錯了就是災難。"""
    p = _long()
    assert p is not None
    assert p.stop < p.entry < p.target


def test_short_plan_ordering_is_inverted():
    p = build_plan(stock_id="6770", side="short", ref_price=74.0, atr=2.2,
                   quota=250000, cost_pct=0.61)
    assert p is not None
    assert p.target < p.entry < p.stop


def test_all_prices_are_tick_aligned():
    """卡片上的價格必須是掛得進去的 —— 74.37 這種數字使用者沒時間自己換算。"""
    for side in ("long", "short"):
        p = build_plan(stock_id="6770", side=side, ref_price=74.37, atr=2.2,
                       quota=250000, cost_pct=0.61)
        for v in (p.entry, p.stop, p.target):
            assert abs(round(v / 0.1) * 0.1 - v) < 1e-6, f"{v} 不是 0.1 的倍數"


def test_net_profit_is_after_cost():
    """預計獲利必須扣成本。毛利沒有意義 —— 來回成本吃掉 0.5%~1.3%。"""
    p = _long()
    assert p.net_profit == p.gross_profit - p.cost_amount
    assert p.cost_amount > 0


def test_borrow_fee_only_charged_on_shorts():
    long_p = build_plan(stock_id="6770", side="long", ref_price=74.0, atr=2.2,
                        quota=250000, cost_pct=0.61, borrow_fee_pct=2.0)
    short_p = build_plan(stock_id="6770", side="short", ref_price=74.0, atr=2.2,
                         quota=250000, cost_pct=0.61, borrow_fee_pct=2.0)
    assert short_p.cost_amount > long_p.cost_amount


def test_target_capped_by_resistance():
    """1.5R 的目標若超過前高,改用前高並標記 —— 不要給一個到不了的目標。

    74.0 + 0.4×2.2×1.5 ≈ 75.3,所以壓力要設在那之下才會觸發修正。
    """
    p = build_plan(stock_id="6770", side="long", ref_price=74.0, atr=2.2,
                   quota=250000, cost_pct=0.61, resistance=74.8)
    assert p.target <= 74.8 and p.target_capped_by == "最近壓力"
    assert p.rr < 1.5          # 被壓下來所以實際風報比降低,要如實反映


def test_target_cap_symmetric_for_short():
    p = build_plan(stock_id="6770", side="short", ref_price=74.0, atr=2.2,
                   quota=250000, cost_pct=0.61, support=72.8)
    assert p.target >= 72.8 and p.target_capped_by == "最近支撐"


def test_missing_atr_returns_none():
    """算不出來就說沒有,不要用猜的數字讓人拿去下單。"""
    assert _long(atr=None) is None
    assert _long(atr=0) is None
    assert _long(ref_price=0) is None
    assert build_plan(stock_id="x", side="both", ref_price=10, atr=1,
                      quota=1e6, cost_pct=0.5) is None


def test_unaffordable_returns_none():
    assert build_plan(stock_id="2330", side="long", ref_price=1200.0, atr=30.0,
                      quota=250000, cost_pct=0.9) is None


def test_max_lots_caps_position():
    p = _long(max_lots=1)
    assert p.lots == 1


def test_position_sized_by_risk_budget_not_just_affordability():
    """只看「買得起」會讓停損寬的標的一次押掉太多風險。

    實測第一版:台半用滿額度時單筆最大虧損 10,766 元 = 25 萬的 4.3%。
    風險預算必須把它壓回上限內。
    """
    p = build_plan(stock_id="5425", side="long", ref_price=88.2, atr=4.82,
                   quota=250000, cost_pct=0.548, max_risk_pct=2.0)
    assert p is not None
    assert p.max_loss <= 250000 * 0.02 * 1.05    # 容許成本造成的些微超出
    # 關掉風險預算就會回到「買得起幾張」
    loose = build_plan(stock_id="5425", side="long", ref_price=88.2, atr=4.82,
                       quota=250000, cost_pct=0.548, max_risk_pct=0)
    assert loose.lots >= p.lots


def test_daytrade_stop_is_fraction_of_daily_atr():
    """ATR(14) 是整天的區間,當沖只持有幾小時 —— 停損用滿 1.0 倍會寬到不合理。

    預設 0.4 倍:88 元、ATR 4.82 的標的停損應落在 2~3%,不是 5%+。
    """
    p = build_plan(stock_id="5425", side="long", ref_price=88.2, atr=4.82,
                   quota=250000, cost_pct=0.548)
    assert 1.5 <= p.stop_pct <= 3.5, f"停損 {p.stop_pct}% 對當沖不合理"


def test_stop_has_minimum_distance():
    """ATR 極小時停損不能只差一跳,否則等於開盤就出場。"""
    p = build_plan(stock_id="6770", side="long", ref_price=74.0, atr=0.01,
                   quota=250000, cost_pct=0.61)
    assert p is not None
    assert abs(p.entry - p.stop) >= 0.2 - 1e-9    # 至少 2 個 tick(0.1×2)


def test_quality_flags_negative_net_profit():
    """扣完成本是負的 → 一定要標 bad,這是最常見的自欺。"""
    # 目標被緊貼的壓力壓到只剩一檔,而成本很高 → 扣完成本是負的。
    # (不能只靠「誇張成本」了:停損下限現在是成本的 2 倍,目標跟著等比拉開,
    #  單純把 cost_pct 調高反而算得出正淨利。要讓淨利為負,必須壓縮**目標距離**。)
    p = build_plan(stock_id="6770", side="long", ref_price=74.0, atr=2.0,
                   quota=250000, cost_pct=3.0, resistance=75.5)
    lvl, msg = plan_quality(p)
    assert lvl == "bad" and "虧的" in msg


def test_quality_none_plan():
    assert plan_quality(None)[0] == "none"


def test_explain_contains_actionable_numbers():
    p = _long()
    s = explain_plan(p, name="力積電", stock_id="6770")
    for token in (str(p.entry), str(p.target), str(p.stop), "淨賺", "最大賠"):
        assert token.rstrip("0").rstrip(".") in s or token in s


# ── 2026-09-09 實戰事故:Discord 推出「做多但目標低於進場」的卡片 ──
# 台塑化 6505 現價 87.45、20 日高 83.1(當天大漲 9.86% 早就站上 20 日高),
# 目標被壓力修正壓到 83 —— 比進場 87.5 還低,卻因為距離用 abs() 算,
# 卡片上顯示成「+5.14%、預計獲利 +7,976 元、風報比 2.65」,全部是假的。

def test_resistance_below_entry_must_not_cap_target():
    """突破日:股價已站上 20 日高,壓力低於進場價 —— 那條壓力對做多沒有意義,
    不可以拿它當目標(會產生比進場還低的目標)。"""
    p = build_plan(stock_id="6505", side="long", ref_price=87.45, atr=4.25,
                   quota=250000, cost_pct=0.585, resistance=83.1, support=68.6)
    assert p is not None
    assert p.target > p.entry, f"做多目標 {p.target} 不該低於進場 {p.entry}"
    assert p.target_capped_by is None, "壓力在進場之下時不該被當成目標"


def test_resistance_above_entry_still_caps():
    """壓力落在進場與原目標之間時,仍要正常修正 —— 不能因為修 bug 就把功能關掉。"""
    p = build_plan(stock_id="6505", side="long", ref_price=87.45, atr=4.25,
                   quota=250000, cost_pct=0.585, resistance=88.5)
    assert p.target_capped_by == "最近壓力"
    assert p.entry < p.target <= 88.5


def test_support_above_entry_must_not_cap_short_target():
    """做空的對稱情況:支撐高於進場價時不可拿來當目標。"""
    p = build_plan(stock_id="6505", side="short", ref_price=70.0, atr=3.0,
                   quota=250000, cost_pct=0.6, support=75.0)
    assert p is not None
    assert p.target < p.entry, f"做空目標 {p.target} 不該高於進場 {p.entry}"


def test_price_ordering_invariant_always_holds():
    """不變式安全網:掃一批參數組合,做多一律 停損<進場<目標、做空一律 目標<進場<停損。
    任何一段邏輯寫錯都應該在這裡被擋成 None,而不是產生看起來合理的錯誤價格。"""
    import itertools
    for side, px, atr, res, sup in itertools.product(
            ("long", "short"), (25.0, 87.45, 250.0), (0.5, 4.25),
            (None, 20.0, 83.1, 88.5, 300.0), (None, 20.0, 68.6, 90.0)):
        p = build_plan(stock_id="6505", side=side, ref_price=px, atr=atr,
                       quota=500000, cost_pct=0.6, resistance=res, support=sup)
        if p is None:
            continue
        if side == "long":
            assert p.stop < p.entry < p.target, (
                f"long {px}/{atr}/res={res}: {p.stop} {p.entry} {p.target}")
        else:
            assert p.target < p.entry < p.stop, (
                f"short {px}/{atr}/sup={sup}: {p.target} {p.entry} {p.stop}")
        assert p.net_profit == p.gross_profit - p.cost_amount


def test_risk_quota_is_separate_from_per_trade_capital():
    """**額度(能買多少)與風險基準(能賠多少)是兩件事,不可以共用一個參數。**

    2026-09-10 排錯抓到:`_plan_fields` 把「每筆可用資金」(總額度 × 50%)
    當成 `quota` 傳進來,而 `build_plan` 內部拿 `quota × max_risk_pct`
    當單筆風險預算 —— 於是風險預算變成真實額度的 1%,不是設定的 2%。
    結果是**靜默地**把 43 檔的計畫算成 None(風險預算連一張都買不起)。
    分開之後,同樣的資金上限但風險基準是完整額度,張數才對得上。
    """
    common = dict(stock_id="6770", side="long", ref_price=50.0, atr=1.0,
                  cost_pct=0.6, max_risk_pct=2.0)
    tight = build_plan(quota=125000, **common)                      # 舊寫法
    fixed = build_plan(quota=125000, risk_quota=250000, **common)   # 修好之後
    assert fixed is not None
    assert fixed.lots >= (tight.lots if tight else 0)
    # 資金上限仍然是 12.5 萬 —— 分開風險基準不等於放大部位
    assert fixed.notional <= 125000 * 1.001, fixed.notional


def test_risk_quota_defaults_to_quota_when_not_given():
    """沒傳 risk_quota 就沿用 quota —— 既有呼叫端的行為不能被這個改動改掉。"""
    a = build_plan(stock_id="6770", side="long", ref_price=50.0, atr=1.0,
                   quota=250000, cost_pct=0.6)
    b = build_plan(stock_id="6770", side="long", ref_price=50.0, atr=1.0,
                   quota=250000, risk_quota=250000, cost_pct=0.6)
    assert a is not None and b is not None
    assert a.lots == b.lots and a.stop == b.stop


def test_stop_is_never_narrower_than_a_round_trip_cost_floor():
    """**低波動金融股的停損整條都在雜訊帶裡。**

    兆豐金 ATR 小 × atr_stop_mult 0.4 → 停損只有 0.99%,而來回成本 0.719%,
    倍數 1.38。被雜訊掃掉時虧的錢跟手續費同一個量級,等於在付費擲硬幣。
    它能通過 edge_ratio 閘門是因為成本也低 —— 比值不能取代絕對距離。
    """
    p = build_plan(stock_id="2886", side="long", ref_price=45.0, atr=0.15,
                   quota=250000, cost_pct=0.72, min_stop_cost_mult=2.0)
    assert p is not None
    assert abs(p.entry - p.stop) / p.entry * 100 >= 0.72 * 2 - 1e-6, p.stop_pct
    # 空方對稱
    q = build_plan(stock_id="2886", side="short", ref_price=45.0, atr=0.15,
                   quota=250000, cost_pct=0.72, min_stop_cost_mult=2.0)
    assert q is not None and abs(q.stop - q.entry) / q.entry * 100 >= 0.72 * 2 - 1e-6


def test_cost_floor_does_not_shrink_an_already_wide_stop():
    """ATR 夠寬時下限不該生效 —— 下限是保護,不是覆蓋。"""
    wide = build_plan(stock_id="6770", side="long", ref_price=100.0, atr=5.0,
                      quota=250000, cost_pct=0.6, min_stop_cost_mult=2.0)
    none_ = build_plan(stock_id="6770", side="long", ref_price=100.0, atr=5.0,
                       quota=250000, cost_pct=0.6, min_stop_cost_mult=0.0)
    assert wide is not None and none_ is not None
    assert wide.stop == none_.stop


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
            except Exception as e:
                fails += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'全部通過' if not fails else str(fails) + ' 項失敗'}")
    sys.exit(1 if fails else 0)
