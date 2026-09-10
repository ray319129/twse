"""當沖層端到端整合測試 —— 用假報價驅動 scan_once,確認訊號真的會產生並被分級。

單元測試過不代表串起來會動:欄位名對不上、pool 與 quotes 沒接上、
第一輪就誤觸發 —— 這些只有整合測試抓得到。全程不打網路、不寫 repo 檔案。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.daytrade import engine as E
from scripts.daytrade import ledger as L
from scripts.daytrade import risk as R
from scripts.quotes import Quote


def _pool():
    row = {
        "stock_id": "2426", "name": "鼎元", "market": "twse", "price": 82.2,
        "atr_pct": 9.23, "edge_ratio": 16.35, "dollar_volume_m": 900.0,
        "tick": 0.1, "cost_pct": 0.564, "cost_rating": "cheap", "cost_note": "",
        "breakeven_ticks": 4.6, "borrow_fee_pct": None, "lots_affordable": 3,
        "gate_note": "閘門全過", "score": 88.0,
        # 昨日高低必須來自這裡(盤前算好的),不是 Quote 的 high/low ——
        # 那兩個是**今日盤中**的高低,拿來當昨高低既標錯也不可能正確觸發。
        "indicators": {"prev_high": 83.0, "prev_low": 79.5, "rsi": 55.0,
                       "high20": 88.0, "low20": 76.0},
    }
    return {"date": "2026-09-08", "prefs": {"quota_twd": 250000},
            "long": [dict(row, side="long")], "short": [dict(row, side="short")],
            "stats": {}}


def _quote(price, change=2.0, vr=1.8):
    return Quote(stock_id="2426", price=price, open=80.0, high=83.0, low=79.5,
                 prev_close=80.0, change_pct=change, volume=5000, vwap=81.0,
                 volume_ratio=vr, name="鼎元", source="mis", ts="2026-09-08 10:00:00")


class _Harness:
    """把 engine 的外部依賴換成假的,讓測試完全離線且可重現。"""

    def __init__(self, tmp: Path, hhmm="10:00", prices=(82.2,)):
        self.tmp = tmp
        self.hhmm = hhmm
        self.prices = list(prices)
        self.pushed_signals = []
        self.pushed_risk = []
        self._orig = {}

    def __enter__(self):
        import datetime as dt

        class _Now:
            def __init__(s, hhmm): s.hhmm = hhmm
            def strftime(s, f):
                return s.hhmm if f == "%H:%M" else "2026-09-08"
            def date(s): return date(2026, 9, 8)

        self._orig = {
            "now": E.now_tpe, "gq": E.get_quotes, "ss": E.sponsor_status,
            "its": E.in_trading_session, "ps": E._push_signals, "pr": E._push_risk,
            "ldir": L.LEDGER_DIR, "rdir": R.POS_DIR,
        }
        E.now_tpe = lambda: _Now(self.hhmm)
        E.get_quotes = lambda ids: {"2426": _quote(self.prices[-1])}
        E.sponsor_status = lambda: {"active": False}
        E.in_trading_session = lambda: True
        E._push_signals = lambda sigs: self.pushed_signals.extend(sigs)
        E._push_risk = lambda a: self.pushed_risk.append(a)
        L.LEDGER_DIR = self.tmp
        R.POS_DIR = self.tmp
        return self

    def __exit__(self, *a):
        E.now_tpe = self._orig["now"]; E.get_quotes = self._orig["gq"]
        E.sponsor_status = self._orig["ss"]; E.in_trading_session = self._orig["its"]
        E._push_signals = self._orig["ps"]; E._push_risk = self._orig["pr"]
        L.LEDGER_DIR = self._orig["ldir"]; R.POS_DIR = self._orig["rdir"]
        return False


def _tmp():
    import tempfile
    return Path(tempfile.mkdtemp(prefix="dt_test_"))


CFG = {"levels": {"opening_range_minutes": 15, "breakout_buffer": 0.002,
                  "use_round_numbers": True, "use_vwap": True},
       "ranking": {"push_top_n": 3, "min_score": 55, "cooldown_minutes": 20}}


def test_first_poll_fires_nothing():
    """第一輪只建立 prev_price 基準,不能觸發 —— 冷啟動噴一整天存量是踩過的坑。"""
    with _Harness(_tmp()) as h:
        state = {}
        r = E.scan_once(_pool(), state, CFG)
        assert r["ok"] and r["pushed"] == 0
        assert h.pushed_signals == []
        assert state["prev_price"]["2426"] == 82.2


def test_upward_cross_fires_long_signal():
    """第二輪價格穿過昨高 83.0(來自 indicators.prev_high),應產生做多訊號。"""
    with _Harness(_tmp()) as h:
        state = {}
        E.scan_once(_pool(), state, CFG)          # 基準輪 82.2
        h.prices.append(84.0)                      # 穿過 high=83.0
        r = E.scan_once(_pool(), state, CFG)
        assert r["pushed"] >= 1, r
        assert any(s.side == "long" for s in h.pushed_signals)
        s = h.pushed_signals[0]
        assert s.stock_id == "2426" and s.score >= 55
        assert s.degraded is True                  # 無訂閱 → 必須標記
        assert s.suggested_stop is not None and s.suggested_stop < s.price


def test_prev_high_comes_from_pool_not_from_todays_quote():
    """昨日高低一定要取盤前算好的那份。

    2026-09-10 排錯:原本 `_levels_for` 傳的是 `quote.high` / `quote.low`,
    而那是**今日盤中**的最高最低。後果是 (a) 卡片標「昨日高點」卻顯示今天的,
    (b) 今日高點依定義 ≥ 現價且隨現價移動,`detect_cross` 的
    `price >= level×1.002` 永遠不會成立 —— 這條水位只會在報價欄位不同步時
    誤觸發。權重 0.85 的第二重要水位變成雜訊產生器。
    """
    cand = dict(_pool()["long"][0])
    cand["indicators"] = {"prev_high": 100.0, "prev_low": 90.0}
    q = _quote(95.0)                       # 今日高 83.0 / 低 79.5,與昨日高低無關
    levels = E._levels_for(cand, q, CFG, {})
    by_kind = {lv.kind: lv.price for lv in levels}
    assert by_kind.get("prev_high") == 100.0, by_kind
    assert by_kind.get("prev_low") == 90.0, by_kind
    assert 83.0 not in by_kind.values(), "今日盤中高點不該出現在水位裡"


def test_zone_bounds_picks_confluent_zones_around_live_price():
    """盤中的壓力/支撐要依**即時價**重挑,而且只認有兩個以上週期背書的。"""
    zones = [{"price": 88.0, "confluence": 2}, {"price": 86.0, "confluence": 1},
             {"price": 80.0, "confluence": 3}, {"price": 84.5, "confluence": 1}]
    res, sup = E._zone_bounds(zones, 85.0)
    assert res == 88.0, "86.0 只有一個週期背書,不該當壓力"
    assert sup == 80.0, "84.5 只有一個週期背書,不該當支撐"
    assert E._zone_bounds([], 85.0) == (None, None)
    assert E._zone_bounds(None, 85.0) == (None, None)


def test_downward_cross_fires_short_signal():
    with _Harness(_tmp()) as h:
        state = {}
        E.scan_once(_pool(), state, CFG)          # 基準 82.2
        h.prices.append(79.0)                      # 跌破 low=79.5
        E.scan_once(_pool(), state, CFG)
        assert any(s.side == "short" for s in h.pushed_signals), \
            [(s.side, s.kind) for s in h.pushed_signals]


def test_same_level_never_fires_twice():
    """當日去重:同一檔同一水位同一方向只推一次。"""
    with _Harness(_tmp()) as h:
        state = {}
        E.scan_once(_pool(), state, CFG)
        h.prices.append(84.0)
        E.scan_once(_pool(), state, CFG)
        n1 = len(h.pushed_signals)
        h.prices.append(82.0)                      # 回落
        E.scan_once(_pool(), state, CFG)
        h.prices.append(84.5)                      # 再穿一次
        E.scan_once(_pool(), state, CFG)
        assert len(h.pushed_signals) == n1, "同一水位重複觸發了"


def test_ledger_records_pushed_and_followups():
    """台帳要記下推播,並在時間到時補後續價格。"""
    tmp = _tmp()
    with _Harness(tmp) as h:
        state = {}
        E.scan_once(_pool(), state, CFG)
        h.prices.append(84.0)
        E.scan_once(_pool(), state, CFG)
        entries = L.load(date(2026, 9, 8))
        assert len(entries) >= 1
        e = entries[0]
        assert e.pushed is True and e.price == 84.0
        # 15 分鐘後再掃一次 → 應補上 "5" 與 "15"
        h.hhmm = "10:15"
        h.prices.append(85.0)
        E.scan_once(_pool(), state, CFG)
        e2 = L.load(date(2026, 9, 8))[0]
        assert e2.followup.get("5") is not None and e2.followup.get("15") is not None


def test_forced_close_pushes_when_position_open():
    """13:15 有未平倉部位 → 必須推風控提醒,而且與訊號分開發。"""
    tmp = _tmp()
    with _Harness(tmp, hhmm="13:15") as h:
        R.save_positions([R.Position(stock_id="2426", name="鼎元", side="short",
                                     entry_price=82.0, lots=1, stop_price=85.0)],
                         date(2026, 9, 8))
        state = {}
        E.scan_once(_pool(), state, CFG)
        assert len(h.pushed_risk) >= 1
        msg = h.pushed_risk[-1].message
        assert "未平倉" in msg and "違約交割" in msg      # 空單要講清楚後果
        # 送出 13:15 時,比它更早的 13:00 要一併標成已送 —— 否則下一輪會倒退
        # 送一次比較不急的提醒(這是整合測試抓到的 bug)。
        assert state["forced_sent"] == ["13:00", "13:15"]
        # 同一時點不重送,也不倒退送
        E.scan_once(_pool(), state, CFG)
        assert sum(1 for a in h.pushed_risk if a.kind == "forced_close") == 1


def test_stop_hit_pushes_critical():
    tmp = _tmp()
    with _Harness(tmp, prices=(82.2,)) as h:
        R.save_positions([R.Position(stock_id="2426", name="鼎元", side="long",
                                     entry_price=90.0, lots=1, stop_price=85.0)],
                         date(2026, 9, 8))
        E.scan_once(_pool(), {}, CFG)              # 現價 82.2 < 停損 85
        hits = [a for a in h.pushed_risk if a.kind == "stop_hit"]
        assert hits and hits[0].urgency == "critical"


def test_plan_embed_has_every_field_the_user_asked_for():
    """使用者要求卡片上要有:進場/出場/停損/預計獲利/預計成本/為什麼推薦/指標/參考資料。
    少一個就是沒做到,所以直接把清單釘進測試。"""
    c = dict(_pool()["long"][0])
    c["plan"] = {"side": "long", "entry": 82.2, "stop": 80.5, "target": 84.8,
                 "stop_pct": 2.07, "target_pct": 3.16, "rr": 1.53, "lots": 3,
                 "notional": 246600, "cost_amount": 1390, "gross_profit": 7800,
                 "net_profit": 6410, "max_loss": 6490, "target_capped_by": None,
                 "tick": 0.1}
    c["reasons"] = ["日均波動 ATR 9.2%", "RSI 55(中性)"]
    c["indicators"] = {"rsi": 55.0, "ma20": 80.1, "high20": 88.0, "low20": 76.0,
                       "vol_ratio": 1.3}
    c["plan_note"] = "風報比 1.53"
    e = E._plan_embed(c, "long", title_prefix="▲ 做多")
    names = [f["name"] for f in e["fields"]]
    for need in ("進場", "目標", "停損", "預計獲利", "最大虧損", "預計成本",
                 "風報比", "為什麼推薦", "指標", "參考"):
        assert need in names, f"卡片缺少「{need}」"
    profit = next(f["value"] for f in e["fields"] if f["name"] == "預計獲利")
    assert "已扣成本" in profit, "預計獲利必須標明已扣成本"
    ref = next(f["value"] for f in e["fields"] if f["name"] == "參考")
    assert "cmoney" in ref and "yahoo" in ref.lower() and "mops" in ref


def test_plan_embed_skipped_when_no_plan():
    """沒有交易計畫就不發卡 —— 只有代號和成本的卡片沒有用,反而佔版面。"""
    c = dict(_pool()["long"][0]); c["plan"] = None
    assert E._plan_embed(c, "long") is None


def test_premarket_push_sends_both_sides():
    sent = {}
    orig = E.send_discord
    E.send_discord = lambda embeds, content="", **kw: sent.update(
        {"n": len(embeds), "content": content, "titles": [e["title"] for e in embeds]})
    try:
        pool = _pool()
        plan = {"side": "long", "entry": 82.2, "stop": 80.5, "target": 84.8,
                "stop_pct": 2.07, "target_pct": 3.16, "rr": 1.53, "lots": 3,
                "notional": 246600, "cost_amount": 1390, "gross_profit": 7800,
                "net_profit": 6410, "max_loss": 6490, "target_capped_by": None, "tick": 0.1}
        pool["long"][0]["plan"] = dict(plan)
        pool["short"][0]["plan"] = dict(plan, side="short")
        n = E.push_premarket_picks(pool, top_n=1)
        assert n == 2 and sent["n"] == 2
        assert any("做多" in t for t in sent["titles"])
        assert any("做空" in t for t in sent["titles"])
        assert "盤前精選" in sent["content"]
    finally:
        E.send_discord = orig


def test_premarket_push_silent_when_no_plans():
    calls = []
    orig = E.send_discord
    E.send_discord = lambda *a, **k: calls.append(1)
    try:
        assert E.push_premarket_picks({"long": [], "short": []}) == 0
        assert not calls
    finally:
        E.send_discord = orig


def test_empty_pool_is_safe():
    with _Harness(_tmp()):
        r = E.scan_once({"long": [], "short": []}, {}, CFG)
        assert r["ok"] is False and r["reason"] == "empty_pool"


# ── 2026-09-09 實戰事故:「觸發 整數關卡 85」但即時價 87.45 ──
# 台塑化昨收 75.8。get_quotes 的最後一層降級會回本機昨收當 price,
# 那個值進到 prev_price 之後,下一輪拿到真實價 87.45 就「穿越」了
# 75.8~87.45 之間的每一條水位 —— 85 正好在中間,於是在 13:17 推出
# 一個早上就該觸發完的假訊號。

def test_stale_close_quote_must_not_create_phantom_cross():
    """報價降級成「昨收」時必須整輪跳過,不能拿昨天的價格當 prev_price。"""
    from scripts.quotes import Quote
    with _Harness(_tmp()) as h:
        # 第一輪:報價層降級,回昨收 75.8(source="close")
        E.get_quotes = lambda ids: {"2426": Quote(
            stock_id="2426", price=75.8, prev_close=75.8, name="鼎元",
            source="close", ts="")}
        state = {}
        r1 = E.scan_once(_pool(), state, CFG)
        assert r1["ok"] is False and r1["reason"] == "no_live_quotes"
        assert "2426" not in state.get("prev_price", {}), "昨收不該進 prev_price"

        # 第二輪:真實價 87.45。若上一輪汙染了 prev_price,這裡會假穿越一堆水位
        E.get_quotes = lambda ids: {"2426": _quote(87.45)}
        E.scan_once(_pool(), state, CFG)
        assert h.pushed_signals == [], "不該因為昨收→現價的跳躍而觸發"


def test_near_limit_up_is_not_recommended_long():
    """漲停附近不推做多:買不到,而且隔天跳空風險完全不對稱。"""
    with _Harness(_tmp()) as h:
        state = {}
        E.get_quotes = lambda ids: {"2426": _quote(82.2, change=1.0)}
        E.scan_once(_pool(), state, CFG)            # 建基準
        E.get_quotes = lambda ids: {"2426": _quote(84.0, change=9.5)}
        E.scan_once(_pool(), state, CFG)            # 穿越昨高,但已漲 9.5%
        assert not [s for s in h.pushed_signals if s.side == "long"],             "已漲 9.5% 不該再推做多"


def test_near_limit_down_is_not_recommended_short():
    with _Harness(_tmp()) as h:
        state = {}
        E.get_quotes = lambda ids: {"2426": _quote(82.2, change=-1.0)}
        E.scan_once(_pool(), state, CFG)
        E.get_quotes = lambda ids: {"2426": _quote(79.0, change=-9.5)}
        E.scan_once(_pool(), state, CFG)
        assert not [s for s in h.pushed_signals if s.side == "short"],             "已跌 9.5% 不該再推做空"


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
