"""當沖通知用的 5 分K 縮圖(2026-09-09)。

## 為什麼是 5 分K 而不是日K

既有的 `scripts/alert_chart.daily_k_png` 畫的是日K —— 那是給波段訊號看
「在更大的圖上長什麼樣」用的。**當沖不一樣**:進出都在同一天內,日K 上
今天只是一根棒子,看不出開盤區間、盤中支撐、現在是在高檔還低檔。
使用者 2026-09-09 直接指定「盡量用五分鐘K」。

## 資料來源

`fetchers.fetch_intraday_1m`(yfinance 當日 1 分K)再重採樣成 5 分K。
沒有另外的 5 分K 端點,而 1 分K 本來就是 ORB 在用的,不需要新資料源。
⚠️ 這支**會打網路**(與 alert_chart 的「零 API」不同),所以只給實際要推播的
那幾檔畫,不要對整個標的池呼叫。

## 圖上畫什麼

進場 / 目標 / 停損三條水平線 —— 這是當沖卡片的核心,圖的功能是讓你一眼看出
「現在離這三條線多遠」。再加開盤區間的上下緣(當沖最常用的參考位)。
不畫任何副圖:縮圖塞 KD/MACD 會全糊掉,要細節該點進網頁。

## 失敗一律回 None

matplotlib 沒裝、yfinance 抓不到、盤前沒有分K —— 通知裡少一張圖而已,
不該影響訊號本身。
"""
from __future__ import annotations

import io

from ..utils import log

MAX_BARS = 60           # 5 分K 60 根 = 5 小時,足以涵蓋整個交易時段
_OPEN_RANGE_MIN = 15

# matplotlib 預設的 DejaVu Sans **沒有中文字形**,直接寫中文會整排變成豆腐方塊。
# 既有的 alert_chart.py 是靠「標題只用英數」繞過去的;這裡改成先找系統有沒有
# CJK 字型,找不到才退回英文標籤 —— 本機(Windows 有微軟正黑)會是中文,
# GitHub Actions(Ubuntu 通常沒裝 Noto CJK)會自動變英文,兩邊都不會出現豆腐。
_CJK_CANDIDATES = ("Microsoft JhengHei", "Microsoft YaHei", "PingFang TC",
                   "Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans TC",
                   "Source Han Sans TC", "SimHei", "Arial Unicode MS")
_FONT_CACHE: dict = {}


def _pick_cjk_font() -> str | None:
    """回一個系統真的有的 CJK 字型名稱;找不到回 None。結果快取,不重複掃字型庫。"""
    if "name" in _FONT_CACHE:
        return _FONT_CACHE["name"]
    name = None
    try:
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
        name = next((c for c in _CJK_CANDIDATES if c in have), None)
    except Exception:
        name = None
    _FONT_CACHE["name"] = name
    return name


def _labels(cjk: bool) -> dict:
    """圖上的文字。沒有 CJK 字型就全部改英文 —— 寧可英文也不要豆腐方塊。"""
    if cjk:
        return {"entry": "進場", "target": "目標", "stop": "停損",
                "orh": " 開盤區間高", "orl": " 開盤區間低",
                "long": "做多", "short": "做空", "px": "現價", "vs": "vs 開盤"}
    return {"entry": "Entry", "target": "Target", "stop": "Stop",
            "orh": " OR High", "orl": " OR Low",
            "long": "LONG", "short": "SHORT", "px": "Last", "vs": "vs open"}


def five_min_k_png(stock_id: str, name: str = "", market: str = "twse", *,
                   entry: float | None = None, target: float | None = None,
                   stop: float | None = None, side: str = "long") -> bytes | None:
    """當日 5 分K + 進場/目標/停損三條線。回傳 PNG bytes;任何問題回 None。"""
    try:
        import matplotlib
        matplotlib.use("Agg")            # 無視窗環境(GitHub Actions)必須
        import matplotlib.pyplot as plt
        import pandas as pd
        from ..fetchers import fetch_intraday_1m
    except Exception as e:
        log.info(f"5分K圖略過(matplotlib/相依未安裝?):{e}")
        return None

    try:
        cjk = _pick_cjk_font()
        if cjk:
            matplotlib.rcParams["font.sans-serif"] = [cjk] + list(
                matplotlib.rcParams.get("font.sans-serif", []))
            matplotlib.rcParams["axes.unicode_minus"] = False
        LB = _labels(bool(cjk))

        m1 = fetch_intraday_1m(str(stock_id), market or "twse")
        if m1 is None or m1.empty or len(m1) < 5:
            return None
        df = (m1.resample("5min")
                .agg({"open": "first", "high": "max", "low": "min",
                      "close": "last", "volume": "sum"})
                .dropna(subset=["close"]))
        if df.empty:
            return None
        df = df.tail(MAX_BARS)

        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(7.2, 4.0), dpi=110, sharex=True,
            gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.05})
        fig.patch.set_facecolor("#15171c")       # Discord 深色主題,白底會刺眼
        for a in (ax, axv):
            a.set_facecolor("#15171c")
            a.tick_params(colors="#8b93a7", labelsize=7.5)
            for sp in a.spines.values():
                sp.set_color("#2a2e39")
            a.grid(True, color="#22262f", linewidth=0.6)

        up, dn = "#22c55e", "#ef4444"
        x = range(len(df))
        for i, (_, r) in enumerate(df.iterrows()):
            c = up if r["close"] >= r["open"] else dn
            ax.vlines(i, r["low"], r["high"], color=c, linewidth=0.9)
            lo, hi = sorted((r["open"], r["close"]))
            ax.add_patch(plt.Rectangle((i - 0.32, lo), 0.64, max(hi - lo, 1e-9),
                                       facecolor=c, edgecolor=c, linewidth=0.6))
            axv.bar(i, r["volume"], color=c, width=0.64, alpha=0.75)

        # 開盤區間(當沖最常用的參考位)
        orb_bars = max(1, _OPEN_RANGE_MIN // 5)
        if len(df) >= orb_bars:
            orh = float(df["high"].iloc[:orb_bars].max())
            orl = float(df["low"].iloc[:orb_bars].min())
            ax.axhline(orh, color="#6366f1", linewidth=0.8, linestyle=":", alpha=0.9)
            ax.axhline(orl, color="#6366f1", linewidth=0.8, linestyle=":", alpha=0.9)
            ax.text(len(df) - 0.5, orh, LB["orh"], color="#8b93a7", fontsize=6.5, va="bottom")
            ax.text(len(df) - 0.5, orl, LB["orl"], color="#8b93a7", fontsize=6.5, va="top")

        # 進場 / 目標 / 停損 —— 圖的重點就是讓你看出離這三條線多遠
        for val, color, label in ((entry, "#e5e7eb", LB["entry"]),
                                  (target, up, LB["target"]),
                                  (stop, dn, LB["stop"])):
            if val:
                ax.axhline(float(val), color=color, linewidth=1.1, alpha=0.95)
                ax.text(0, float(val), f" {label} {val:g}", color=color,
                        fontsize=7.5, va="bottom", fontweight="bold")

        last = float(df["close"].iloc[-1])
        first = float(df["open"].iloc[0])
        chg = (last / first - 1) * 100 if first else 0
        dir_txt = LB["long"] if side == "long" else LB["short"]
        # 沒有 CJK 字型時連股名都不能放(公司名一定是中文)—— 只留代號。
        head = f"{name}({stock_id})" if cjk else str(stock_id)
        ax.set_title(f"{head} 5min | {dir_txt} | {LB['px']} {last:g} "
                     f"({chg:+.2f}% {LB['vs']})",
                     color="#e5e7eb", fontsize=9.5, pad=7)
        axv.set_yticks([])
        step = max(1, len(df) // 6)
        axv.set_xticks(list(x)[::step])
        axv.set_xticklabels([t.strftime("%H:%M") for t in df.index][::step])

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(),
                    bbox_inches="tight", pad_inches=0.15)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"5分K圖產生失敗 {stock_id}(不影響通知):{e}")
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        return None
