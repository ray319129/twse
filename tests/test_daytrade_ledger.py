"""當沖台帳測試。重點:報酬要扣成本、做空方向要對、追蹤時點不能被覆蓋。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade.ledger import Entry, record, summarise, update_followups
from scripts.daytrade.signals import Signal


def _entry(side="long", price=100.0, cost=0.52, fired="10:00", pushed=True):
    return Entry(stock_id="2330", name="台積電", side=side, kind="orh",
                 fired_at=fired, price=price, level=100.0, score=80.0,
                 cost_pct=cost, pushed=pushed)


def test_excess_is_net_of_cost():
    """方向對但走不到成本 = 白做,台帳必須誠實反映這件事。"""
    e = _entry()
    e.followup = {"30": 100.3}          # +0.30%,但成本 0.52%
    assert e.excess_pct(30) == -0.22
    e.followup = {"30": 101.0}          # +1.00%
    assert e.excess_pct(30) == 0.48


def test_excess_direction_for_shorts():
    e = _entry(side="short")
    e.followup = {"30": 99.0}           # 跌 1% → 做空賺 1%
    assert e.excess_pct(30) == 0.48
    e.followup = {"30": 101.0}          # 漲 1% → 做空賠
    assert e.excess_pct(30) == -1.52


def test_excess_missing_followup():
    assert _entry().excess_pct(30) is None


def test_update_followups_fills_only_due_and_never_overwrites():
    """5 分鐘那格一旦寫入就不能被後續輪詢改成最新價,否則追蹤沒有意義。"""
    e = _entry(fired="10:00")
    entries = [e]
    update_followups(entries, "10:05", {"2330": 101.0})
    assert e.followup == {"5": 101.0}                      # 只有 5 分鐘到期
    update_followups(entries, "10:20", {"2330": 105.0})
    assert e.followup["5"] == 101.0                        # 舊值不動
    assert e.followup["15"] == 105.0
    assert "30" not in e.followup


def test_update_followups_ignores_before_trigger():
    e = _entry(fired="10:00")
    update_followups([e], "09:30", {"2330": 99.0})
    assert e.followup == {}


def test_record_dedupes_same_stock_kind_side():
    sig = Signal(stock_id="2330", name="台積電", side="long", kind="orh", label="",
                 price=100.0, level=100.0, change_pct=1.0, volume_ratio=1.5,
                 atr_pct=4.0, cost_pct=0.52, edge_ratio=7.7, score=80.0, fired_at="10:00")
    entries = record([], sig, pushed=True)
    entries = record(entries, sig, pushed=True)
    assert len(entries) == 1
    sig2 = Signal(**{**sig.__dict__, "side": "short"})
    assert len(record(entries, sig2, pushed=False)) == 2   # 反向算不同筆


def test_summarise_separates_pushed_from_held():
    """pushed vs not_pushed 的差距就是訊號分級的價值 —— 必須分開算。"""
    good = _entry(pushed=True);  good.followup = {"30": 102.0}
    bad = _entry(pushed=False);  bad.followup = {"30": 99.0}
    s = summarise([good, bad], minutes=30)
    assert s["pushed"]["n"] == 1 and s["pushed"]["win_rate"] == 100.0
    assert s["not_pushed"]["n"] == 1 and s["not_pushed"]["win_rate"] == 0.0
    assert s["pushed"]["avg_excess_pct"] > s["not_pushed"]["avg_excess_pct"]


def test_summarise_handles_no_measurements():
    s = summarise([_entry()], minutes=30)
    assert s["pushed"]["measured"] == 0


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
