"""當沖風控測試。重點:做空方向不能算反、強制回補不能漏送也不能連噴。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade.risk import (Position, check_stops, due_forced_close_level,
                                   forced_close_alert, position_risk_summary,
                                   stop_hit, unrealised_pct)


def _long(stop=97.0):
    return Position(stock_id="2330", name="台積電", side="long",
                    entry_price=100.0, lots=2, stop_price=stop)


def _short(stop=103.0):
    return Position(stock_id="2449", name="京元電", side="short",
                    entry_price=100.0, lots=2, stop_price=stop)


def test_unrealised_direction_is_inverted_for_shorts():
    """做空跌了才是賺 —— 這裡寫錯會讓停損判斷整個顛倒。"""
    assert unrealised_pct(_long(), 105.0) == 5.0
    assert unrealised_pct(_long(), 95.0) == -5.0
    assert unrealised_pct(_short(), 95.0) == 5.0
    assert unrealised_pct(_short(), 105.0) == -5.0


def test_stop_hit_direction():
    assert stop_hit(_long(97.0), 96.9) is True
    assert stop_hit(_long(97.0), 97.5) is False
    assert stop_hit(_short(103.0), 103.5) is True
    assert stop_hit(_short(103.0), 102.0) is False


def test_stop_hit_without_stop_is_never_true():
    assert stop_hit(Position("2330", "", "long", 100.0, 1, None), 1.0) is False


def test_check_stops_skips_closed_and_missing_quotes():
    closed = _long(); closed.closed = True
    alerts = check_stops([closed], {"2330": 90.0})
    assert alerts == []
    assert check_stops([_long()], {}) == []          # 沒報價就不判


def test_check_stops_reports_both_sides():
    alerts = check_stops([_long(97.0), _short(103.0)], {"2330": 96.0, "2449": 104.0})
    assert len(alerts) == 2
    assert all(a.urgency == "critical" for a in alerts)


def test_forced_close_only_sends_latest_due_level():
    """排程延遲或重啟時不能一次補送三則 —— 只送最後一個到期的。"""
    lv = due_forced_close_level("13:20", already_sent=set())
    assert lv is not None and lv[0] == "13:15"
    # 已送過 13:00 / 13:15,現在 13:26 → 應送 13:25
    lv2 = due_forced_close_level("13:26", already_sent={"13:00", "13:15"})
    assert lv2[0] == "13:25" and lv2[1] == "critical"
    # 全送過就不再送
    assert due_forced_close_level("13:29", {"13:00", "13:15", "13:25"}) is None
    # 還沒到時間
    assert due_forced_close_level("12:00", set()) is None


def test_forced_close_silent_when_no_open_positions():
    """沒有未平倉就不要吵人。"""
    lv = ("13:15", "warn", "剩 15 分鐘")
    assert forced_close_alert([], lv) is None
    closed = _long(); closed.closed = True
    assert forced_close_alert([closed], lv) is None


def test_forced_close_flags_short_settlement_risk():
    """有做空部位時,必須把券差→違約交割的後果講出來。"""
    lv = ("13:25", "critical", "剩 5 分鐘")
    a = forced_close_alert([_short()], lv, quotes={"2449": 101.0})
    assert a is not None and a.urgency == "critical"
    assert "違約交割" in a.message and "先賣後買" in a.message
    # 純多方部位不該出現這段嚇人的文字
    b = forced_close_alert([_long()], lv, quotes={"2330": 99.0})
    assert "違約交割" not in b.message


def test_position_risk_summary_against_250k_quota():
    s = position_risk_summary(_long(97.0), quota=250_000)
    assert s["notional"] == 200_000          # 100 元 × 2 張
    assert s["quota_pct"] == 80.0
    assert s["risk_amount"] == 6_000         # (100-97) × 1000 × 2
    assert s["risk_pct_of_quota"] == 2.4


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
