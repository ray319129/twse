"""大盤閘門(market regime)— 審查清單 3.3 完整版。

原本 index_below_ma20 只是裝飾:出現在 log/email 文案/JSON,但不影響 core_count / min_score /
任何決策。這裡把它變成真正的閘門:綜合三個免費訊號投票出「市場積極度」,動態調整每日選幾檔核心、
入榜門檻,並在弱盤時讓觸發偏好『回測轉強』而非『追突破』。

三組訊號(投票,每項 ±1):
  1. 指數技術面:加權指數 vs MA20(中期)、vs MA5(短期)、MA20 斜率(近 N 日是否上揚)
  2. 市場廣度(騰落):站上 MA20 的家數比、上漲家數比(全市場第一遍評分時已算,零額外 API)
  3. 漲跌停家數:漲停明顯多於跌停 → 偏多;反之偏空(要有一定家數才計入)

投票總和 → 依 config market.tiers 的 min_votes 門檻由高到低選第一個符合的級別,
輸出該級別的 core_count / min_score / prefer_pullback。config 缺 tiers 時用內建預設。
"""
from __future__ import annotations

import pandas as pd

# 內建預設分級(config market.tiers 未提供時使用)。對應審查清單:core 10/7/5/3、min_score 45/50/55/60。
_DEFAULT_TIERS = {
    "aggressive": {"min_votes": 3, "core_count": 10, "min_score": 45, "prefer_pullback": False},
    "neutral":    {"min_votes": 1, "core_count": 7,  "min_score": 50, "prefer_pullback": False},
    "cautious":   {"min_votes": -2, "core_count": 5, "min_score": 55, "prefer_pullback": True},
    "defensive":  {"min_votes": -99, "core_count": 3, "min_score": 60, "prefer_pullback": True},
}
_TIER_ORDER = ("aggressive", "neutral", "cautious", "defensive")
LEVEL_LABEL = {"aggressive": "積極", "neutral": "中性", "cautious": "保守", "defensive": "觀望"}


def compute_market_regime(index_close: "pd.Series | None", breadth: dict | None, cfg: dict | None) -> dict | None:
    """回傳 regime dict:{enabled, votes, level, core_count, min_score, prefer_pullback, detail}。
    market.enabled=false 或無任何可用訊號時回 None(呼叫端沿用 config 的固定 core_count/min_score)。"""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return None

    votes = 0
    used = False
    detail: dict = {}

    # ---------- 1. 指數技術面 ----------
    ma_long = int(cfg.get("ma_long", 20))
    ma_short = int(cfg.get("ma_short", 5))
    slope_lb = int(cfg.get("slope_lookback", 20))
    if index_close is not None and len(index_close) >= ma_long:
        s = pd.to_numeric(index_close, errors="coerce").dropna()
        if len(s) >= ma_long:
            cur = float(s.iloc[-1])
            mal = s.rolling(ma_long).mean()
            mas = s.rolling(ma_short).mean()
            if pd.notna(mal.iloc[-1]):
                above_long = cur > float(mal.iloc[-1])
                votes += 1 if above_long else -1
                detail["idx_above_ma_long"] = above_long
                used = True
            if pd.notna(mas.iloc[-1]):
                above_short = cur > float(mas.iloc[-1])
                votes += 1 if above_short else -1
                detail["idx_above_ma_short"] = above_short
                used = True
            if len(s) > ma_long + slope_lb and pd.notna(mal.iloc[-1]) and pd.notna(mal.iloc[-1 - slope_lb]):
                rising = float(mal.iloc[-1]) > float(mal.iloc[-1 - slope_lb])
                votes += 1 if rising else -1
                detail["idx_ma_long_rising"] = rising
                used = True

    # ---------- 2. 市場廣度(騰落)+ 3. 漲跌停家數 ----------
    breadth = breadth or {}
    n = int(breadth.get("n", 0))
    if n > 0:
        used = True
        above_ratio = breadth.get("above_ma20", 0) / n
        adv = int(breadth.get("adv", 0)); dec = int(breadth.get("dec", 0))
        adv_ratio = adv / (adv + dec) if (adv + dec) > 0 else 0.5
        bs = float(cfg.get("breadth_strong", 0.60)); bw = float(cfg.get("breadth_weak", 0.40))
        as_ = float(cfg.get("adv_strong", 0.60)); aw = float(cfg.get("adv_weak", 0.40))
        if above_ratio >= bs:
            votes += 1
        elif above_ratio <= bw:
            votes -= 1
        if adv_ratio >= as_:
            votes += 1
        elif adv_ratio <= aw:
            votes -= 1
        lu = int(breadth.get("limit_up", 0)); ld = int(breadth.get("limit_down", 0))
        min_lim = int(cfg.get("limit_min_count", 3))
        if lu + ld >= min_lim:
            if lu > ld * 1.5:
                votes += 1
            elif ld > lu * 1.5:
                votes -= 1
        detail.update(above_ma20_ratio=round(above_ratio, 2), adv_ratio=round(adv_ratio, 2),
                      limit_up=lu, limit_down=ld, scanned=n)

    if not used:
        return None

    # ---------- 依投票分級 ----------
    tiers = cfg.get("tiers") or _DEFAULT_TIERS
    chosen_name, chosen = None, None
    for name in _TIER_ORDER:
        t = tiers.get(name)
        if not t:
            continue
        if votes >= int(t.get("min_votes", -99)):
            chosen_name, chosen = name, t
            break
    if chosen is None:                       # config tiers 全缺 min_votes 門檻時的保底
        chosen_name, chosen = "neutral", _DEFAULT_TIERS["neutral"]

    return {
        "enabled": True,
        "votes": votes,
        "level": chosen_name,
        "level_label": LEVEL_LABEL.get(chosen_name, chosen_name),
        "core_count": int(chosen["core_count"]) if chosen.get("core_count") is not None else None,
        "min_score": float(chosen["min_score"]) if chosen.get("min_score") is not None else None,
        "prefer_pullback": bool(chosen.get("prefer_pullback", False)),
        "detail": detail,
    }
