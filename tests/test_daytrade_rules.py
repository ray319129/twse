"""當沖法規閘門的單元測試。

這一層錯的代價**不對稱**:少推幾個機會只是少賺,推了一個不能做空的標的
可能變成券差 → 違約交割。所以測試重點全放在「該擋的有沒有擋住」。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade.rules import Gate, gate_for, in_roc_period


def test_gate_defaults_to_not_tradeable():
    """fail closed:查不到的標的一律不可做,不是預設可做。"""
    g = Gate(stock_id="9999")
    assert not g.eligible and not g.long_ok and not g.short_ok
    assert gate_for({}, "2330").long_ok is False


def test_short_suspended_blocks_short_only():
    """Suspension=Y 是『暫停先賣後買』,只擋空方 —— 擋掉多方會少掉一堆可做標的。
    實測名單裡有台泥、瑞昱這種大型股(原因是除權息停止過戶)。"""
    g = Gate(stock_id="1101", eligible=True, short_suspended=True)
    assert g.long_ok is True
    assert g.short_ok is False
    assert "暫停先賣後買" in g.explain()


def test_disposed_blocks_both_sides():
    g = Gate(stock_id="2455", eligible=True, disposed=True)
    assert g.long_ok is False and g.short_ok is False
    assert "處置股" in g.explain()


def test_warned_blocks_nothing_but_shows():
    """注意股只標示不擋單 —— 但要看得見,因為它常是處置的前一步。"""
    g = Gate(stock_id="2221", eligible=True, warned=True)
    assert g.long_ok and g.short_ok
    assert "注意股" in g.explain()


def test_borrow_fee_shows_in_explain():
    g = Gate(stock_id="5425", eligible=True, borrow_fee_pct=3.0)
    assert "借券費 3%" in g.explain()


def test_gate_all_clear():
    assert Gate(stock_id="2330", eligible=True).explain() == "閘門全過"


def test_roc_period_both_formats():
    """處置期間實測有兩種格式,都要判得出來。"""
    today = date(2026, 9, 8)
    assert in_roc_period("1150907~1150914", today) is True      # 緊湊格式,今天在期間內
    assert in_roc_period("115/09/07～115/09/15", today) is True  # 斜線 + 全形波浪
    assert in_roc_period("1150901~1150905", today) is False     # 已結束
    assert in_roc_period("1150910~1150915", today) is False     # 還沒開始


def test_roc_period_unparseable_fails_closed():
    """看不懂的格式一律當成『在期間內』—— 寧可誤擋也不要誤放一檔處置股。"""
    today = date(2026, 9, 8)
    for weird in ("民國115年9月", "TBD~TBD", "1150907", "亂碼~~~"):
        assert in_roc_period(weird, today) is True, f"{weird} 應 fail closed"
    assert in_roc_period("", today) is False        # 空字串 = 沒有處置,不是未知


def test_to_dict_exposes_computed_flags():
    """to_dict 要把 long_ok / short_ok 攤平出去 —— 快取與前端都靠它,
    不能只存原始欄位讓下游自己重算(重算就會有兩套邏輯)。"""
    d = Gate(stock_id="1101", eligible=True, short_suspended=True).to_dict()
    assert d["long_ok"] is True and d["short_ok"] is False
    assert "explain" in d


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
