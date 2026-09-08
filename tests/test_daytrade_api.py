"""當沖同步 API 的清洗邏輯測試。

這一層是**公開端點**,而且會寫進 repo。所以測試重點在「不該收的有沒有擋掉」:
隱私界線(不收損益/餘額)與防呆(NaN、負數、亂碼代號、超量)。
不測 GitHub 寫入本身(那要網路),只測純函數。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.daytrade import _clean_positions, _clean_prefs, _valid_day


def test_prefs_only_whitelisted_fields():
    """前端多送的欄位不能寫進 repo —— 尤其是損益類。"""
    out = _clean_prefs({
        "quota_twd": 250000, "price_min": 20, "price_max": 300,
        "total_pnl": 123456, "account_balance": 999999,   # ← 不該被收
        "亂七八糟": 1,
    })
    assert out["quota_twd"] == 250000
    assert "total_pnl" not in out and "account_balance" not in out
    assert "亂七八糟" not in out


def test_prefs_rejects_nan_and_out_of_range():
    assert "quota_twd" not in _clean_prefs({"quota_twd": float("nan")})
    assert "quota_twd" not in _clean_prefs({"quota_twd": -5})
    assert "max_universe" not in _clean_prefs({"max_universe": 9999})
    assert "price_max" not in _clean_prefs({"price_max": "abc"})


def test_prefs_swaps_inverted_price_range():
    """min > max 會篩不到任何東西 —— 直接交換,不要寫進一組壞設定。"""
    out = _clean_prefs({"price_min": 300, "price_max": 20})
    assert out["price_min"] == 20 and out["price_max"] == 300


def test_prefs_booleans_and_id_lists():
    out = _clean_prefs({"allow_short": False,
                        "exclude": ["2330", "bad!", "", "00878"]})
    assert out["allow_short"] is False
    assert out["exclude"] == ["2330", "00878"]


def test_prefs_quota_is_int():
    assert isinstance(_clean_prefs({"quota_twd": 250000.9})["quota_twd"], int)


def _pos(**kw):
    base = {"stock_id": "2330", "name": "台積電", "side": "long",
            "entry_price": 100.0, "lots": 2, "stop_price": 97.0}
    base.update(kw)
    return base


def test_positions_keeps_only_needed_fields():
    """部位只收強制回補提醒需要的欄位,損益類一律不收。"""
    out = _clean_positions([_pos(unrealised_pnl=5000, account_total=1_000_000)])
    assert len(out) == 1
    assert set(out[0]) == {"stock_id", "name", "side", "entry_price", "lots",
                           "stop_price", "opened_at", "closed"}


def test_positions_rejects_bad_rows():
    bad = [
        _pos(stock_id="XX"),          # 代號格式錯
        _pos(side="both"),            # 方向錯
        _pos(entry_price=0),          # 價格錯
        _pos(lots=0),                 # 張數錯
        _pos(lots=99999),             # 張數離譜
        "not a dict",
    ]
    assert _clean_positions(bad) == []


def test_positions_stop_price_optional():
    out = _clean_positions([_pos(stop_price=None), _pos(stop_price=-3)])
    assert len(out) == 2
    assert all(o["stop_price"] is None for o in out)


def test_positions_short_side_preserved():
    """做空一定要保留 —— 強制回補提醒靠它判斷券差風險。"""
    out = _clean_positions([_pos(side="short", stock_id="2449")])
    assert out[0]["side"] == "short"


def test_positions_cap():
    out = _clean_positions([_pos(stock_id=f"{2000 + i}") for i in range(80)])
    assert len(out) == 50


def test_valid_day():
    assert _valid_day("2026-09-08") == "2026-09-08"
    for bad in ("2026/09/08", "abc", "", None, "../../etc/passwd"):
        got = _valid_day(bad)
        assert len(got) == 10 and got.count("-") == 2   # 一律退回今天,不吃路徑注入


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
