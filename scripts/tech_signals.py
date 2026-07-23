"""技術訊號標籤 + K線型態辨識(2026-07-23)。

使用者需求:「自動查看技術指標跟形態學」。與使用者討論後定案(見
memory/twse-pattern-recognition-research):
  ① 指標訊號標籤(布林/KD/MACD/均線/量價)—— 確定性規則,原料 indicators.py 全有
  ② K線型態 —— TA-Lib 61 種全掃,記錄全部、只「呈現」有共識的十幾種
  空方訊號(出貨/轉弱)與多方同等對待 —— 系統的弱點一直在出場端
  圖表形態(頭肩/雙底/三角)擱置;**任何標籤都不進信心分**,先顯示+記錄,
  累積後用 score_validate 那套框架驗過才談。

## 定位:給人看的,不是給機器交易的

大樣本回測(QuantifiedStrategies 75 型態 / 5000 檔 24 型態)結論:單獨使用無統計
意義,配 context 後少數有小 edge。使用者是人工混合型 —— 這些標籤的價值是**幫他
省下逐檔開圖的時間**(掃標籤決定哪檔值得看圖),不是預測。

## 布林通道規則的依據(使用者剛學,規則由研究文獻定)

- **收斂(squeeze)**:通道寬降到 120 日最窄附近 → 波動壓縮,醞釀變盤(方向未知!)。
  文獻:「narrowest in 100+ candles」。
- **帶量突破上軌**:收盤 > 上軌且量比 >= 1.5 —— 文獻強調**無量的突破多半是雜訊**,
  所以無量觸軌刻意不給標籤。
- **沿上軌行走(band walk)**:連 3 日貼著上軌 —— 這是**趨勢強勢的確認,不是反轉訊號**,
  文獻特別警告別把它當超買反手做空。
- **跌破下軌**:波動向下擴張,弱勢。

## 誠實邊界

- 指標標籤基於**還原價**算的指標(與全系統一致);K線型態用**原始 OHLC**
  (影線/實體是實際成交價的形狀;還原係數同一天等比縮放,比例不變,但跨除息日的
  缺口類型態用原始價才對)。
- 型態的「趨勢前置條件」由 TA-Lib 內建判斷(如錘子要求出現在下跌段)。
- 所有標籤描述的是**昨收(最後一根完成的日K)**的狀態;盤中看到時要自己記得
  「今天這根還沒收」。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .utils import log

# ---------------------------------------------------------------------------
# K線型態:TA-Lib 61 種
# ---------------------------------------------------------------------------

# 「呈現」用的共識型態(中文名 + 這個型態慣例上偏多還偏空)。
# 61 種**全部記錄**(供日後驗證),但卡片只顯示這份清單裡的 —— 其餘多是冷僻/爭議型態,
# 顯示出來只會洗版。TA-Lib 回傳 +100/-100 已含方向,這裡的 bias 只用來校對。
CONSENSUS = {
    "CDLENGULFING":       "吞噬",
    "CDLHAMMER":          "錘子",
    "CDLINVERTEDHAMMER":  "倒錘",
    "CDLHANGINGMAN":      "吊人線",
    "CDLSHOOTINGSTAR":    "流星",
    "CDLMORNINGSTAR":     "晨星",
    "CDLEVENINGSTAR":     "夜星",
    "CDLMORNINGDOJISTAR": "晨星十字",
    "CDLEVENINGDOJISTAR": "夜星十字",
    "CDLDARKCLOUDCOVER":  "烏雲罩頂",
    "CDLPIERCING":        "貫穿",
    "CDLHARAMI":          "母子",
    "CDLHARAMICROSS":     "十字母子",
    "CDL3WHITESOLDIERS":  "紅三兵",
    "CDL3BLACKCROWS":     "三烏鴉",
    "CDLDOJI":            "十字線",
    "CDLDRAGONFLYDOJI":   "蜻蜓十字",
    "CDLGRAVESTONEDOJI":  "墓碑十字",
    "CDLMARUBOZU":        "光頭光腳",
    "CDL3INSIDE":         "內困三日",
    "CDL3OUTSIDE":        "外側三日",
    "CDLABANDONEDBABY":   "棄嬰",
    "CDLKICKING":         "反衝",
    "CDLTASUKIGAP":       "跳空並列",
}

# ⚠️ TA-Lib 對「中性/看情境」的型態一律回 +100(它不做方向判斷),照 sign 直翻會把
# 十字線標成偏多、墓碑十字標成偏多 —— 都是錯的。這裡照教科書慣例覆寫:
# 十字線=變盤觀察(中性)、墓碑(長上影)=偏空、蜻蜓(長下影)=偏多。
SIDE_OVERRIDE = {
    "CDLDOJI": "warn",
    "CDLGRAVESTONEDOJI": "bear",
    "CDLDRAGONFLYDOJI": "bull",
}

_talib_warned = [False]


def candle_patterns(raw: pd.DataFrame, days: int = 1, all_hits: bool = False) -> list[dict]:
    """最後 `days` 根完成日K上觸發的型態。

    回傳 [{"code","t"(顯示名),"s"("bull"/"bear"),"date","consensus"}]。
    `all_hits=True` 連非共識型態也回(給記錄/驗證用);預設只回共識清單(給卡片)。
    TA-Lib 沒裝 → 回空 list、只警告一次 —— 型態是加分項,不該讓批次掛掉。
    """
    try:
        import talib
    except Exception as e:
        if not _talib_warned[0]:
            _talib_warned[0] = True
            log.warning(f"TA-Lib 未安裝,K線型態略過:{e}")
        return []
    if raw is None or len(raw) < 30:
        return []
    d = raw.dropna(subset=["close"]).tail(160)      # 型態函式要暖身(內建趨勢判斷),160 根綽綽有餘
    if len(d) < 30:
        return []
    try:
        o, h, l, c = (d["open"].values.astype(float), d["high"].values.astype(float),
                      d["low"].values.astype(float), d["close"].values.astype(float))
    except KeyError:
        return []
    out = []
    for fn in talib.get_function_groups()["Pattern Recognition"]:
        try:
            r = getattr(talib, fn)(o, h, l, c)
        except Exception:
            continue
        for i in range(max(0, len(r) - days), len(r)):
            v = int(r[i])
            if not v:
                continue
            consensus = fn in CONSENSUS
            if not (consensus or all_hits):
                continue
            out.append({
                "code": fn,
                "t": CONSENSUS.get(fn, fn.replace("CDL", "")),
                "s": SIDE_OVERRIDE.get(fn, "bull" if v > 0 else "bear"),
                "date": str(d.index[i].date()) if hasattr(d.index[i], "date") else str(d.index[i])[:10],
                "consensus": consensus,
            })
    return out


# ---------------------------------------------------------------------------
# 指標訊號標籤
# ---------------------------------------------------------------------------

def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def indicator_tags(ind: pd.DataFrame) -> list[dict]:
    """compute_all() 的輸出 → 訊號標籤清單。全部是確定性規則,規則依據見檔頭。

    回傳 [{"t"(標籤),"s"("bull"/"bear"/"warn"),"d"(細節,tooltip 用)}]。
    s 的語意:bull=偏多訊號、bear=偏空/出場警示、warn=中性但值得注意(變盤醞釀/過熱)。
    """
    if ind is None or len(ind) < 130:
        return []
    last, prev = ind.iloc[-1], ind.iloc[-2]
    tags: list[dict] = []

    close = _f(last.get("close")); open_ = _f(last.get("open"))
    high = _f(last.get("high"))
    bbw = _f(last.get("bb_width")); up = _f(last.get("bb_upper")); lo = _f(last.get("bb_lower"))
    k, dv = _f(last.get("k")), _f(last.get("d"))
    pk, pdv = _f(prev.get("k")), _f(prev.get("d"))
    hist, phist = _f(last.get("macd_hist")), _f(prev.get("macd_hist"))
    ma5, ma20, ma60 = _f(last.get("ma5")), _f(last.get("ma20")), _f(last.get("ma60"))
    pma5, pma20, pma60 = _f(prev.get("ma5")), _f(prev.get("ma20")), _f(prev.get("ma60"))
    pclose = _f(prev.get("close"))
    vr = _f(last.get("vol_ratio"))
    rsi = _f(last.get("rsi14"))

    # ---------- 布林通道 ----------
    if bbw is not None and len(ind) >= 120:
        w120 = pd.to_numeric(ind["bb_width"].tail(120), errors="coerce").dropna()
        if len(w120) >= 60 and bbw <= float(w120.min()) * 1.05:
            tags.append({"t": "布林收斂", "s": "warn",
                         "d": f"通道寬 {bbw:.3f} 為 120 日最窄附近 —— 波動壓縮,醞釀變盤(方向未知)"})
    if close is not None and up is not None and close > up:
        if vr is not None and vr >= 1.5:
            tags.append({"t": "帶量突破布林上軌", "s": "bull",
                         "d": f"收盤 {close:.2f} > 上軌 {up:.2f},量比 {vr:.2f} —— 無量的突破多半是雜訊,這根有量"})
        # 沿上軌行走:連 3 日收盤都在上軌 98% 之上 → 趨勢強勢確認,不是反轉訊號
        if len(ind) >= 3:
            t3 = ind.tail(3)
            walk = all(_f(r.get("close")) is not None and _f(r.get("bb_upper")) is not None
                       and _f(r.get("close")) >= _f(r.get("bb_upper")) * 0.98
                       for _, r in t3.iterrows())
            if walk:
                tags.append({"t": "沿上軌行走", "s": "bull",
                             "d": "連 3 日貼上軌 —— 趨勢強勢的確認;文獻警告別把它當超買反手放空"})
    if close is not None and lo is not None and close < lo:
        tags.append({"t": "跌破布林下軌", "s": "bear",
                     "d": f"收盤 {close:.2f} < 下軌 {lo:.2f} —— 波動向下擴張"})

    # ---------- KD ----------
    if None not in (k, dv, pk, pdv):
        if pk <= pdv and k > dv and dv < 30:
            tags.append({"t": "KD 低檔金叉", "s": "bull", "d": f"K {k:.0f} 上穿 D {dv:.0f}(低檔區)"})
        if pk >= pdv and k < dv and dv > 70:
            tags.append({"t": "KD 高檔死叉", "s": "bear", "d": f"K {k:.0f} 下穿 D {dv:.0f}(高檔區)"})
    if len(ind) >= 3:
        k3 = pd.to_numeric(ind["k"].tail(3), errors="coerce")
        if k3.notna().all() and (k3 > 80).all():
            tags.append({"t": "KD 高檔鈍化", "s": "warn",
                         "d": "K 值連 3 日 > 80 —— 強勢股常態,但追價風險高"})

    # ---------- MACD ----------
    if hist is not None and phist is not None:
        if phist <= 0 < hist:
            tags.append({"t": "MACD 柱轉正", "s": "bull", "d": "動能由空翻多"})
        if phist >= 0 > hist:
            tags.append({"t": "MACD 柱轉負", "s": "bear", "d": "動能由多翻空"})

    # ---------- 均線 ----------
    if None not in (ma5, ma20, ma60):
        spread = (max(ma5, ma20, ma60) - min(ma5, ma20, ma60)) / min(ma5, ma20, ma60)
        if spread <= 0.03:
            tags.append({"t": "均線糾結", "s": "warn",
                         "d": f"5/20/60 日線收斂在 {spread*100:.1f}% 內 —— 變盤前兆,等表態"})
        if None not in (pma5, pma20, pma60):
            now_bull = ma5 > ma20 > ma60
            was_bull = pma5 > pma20 > pma60
            if now_bull and not was_bull:
                tags.append({"t": "轉多頭排列", "s": "bull", "d": "5>20>60 首日成立"})
    if None not in (close, ma60, pclose, pma60):
        if pclose >= pma60 and close < ma60:
            tags.append({"t": "跌破季線", "s": "bear", "d": f"收盤 {close:.2f} 首日跌破 60 日線 {ma60:.2f}"})
        elif None not in (ma20, pma20) and pclose >= pma20 and close < ma20:
            tags.append({"t": "跌破月線", "s": "bear", "d": f"收盤 {close:.2f} 首日跌破 20 日線 {ma20:.2f}"})

    # ---------- 量價(空方與多方同等對待 —— 使用者明確要求) ----------
    if None not in (close, open_, vr) and open_ > 0:
        body = close / open_ - 1
        if vr >= 2 and body >= 0.025:
            tags.append({"t": "爆量長紅", "s": "bull", "d": f"量比 {vr:.1f},實體 {body*100:+.1f}%"})
        if vr >= 2 and body <= -0.025:
            tags.append({"t": "爆量長黑", "s": "bear", "d": f"量比 {vr:.1f},實體 {body*100:+.1f}% —— 出貨疑慮"})
        if None not in (high,) and vr >= 2:
            upper_shadow = high - max(close, open_)
            if upper_shadow >= abs(close - open_) * 2 and upper_shadow / close >= 0.015:
                tags.append({"t": "爆量長上影", "s": "bear",
                             "d": f"上影 {upper_shadow/close*100:.1f}% 且量比 {vr:.1f} —— 衝高遭壓"})

    # ---------- RSI 極端 ----------
    if rsi is not None:
        if rsi >= 85:
            tags.append({"t": "RSI 過熱", "s": "warn", "d": f"RSI {rsi:.0f} >= 85"})
        elif rsi <= 20:
            tags.append({"t": "RSI 超賣", "s": "warn", "d": f"RSI {rsi:.0f} <= 20"})

    return tags


def tags_for(raw: pd.DataFrame, ind: pd.DataFrame) -> dict:
    """一檔的完整標籤包(給 main.py 批次用)。
    i = 指標訊號(共識呈現)、p = 共識K線型態、p_all = 全部型態代碼(記錄/驗證用,精簡)。"""
    pats = candle_patterns(raw, days=1, all_hits=True)
    return {
        "i": indicator_tags(ind),
        "p": [{k: x[k] for k in ("t", "s")} for x in pats if x["consensus"]],
        "p_all": [f"{'+' if x['s']=='bull' else '-'}{x['code'][3:]}" for x in pats],
    }
