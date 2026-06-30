"""籌碼分析(Chip)Engine — 個股健檢面向六。

資料來自 ctx['chips'](fetchers.fetch_chips_history:三大法人/融資融券/外資持股)與
ctx['holder_dist'](fetchers.fetch_holder_distribution,2026-06-30 新增,大戶持股/股東人數,
欄位名稱未經本機實測驗證,缺資料時對應指標誠實標 missing)。

法人連買邏輯複用既有 main.py/scoring.py 的 stage-2 chip_bonus 概念,但獨立成這裡的
Chip Score,不直接搬信心分數字(健檢給長期投資人也要看籌碼結構,不只是短線連買天數)。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from .metric import metric, missing_metric, engine_result, status_from_delta, avg_score, clip01

_SRC_INST = "FinMind:TaiwanStockInstitutionalInvestorsBuySell"
_SRC_MARGIN = "FinMind:TaiwanStockMarginPurchaseShortSale"
_SRC_HOLD = "FinMind:TaiwanStockHoldingSharesPer(欄位未實測驗證)"


def _consecutive_positive(series: pd.Series, max_check: int = 20) -> int:
    s = series.dropna().tail(max_check)
    count = 0
    for v in reversed(s.tolist()):
        if v > 0:
            count += 1
        else:
            break
    return count


def compute(ctx: dict) -> dict:
    chips = ctx.get("chips")
    holder = ctx.get("holder_dist")
    updated = ctx.get("updated_at", "")

    metrics: list[dict] = []
    inst_bits: list[float] = []
    margin_bits: list[float] = []
    concentration_bits: list[float] = []

    if chips is not None and not chips.empty:
        asof = str(chips.index[-1].date())
        if "inst_total" in chips.columns:
            s = chips["inst_total"].dropna()
            streak = _consecutive_positive(s)
            today = float(s.iloc[-1]) if not s.empty else None
            net5 = float(s.tail(5).sum()) if len(s) >= 1 else None
            metrics.append(metric(
                "inst_buy_streak", "三大法人連買天數", streak, unit="天",
                rating=("good" if streak >= 3 else ("bad" if streak == 0 and today is not None and today < 0 else "neutral")),
                formula="從最新交易日往回數,三大法人合計淨買超連續為正的天數",
                source=_SRC_INST, asof=asof, updated_at=updated,
            ))
            inst_bits.append(clip01(streak / 5))
            if today is not None:
                metrics.append(metric(
                    "inst_net_today", "三大法人今日淨買超", round(today / 1000, 0), unit="張",
                    rating=("good" if today > 0 else "bad"),
                    formula="今日三大法人(外資+投信+自營商)合計買超股數 ÷ 1000",
                    source=_SRC_INST, asof=asof, updated_at=updated,
                ))
            if net5 is not None:
                metrics.append(metric(
                    "inst_net_5d", "三大法人近5日淨買超", round(net5 / 1000, 0), unit="張",
                    rating=("good" if net5 > 0 else "bad"),
                    formula="近5個交易日三大法人合計淨買超股數加總 ÷ 1000",
                    source=_SRC_INST, asof=asof, updated_at=updated,
                ))
                inst_bits.append(clip01((net5 / 1000 + 500) / 1000))
        else:
            metrics.append(missing_metric("inst_buy_streak", "三大法人連買天數", source=_SRC_INST))

        if "inst_foreign" in chips.columns:
            fs = chips["inst_foreign"].dropna()
            if not fs.empty:
                f_today = float(fs.iloc[-1])
                metrics.append(metric(
                    "foreign_net_today", "外資今日買賣超", round(f_today / 1000, 0), unit="張",
                    rating=("good" if f_today > 0 else "bad"),
                    formula="今日外資買超股數 ÷ 1000(正=買超)",
                    source=_SRC_INST, asof=asof, updated_at=updated,
                ))

        if "foreign_holding_pct" in chips.columns:
            fh = chips["foreign_holding_pct"].dropna()
            if not fh.empty:
                latest = float(fh.iloc[-1])
                prev = float(fh.iloc[-21]) if len(fh) >= 21 else None
                metrics.append(metric(
                    "foreign_holding_pct", "外資持股比例", round(latest, 2), unit="%",
                    trend=[{"period": str(idx.date()), "value": round(float(v), 2)} for idx, v in fh.tail(20).items()],
                    status=status_from_delta(latest, prev),
                    formula="FinMind 揭露之外資持股比例,status 比較近20個交易日前",
                    source=_SRC_INST, asof=asof, updated_at=updated,
                ))
                concentration_bits.append(clip01(latest / 50))
        else:
            metrics.append(missing_metric("foreign_holding_pct", "外資持股比例", source=_SRC_INST))

        if "margin_balance" in chips.columns:
            mb = chips["margin_balance"].dropna()
            if len(mb) >= 6:
                latest, prev5 = float(mb.iloc[-1]), float(mb.iloc[-6])
                chg = (latest - prev5) / prev5 if prev5 else None
                metrics.append(metric(
                    "margin_balance_chg5d", "融資餘額5日變化", round(chg * 100, 1) if chg is not None else None, unit="%",
                    rating=("bad" if chg is not None and chg > 0.10 else "neutral"),
                    formula="(今日融資餘額 − 5日前) ÷ 5日前;融資快速增加常代表散戶追價(籌碼較不穩定)",
                    source=_SRC_MARGIN, asof=asof, updated_at=updated,
                ))
                margin_bits.append(clip01(1 - max(0, (chg or 0)) / 0.2) if chg is not None else None)
        else:
            metrics.append(missing_metric("margin_balance_chg5d", "融資餘額5日變化", source=_SRC_MARGIN))

        if "short_balance" in chips.columns:
            sb = chips["short_balance"].dropna()
            if len(sb) >= 6:
                latest, prev5 = float(sb.iloc[-1]), float(sb.iloc[-6])
                cover = (prev5 - latest) / prev5 if prev5 else None
                short_covering = bool(cover is not None and cover >= 0.05)
                metrics.append(metric(
                    "short_cover_5d", "融券5日是否回補", "回補中" if short_covering else "無明顯回補", unit="",
                    rating=("good" if short_covering else "neutral"),
                    formula="(5日前融券餘額 − 今日) ÷ 5日前 ≥ 5% 視為回補中",
                    source=_SRC_MARGIN, asof=asof, updated_at=updated,
                ))
                margin_bits.append(1.0 if short_covering else 0.5)
        else:
            metrics.append(missing_metric("short_cover_5d", "融券5日是否回補", source=_SRC_MARGIN))
    else:
        for k, label in (("inst_buy_streak", "三大法人連買天數"), ("foreign_holding_pct", "外資持股比例"),
                         ("margin_balance_chg5d", "融資餘額5日變化"), ("short_cover_5d", "融券5日是否回補")):
            metrics.append(missing_metric(k, label, source=_SRC_INST))

    # ---------- 大戶持股 / 股東人數 ----------
    if holder is not None and not holder.empty:
        h_asof = str(holder.index[-1].date())
        if "big_holder_pct" in holder.columns:
            bh = holder["big_holder_pct"].dropna()
            if not bh.empty:
                latest = float(bh.iloc[-1])
                prev = float(bh.iloc[-2]) if len(bh) >= 2 else None
                metrics.append(metric(
                    "big_holder_pct", "大戶持股比例(>400張)", round(latest, 2), unit="%",
                    trend=[{"period": str(idx.date()), "value": round(float(v), 2)} for idx, v in bh.tail(12).items()],
                    status=status_from_delta(latest, prev),
                    formula="集保庫存股權分散表中,持股級距下界 ≥400張 各級距 percent 加總",
                    source=_SRC_HOLD, asof=h_asof, updated_at=updated,
                ))
                concentration_bits.append(clip01(latest / 70))
        else:
            metrics.append(missing_metric("big_holder_pct", "大戶持股比例(>400張)", source=_SRC_HOLD))
        if "shareholders_total" in holder.columns:
            sh = holder["shareholders_total"].dropna()
            if not sh.empty:
                latest = float(sh.iloc[-1])
                prev = float(sh.iloc[-2]) if len(sh) >= 2 else None
                metrics.append(metric(
                    "shareholders_total", "股東人數", int(latest),
                    status=status_from_delta(latest, prev, higher_is_better=False),
                    formula="集保庫存股權分散表各級距人數加總;股東人數驟增有時代表籌碼趨於分散",
                    source=_SRC_HOLD, asof=h_asof, updated_at=updated,
                ))
        else:
            metrics.append(missing_metric("shareholders_total", "股東人數", source=_SRC_HOLD))
    else:
        metrics.append(missing_metric("big_holder_pct", "大戶持股比例(>400張)", source=_SRC_HOLD))
        metrics.append(missing_metric("shareholders_total", "股東人數", source=_SRC_HOLD))

    # ---------- 流動性(複用 scoring.py 既有公式精神:日均成交額)----------
    df = ctx.get("price_df")
    if df is not None and not df.empty and "vol_ma20" in df.columns:
        last = df.iloc[-1]
        close = float(last["close"]) if pd.notna(last["close"]) else None
        vol_ma20 = float(last["vol_ma20"]) if pd.notna(last.get("vol_ma20")) else None
        if close is not None and vol_ma20 is not None:
            dollar_vol = close * vol_ma20
            metrics.append(metric(
                "dollar_volume", "日均成交金額(20日)", round(dollar_vol / 1e6, 1), unit="百萬元",
                rating=("good" if dollar_vol >= 1e8 else ("bad" if dollar_vol < 3e7 else "neutral")),
                formula="收盤價 × 20日均量,與 scoring.compute_conviction 流動性公式一致",
                source="本機價格資料(yfinance)", asof=str(df.index[-1].date()), updated_at=updated,
            ))
            concentration_bits.append(clip01(np.log10(max(dollar_vol, 1) / 3e7) / np.log10(50)) if dollar_vol > 0 else 0.0)

    inst_bits = [b for b in inst_bits if b is not None]
    margin_bits = [b for b in margin_bits if b is not None]
    concentration_bits = [b for b in concentration_bits if b is not None]
    sub_scores = [s for s in (avg_score(inst_bits), avg_score(margin_bits), avg_score(concentration_bits)) if s is not None]
    score = (sum(sub_scores) / len(sub_scores) * 100) if sub_scores else None
    return engine_result(score, metrics)
