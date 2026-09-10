"""當沖訊號層測試:水位穿越、分級、停損建議。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade.signals import (  # noqa: F401
    _KIND_WEIGHT, Level, Signal, build_levels, detect_cross,
    rank_and_cap, score_signal, suggest_stop)


def _lv(price=100.0):
    return [Level("orh", price, "開盤區間上緣")]


def test_cross_needs_buffer():
    """貼著水位來回磨不算穿越 —— 沒有 buffer 會在水位附近瘋狂誤觸發。"""
    lv = _lv(100.0)
    assert detect_cross(prev_price=99.5, price=100.05, levels=lv, buffer_pct=0.002) == []
    got = detect_cross(prev_price=99.5, price=100.3, levels=lv, buffer_pct=0.002)
    assert len(got) == 1 and got[0][1] == "long"


def test_cross_is_symmetric_long_and_short():
    """同一條水位:上穿是多、下破是空。多空必須對稱。"""
    lv = _lv(100.0)
    up = detect_cross(prev_price=99.0, price=101.0, levels=lv)
    dn = detect_cross(prev_price=101.0, price=99.0, levels=lv)
    assert up[0][1] == "long"
    assert dn[0][1] == "short"


def test_first_poll_never_fires():
    """第一輪沒有 prev_price → 不猜。冷啟動一次噴一整天存量是踩過的坑。"""
    assert detect_cross(prev_price=None, price=100.0, levels=_lv()) == []
    assert detect_cross(prev_price=0, price=100.0, levels=_lv()) == []


def test_build_levels_skips_missing_inputs():
    """缺的水位就不放,不要用估的湊數。"""
    lv = build_levels(prev_high=None, prev_low=None, open_range_high=105.0,
                      open_range_low=None, vwap=None, price=100.0, use_round=False)
    assert [x.kind for x in lv] == ["orh"]


def test_build_levels_round_number_is_near_price_only():
    far = build_levels(prev_high=None, prev_low=None, open_range_high=None,
                       open_range_low=None, vwap=None, price=100.0)
    # 100 元 tick=0.5 → step 10 → 最近整數關卡就是 100,距離 0% 應納入
    assert any(x.kind == "round" for x in far)


def test_score_prefers_opening_range_over_round_number():
    """水位品質要反映在分數上:開盤區間 > 整數關卡。"""
    kw = dict(edge_ratio=8.0, volume_ratio=2.0, cost_pct=0.55,
              change_pct=1.0, side="long")
    orh, _ = score_signal(kind="orh", **kw)
    rnd, _ = score_signal(kind="round", **kw)
    assert orh > rnd


def test_score_penalises_chasing():
    """已經漲很多再追,分數要明顯下降 —— 台帳驗過追高期望值最差。"""
    kw = dict(kind="orh", edge_ratio=8.0, volume_ratio=2.0, cost_pct=0.55, side="long")
    fresh, _ = score_signal(change_pct=1.0, **kw)
    chased, _ = score_signal(change_pct=7.5, **kw)
    assert chased < fresh


def test_score_penalty_is_symmetric_for_shorts():
    """空方跌太多再追空,一樣要扣分。"""
    kw = dict(kind="orl", edge_ratio=8.0, volume_ratio=2.0, cost_pct=0.55, side="short")
    fresh, _ = score_signal(change_pct=-1.0, **kw)
    chased, _ = score_signal(change_pct=-7.5, **kw)
    assert chased < fresh


def test_score_rewards_edge_ratio():
    kw = dict(kind="orh", volume_ratio=1.5, cost_pct=0.55, change_pct=1.0, side="long")
    lo, _ = score_signal(edge_ratio=2.0, **kw)
    hi, _ = score_signal(edge_ratio=10.0, **kw)
    assert hi > lo


def _sig(sid, side, score):
    return Signal(stock_id=sid, name="", side=side, kind="orh", label="", price=100.0,
                  level=100.0, change_pct=1.0, volume_ratio=1.5, atr_pct=4.0,
                  cost_pct=0.55, edge_ratio=7.0, score=score)


def test_rank_caps_each_side_independently():
    """多空各自取前 N —— 強勢盤不該讓多方把空方的名額吃光。"""
    sigs = [_sig(f"L{i}", "long", 90 - i) for i in range(5)] + \
           [_sig(f"S{i}", "short", 88 - i) for i in range(5)]
    push, dropped = rank_and_cap(sigs, push_top_n=2, min_score=55)
    assert sum(1 for s in push if s.side == "long") == 2
    assert sum(1 for s in push if s.side == "short") == 2
    assert len(push) == 4 and len(dropped) == 6


def test_rank_drops_below_min_score():
    sigs = [_sig("A", "long", 80), _sig("B", "long", 40)]
    push, dropped = rank_and_cap(sigs, push_top_n=5, min_score=55)
    assert [s.stock_id for s in push] == ["A"]
    assert [s.stock_id for s in dropped] == ["B"]


def test_suggest_stop_direction_and_missing_atr():
    assert suggest_stop(price=100.0, side="long", atr=3.0) == 97.0
    assert suggest_stop(price=100.0, side="short", atr=3.0) == 103.0
    # 沒有 ATR 就不給數字 —— 不要讓人拿猜的停損去下單
    assert suggest_stop(price=100.0, side="long", atr=None) is None
    assert suggest_stop(price=0, side="long", atr=3.0) is None


def test_zones_are_reordered_by_distance_not_by_stored_weight():
    """**存檔的順序不可信。**

    標的池存的是 `levels_mtf.summary()` 的輸出,那是依**權重**排的。
    直接取前 N 個會留下「很重但今天走不到」的水位、砍掉近的 ——
    實測 6226 光鼎現價 29.65,存檔第 7 個是 24.50(離 17.4%),
    而 27.12(離 8.5%)排第 8。而且盤中價格早就離盤前基準走掉了。
    ⚠️ 這個測試附加在 runner 之前 —— 之後的測試不會被執行(踩過)。
    """
    zones = [{"price": 24.50, "confluence": 2, "timeframes": ["月線", "日線"]},
             {"price": 27.12, "confluence": 1, "timeframes": ["日線"]}]
    lv = build_levels(prev_high=None, prev_low=None, open_range_high=None,
                      open_range_low=None, vwap=None, price=29.65,
                      stock_id="6226", use_round=False, zones=zones, max_zones=1,
                      min_confluence=1)
    assert [x.price for x in lv] == [27.12], [x.price for x in lv]


def test_zone_hugging_current_price_is_dropped():
    """貼著現價的水位第一個 tick 就會「突破」—— 那不是突破,是雜訊。"""
    zones = [{"price": 100.1, "confluence": 3, "timeframes": ["月線", "日線", "小時線"]}]
    lv = build_levels(prev_high=None, prev_low=None, open_range_high=None,
                      open_range_low=None, vwap=None, price=100.0,
                      stock_id="2330", use_round=False, zones=zones)
    assert lv == [], lv


def test_zone_too_close_to_an_existing_level_is_dropped():
    """相距不到一次來回成本的兩條線在交易上是同一筆,留著會雙重觸發。"""
    zones = [{"price": 105.0, "confluence": 3, "timeframes": ["月線", "日線", "小時線"]}]
    lv = build_levels(prev_high=105.1, prev_low=None, open_range_high=None,
                      open_range_low=None, vwap=None, price=100.0,
                      stock_id="2330", use_round=False, zones=zones)
    assert [x.kind for x in lv] == ["prev_high"], [(x.kind, x.price) for x in lv]


def test_confluence_maps_to_weight_and_three_beats_opening_range():
    """三個週期都認得的價位比開盤區間還硬 —— 這是加入多週期的整個理由。"""
    zones = [{"price": 110.0, "confluence": 3, "timeframes": ["月線", "日線", "小時線"]},
             {"price": 95.0, "confluence": 1, "timeframes": ["小時線"]}]
    lv = build_levels(prev_high=None, prev_low=None, open_range_high=None,
                      open_range_low=None, vwap=None, price=100.0,
                      stock_id="2330", use_round=False, zones=zones,
                      min_confluence=1)
    kinds = {x.kind for x in lv}
    assert kinds == {"mtf3", "mtf1"}, kinds
    assert _KIND_WEIGHT["mtf3"] > _KIND_WEIGHT["orh"] > _KIND_WEIGHT["mtf1"]
    hi, _ = score_signal(kind="mtf3", edge_ratio=5.0, volume_ratio=1.5,
                         cost_pct=0.6, change_pct=1.0, side="long")
    lo, _ = score_signal(kind="round", edge_ratio=5.0, volume_ratio=1.5,
                         cost_pct=0.6, change_pct=1.0, side="long")
    assert hi > lo


def test_malformed_zones_never_crash_the_scan():
    """zones 來自 JSON,盤中掃描不能因為一個壞欄位就整輪掛掉。"""
    zones = [{"price": None}, {"price": "x"}, {}, {"price": -5, "confluence": 2},
             {"price": 110.0, "confluence": 2, "timeframes": ["月線", "日線"]}]
    lv = build_levels(prev_high=None, prev_low=None, open_range_high=None,
                      open_range_low=None, vwap=None, price=100.0,
                      stock_id="2330", use_round=False, zones=zones)
    assert [x.price for x in lv] == [110.0]


def test_single_timeframe_zones_are_gated_out_by_default():
    """匯流才是加入多週期的整個理由 —— 預設只收兩個週期以上背書的價位。

    實測若全收,單週期水位會佔新增水位的 38%(67/175),而它們的權重 0.6
    比 VWAP 還低:等於用最弱的水位把訊號量灌回去,正是使用者抱怨的
    「訊號過多而且不準」。開關在 config `levels.min_mtf_confluence`。
    """
    zones = [{"price": 110.0, "confluence": 1, "timeframes": ["小時線"]},
             {"price": 105.0, "confluence": 2, "timeframes": ["月線", "日線"]}]
    kw = dict(prev_high=None, prev_low=None, open_range_high=None,
              open_range_low=None, vwap=None, price=100.0,
              stock_id="2330", use_round=False, zones=zones)
    assert [x.price for x in build_levels(**kw)] == [105.0]
    assert [x.price for x in build_levels(**kw, min_confluence=1)] == [105.0, 110.0]
    assert build_levels(**kw, min_confluence=3) == []


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
