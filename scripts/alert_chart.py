"""盤中訊號通知裡的日K縮圖(2026-07-21)。

**資料全部來自本機 `data/prices/*.parquet`,零 API 呼叫。** 那些檔案本來就在 repo 裡
(1980 檔、每檔上千根日K),盤後批次每天增量更新,所以畫圖不需要再抓任何東西。

## 為什麼是「日K」而不是分K

使用者要的是「這訊號在更大的圖上長什麼樣」—— 分K只看得到今天,看不出
是不是打底完突破、還是追在半山腰。日K 60 根剛好涵蓋一季。
今天這一根用即時價現補上去(parquet 裡最新只到昨天),否則圖上會看不到觸發的那一根。

## 設計上的取捨

* **深色底**:Discord 預設深色主題,白底圖貼進去會刺眼。
* **標出 20 日高那條線**:突破訊號的判定基準就是它,圖上看得到才能自己驗證。
* **量能用顏色分紅綠**:配合訊號的「量比」欄位,一眼看出是不是帶量。
* **不畫 KD/MACD**:一張 40mm 高的縮圖塞副圖會全糊掉,要細節本來就該點進網頁。

## 失敗一律回 None

matplotlib 沒裝、parquet 缺檔、資料太短 —— 通知裡少一張圖而已,不該影響訊號本身。
"""
from __future__ import annotations

import io

from .utils import log

BARS = 60                 # 顯示幾根日K(約一季)
_UP = "#ef4444"           # 台股習慣:紅漲綠跌(與歐美相反,別改)
_DOWN = "#22c55e"
_BG = "#2b2d31"           # Discord 深色主題的卡片底色
_FG = "#dbdee1"
_GRID = "#3f4147"


def daily_k_png(stock_id: str, name: str = "", live_price: float | None = None,
                high20: float | None = None) -> bytes | None:
    """回傳 PNG bytes;任何問題回 None。"""
    try:
        import matplotlib
        matplotlib.use("Agg")                    # 無視窗環境(GitHub Actions)必須
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
        import pandas as pd
        from .storage import price_path
    except Exception as e:
        log.info(f"日K圖略過(matplotlib 未安裝?):{e}")
        return None

    try:
        p = price_path(str(stock_id))
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        if df is None or df.empty or len(df) < 20:
            return None
        df = df.dropna(subset=["close"]).tail(BARS + 60).copy()

        # parquet 最新只到昨天(盤後批次才寫)。把今天這根用即時價補上,
        # 否則圖上根本看不到剛剛觸發的那一根 —— 那是使用者最想看的。
        if live_price:
            try:
                today = pd.Timestamp(__import__("datetime").date.today())
                if len(df) and pd.Timestamp(df.index[-1]).date() != today.date():
                    prev = float(df["close"].iloc[-1])
                    df.loc[today] = {"open": prev, "high": max(prev, live_price),
                                     "low": min(prev, live_price), "close": live_price,
                                     "volume": float("nan"),
                                     "adj_close": live_price}
            except Exception:
                pass

        for w in (5, 20, 60):
            df[f"ma{w}"] = df["close"].rolling(w).mean()
        d = df.tail(BARS)
        if len(d) < 10:
            return None

        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(7.2, 4.0), dpi=110, sharex=True,
            gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.06})
        fig.patch.set_facecolor(_BG)

        x = range(len(d))
        o, h, l, c = (d["open"].values, d["high"].values,
                      d["low"].values, d["close"].values)
        for i in x:
            col = _UP if c[i] >= o[i] else _DOWN
            ax.vlines(i, l[i], h[i], color=col, linewidth=0.8)
            ax.add_patch(plt.Rectangle(
                (i - 0.3, min(o[i], c[i])), 0.6, max(abs(c[i] - o[i]), 1e-9),
                facecolor=col, edgecolor=col, linewidth=0.5))

        for w, col, lw in ((5, "#fbbf24", 1.0), (20, "#60a5fa", 1.0), (60, "#a78bfa", 1.0)):
            s = d[f"ma{w}"]
            if s.notna().sum() > 2:
                ax.plot(x, s.values, color=col, linewidth=lw, label=f"MA{w}")

        # 突破訊號的判定基準線 —— 圖上看得到才能自己驗證那一刀切在哪
        if high20:
            ax.axhline(high20, color="#f472b6", linewidth=0.9, linestyle="--", alpha=0.9)
            # ⚠️ 標籤只用英數 —— CI 的 DejaVu Sans 沒有中文字,寫中文會變一排豆腐方塊
            ax.text(len(d) - 1, high20, f"20D High {high20:.2f} ", color="#f472b6",
                    fontsize=7, va="bottom", ha="right")

        vols = d["volume"].fillna(0).values
        axv.bar(x, vols, color=[_UP if c[i] >= o[i] else _DOWN for i in x],
                width=0.6, alpha=0.85)
        axv.yaxis.set_major_formatter(FuncFormatter(
            lambda v, _: f"{v/1e6:.0f}M" if v >= 1e6 else f"{v/1e3:.0f}K"))

        for a in (ax, axv):
            a.set_facecolor(_BG)
            a.grid(True, color=_GRID, linewidth=0.5, alpha=0.6)
            a.tick_params(colors=_FG, labelsize=7)
            for sp in a.spines.values():
                sp.set_color(_GRID)
        # 中文字型在 CI 上多半沒有,標題只用代號 + 英數,避免出現一排豆腐方塊
        ax.set_title(f"{stock_id}   daily {BARS}D", color=_FG, fontsize=9, loc="left")
        leg = ax.legend(loc="upper left", fontsize=7, facecolor=_BG,
                        edgecolor=_GRID, labelcolor=_FG, framealpha=0.8)
        if leg:
            leg.get_frame().set_linewidth(0.5)

        step = max(1, len(d) // 6)
        axv.set_xticks(list(x)[::step])
        axv.set_xticklabels([str(t)[:10][5:] for t in d.index[::step]],
                            color=_FG, fontsize=7)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight", pad_inches=0.15)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"日K圖 {stock_id} 產生失敗(不影響通知):{e}")
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        return None
