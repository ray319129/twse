"""技術面(Technical)Engine — 個股健檢面向五。

大部分複用 scripts/indicators.py 已手刻的指標(MA/KD/MACD/RSI/ATR/布林/量能),
ADX 是 2026-06-30 新增進 indicators.py 的新函式(獨立於既有 compute_all() 輸出,
不影響短線信心分管線)。

跟 scripts/scoring.compute_conviction 的 trend/setup 概念有重疊但目的不同:
這裡是「現在的技術面體質好不好」給長期投資人也能看,不是「今天有沒有新鮮觸發訊號」。
"""
from __future__ import annotations
import pandas as pd

from .metric import metric, missing_metric, engine_result, status_from_delta, avg_score, clip01
from ..indicators import adx as _adx, reference_levels

_SRC = "本機價格資料(yfinance)+ 手刻技術指標(scripts/indicators.py)"


def compute(ctx: dict) -> dict:
    df = ctx.get("price_df")
    updated = ctx.get("updated_at", "")
    if df is None or df.empty or len(df) < 60:
        return engine_result(None, [missing_metric("technical", "技術面資料", source=_SRC,
                                                    reason="api_unavailable")],
                             notes=["價格資料不足(< 60 根 K 棒),技術面無法計算。"])

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else None
    close = float(last["close"])
    asof = str(df.index[-1].date())

    metrics: list[dict] = []
    trend_bits: list[float] = []
    momentum_bits: list[float] = []
    volume_bits: list[float] = []

    # ---------- 均線排列 ----------
    ma_keys = ("ma5", "ma20", "ma60", "ma120", "ma240")
    mas = {k: (float(last[k]) if k in last.index and pd.notna(last[k]) else None) for k in ma_keys}
    bullish_bits = [
        mas["ma5"] is not None and close > mas["ma5"],
        mas["ma20"] is not None and mas["ma60"] is not None and mas["ma20"] > mas["ma60"],
        mas["ma60"] is not None and close > mas["ma60"],
        mas["ma60"] is not None and mas["ma120"] is not None and mas["ma60"] > mas["ma120"],
        mas["ma120"] is not None and mas["ma240"] is not None and mas["ma120"] > mas["ma240"],
    ]
    alignment_score = sum(1 for b in bullish_bits if b) / len(bullish_bits)
    alignment_label = "多頭排列" if alignment_score >= 0.8 else ("空頭排列" if alignment_score <= 0.2 else "盤整/排列不明確")
    metrics.append(metric(
        "ma_alignment", "均線排列", alignment_label,
        rating=("good" if alignment_score >= 0.8 else ("bad" if alignment_score <= 0.2 else "neutral")),
        formula="5條多頭排列子條件(收盤>5MA、20MA>60MA、收盤>60MA、60MA>120MA、120MA>240MA)命中比例",
        source=_SRC, asof=asof, updated_at=updated,
    ))
    trend_bits.append(alignment_score)

    # ---------- ADX 趨勢強度 ----------
    try:
        plus_di, minus_di, adx_s = _adx(df["high"], df["low"], df["close"])
        adx_val = float(adx_s.iloc[-1]) if pd.notna(adx_s.iloc[-1]) else None
        pdi = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else None
        mdi = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else None
    except Exception:
        adx_val = pdi = mdi = None
    if adx_val is not None:
        direction = "多方" if (pdi is not None and mdi is not None and pdi > mdi) else "空方"
        strength = "強趨勢" if adx_val >= 25 else ("弱趨勢/盤整" if adx_val < 20 else "趨勢轉強中")
        metrics.append(metric(
            "adx", f"ADX 趨勢強度({direction}{strength})", round(adx_val, 1),
            rating=("good" if (adx_val >= 25 and direction == "多方") else
                    ("bad" if (adx_val >= 25 and direction == "空方") else "neutral")),
            formula="ADX(14) + DI/-DI 判方向;ADX≥25 視為有方向性的強趨勢,<20 視為盤整",
            source=_SRC, asof=asof, updated_at=updated,
        ))
        adx_sub = clip01(adx_val / 50) if direction == "多方" else clip01(1 - adx_val / 50)
        trend_bits.append(adx_sub)
    else:
        metrics.append(missing_metric("adx", "ADX 趨勢強度", source=_SRC))

    # ---------- RSI ----------
    rsi = float(last["rsi14"]) if "rsi14" in last.index and pd.notna(last["rsi14"]) else None
    if rsi is not None:
        rsi_label = "超買" if rsi >= 70 else ("超賣" if rsi <= 30 else "中性")
        metrics.append(metric(
            "rsi14", f"RSI(14,{rsi_label})", round(rsi, 1),
            rating=("bad" if rsi >= 80 else ("good" if 45 <= rsi <= 70 else "neutral")),
            formula="14日 RSI(Wilder平滑)", source=_SRC, asof=asof, updated_at=updated,
        ))
        momentum_bits.append(clip01(1 - abs(rsi - 60) / 40))
    else:
        metrics.append(missing_metric("rsi14", "RSI(14)", source=_SRC))

    # ---------- MACD ----------
    dif = float(last["dif"]) if "dif" in last.index and pd.notna(last["dif"]) else None
    dea = float(last["dea"]) if "dea" in last.index and pd.notna(last["dea"]) else None
    if dif is not None and dea is not None:
        bullish_macd = dif > dea
        metrics.append(metric(
            "macd", "MACD(DIF vs DEA)", "黃金交叉區" if bullish_macd else "死亡交叉區",
            rating=("good" if bullish_macd else "bad"),
            formula="DIF(12,26) 是否高於 DEA(9日訊號線)", source=_SRC, asof=asof, updated_at=updated,
        ))
        momentum_bits.append(1.0 if bullish_macd else 0.0)
    else:
        metrics.append(missing_metric("macd", "MACD(DIF vs DEA)", source=_SRC))

    # ---------- KD ----------
    k = float(last["k"]) if "k" in last.index and pd.notna(last["k"]) else None
    d = float(last["d"]) if "d" in last.index and pd.notna(last["d"]) else None
    if k is not None and d is not None:
        kd_bullish = k > d
        metrics.append(metric(
            "kd", f"KD(K={k:.0f}, D={d:.0f})", "K>D" if kd_bullish else "K<D",
            rating=("good" if kd_bullish and k < 80 else ("bad" if not kd_bullish and k > 20 else "neutral")),
            formula="KD(9,3,3),比較 K 值與 D 值", source=_SRC, asof=asof, updated_at=updated,
        ))
        momentum_bits.append(1.0 if kd_bullish else 0.0)
    else:
        metrics.append(missing_metric("kd", "KD", source=_SRC))

    # ---------- 量能 ----------
    vol_ratio = float(last["vol_ratio"]) if "vol_ratio" in last.index and pd.notna(last["vol_ratio"]) else None
    vol_ma5 = float(last["vol_ma5"]) if "vol_ma5" in last.index and pd.notna(last["vol_ma5"]) else None
    vol_ma20 = float(last["vol_ma20"]) if "vol_ma20" in last.index and pd.notna(last["vol_ma20"]) else None
    if vol_ma5 is not None and vol_ma20 is not None and vol_ma20 > 0:
        vbias = vol_ma5 / vol_ma20
        metrics.append(metric(
            "volume_structure", "量能結構(5日均量/20日均量)", round(vbias, 2),
            rating=("good" if vbias >= 1.1 else ("bad" if vbias <= 0.85 else "neutral")),
            formula="5日均量 ÷ 20日均量;>1 代表近期量能轉強", source=_SRC, asof=asof, updated_at=updated,
        ))
        volume_bits.append(clip01((vbias - 0.7) / (1.5 - 0.7)))
    else:
        metrics.append(missing_metric("volume_structure", "量能結構(5日均量/20日均量)", source=_SRC))

    # ---------- 波動度 ----------
    atr14 = float(last["atr14"]) if "atr14" in last.index and pd.notna(last["atr14"]) else None
    if atr14 is not None and close:
        atr_pct = atr14 / close * 100
        metrics.append(metric(
            "atr_pct", "ATR 波動度(占股價%)", round(atr_pct, 2), unit="%",
            formula="14日 ATR ÷ 收盤價 × 100;數值越高代表波動越大(無good/bad,供短線評估用)",
            source=_SRC, asof=asof, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("atr_pct", "ATR 波動度(占股價%)", source=_SRC))

    # ---------- 支撐 / 壓力(複用既有 reference_levels(),不重新發明)----------
    levels = reference_levels(df)
    support_candidates = [v for k_, v in levels.items()
                          if k_ in ("ma60", "ma120", "low_60", "bb_lower") and v is not None and v < close]
    resistance_candidates = [v for k_, v in levels.items()
                             if k_ in ("high_60", "high_20", "bb_upper") and v is not None and v > close]
    support = max(support_candidates) if support_candidates else None
    resistance = min(resistance_candidates) if resistance_candidates else None
    if support is not None:
        metrics.append(metric(
            "support", "最近支撐", round(support, 1), unit="元",
            formula="近期均線(60/120MA)、近60日低點、布林下緣中,最接近現價下方者",
            source=_SRC, asof=asof, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("support", "最近支撐", source=_SRC, reason="not_applicable"))
    if resistance is not None:
        metrics.append(metric(
            "resistance", "最近壓力", round(resistance, 1), unit="元",
            formula="近60/20日高點、布林上緣中,最接近現價上方者",
            source=_SRC, asof=asof, updated_at=updated,
        ))
    else:
        metrics.append(missing_metric("resistance", "最近壓力", source=_SRC, reason="not_applicable"))

    # ---------- 突破 / 跌破現況(非當日新鮮觸發,是現況)----------
    high_60 = levels.get("high_60"); low_60 = levels.get("low_60")
    if high_60 is not None and low_60 is not None:
        if close >= high_60 * 0.995:
            stance = "站上近60日高點區"
        elif close <= low_60 * 1.005:
            stance = "跌破近60日低點區"
        else:
            stance = "區間整理"
        metrics.append(metric(
            "breakout_status", "目前是否站上/跌破關鍵區間", stance,
            rating=("good" if stance.startswith("站上") else ("bad" if stance.startswith("跌破") else "neutral")),
            formula="收盤價對比近60日高/低點(非當日新鮮突破訊號,是現況位階)",
            source=_SRC, asof=asof, updated_at=updated,
        ))
        trend_bits.append(1.0 if stance.startswith("站上") else (0.0 if stance.startswith("跌破") else 0.5))

    sub_scores = [s for s in (avg_score(trend_bits), avg_score(momentum_bits), avg_score(volume_bits)) if s is not None]
    score = (sum(sub_scores) / len(sub_scores) * 100) if sub_scores else None
    return engine_result(score, metrics)
