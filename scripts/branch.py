"""券商分點(買賣日報表)分析 —— 個股詳情頁的「分點 / 短沖主力」面板資料源。

資料:FinMind `TaiwanStockTradingDailyReport`(Sponsor 級,**逐檔**、當晚 21:00 發布、歷史自 2021-06-30)。
顆粒度是「分公司 × 成交價」→ 必須先 group by securities_trader_id 把 buy/sell 加總,才是該分點當日買賣量。

短沖主力判定採「**領域名單 + 資料驅動**」雙軌,不單靠寫死名單:
  1. KNOWN_SHORT_TERM:公開常被點名的隔日沖/短沖主力分點(僅當先驗,不當定論)。
  2. 資料驅動 reversal:對這檔股票,該分點「大買後隔天轉賣」的比率 —— 這才是隔日沖的**行為定義**,
     且會隨資料自我更新(某分點在這檔是隔日沖、在別檔未必是)。
兩者任一成立即標記,並在前端標示依據,讓使用者知道是「名單」還是「這檔的實際行為」。

⚠️ 誠實邊界:這裡輸出的是**風險等級與依據**,不是勝率保證。隔日沖主力大買 → 隔天賣壓機率高,
但不保證下跌(主力也可能續拉)。所有門檻都標在 output 裡供使用者自行判斷。
"""
from __future__ import annotations
from datetime import date, timedelta
import pandas as pd

from .fetchers import fetch_finmind
from .utils import log

# 公開資料中最常被點名的隔日沖/短沖主力分點(securities_trader_id → 顯示名)。
# 來源:券商分點社群/財經媒體整理 + 本專案實測分點資料中確認存在的代號。
# 僅作「先驗提示」,真正判定仍看該股實際的 reversal 行為(見 _reversal_score)。
KNOWN_SHORT_TERM: dict[str, str] = {
    "1440": "美林",
    "8440": "摩根大通",
    "1470": "台灣摩根士丹利",
    "1480": "美商高盛",
    "1590": "花旗環球",
    "9268": "凱基-台北",
    "9275": "凱基-三多",
    "9658": "富邦-建國",
    "9A00": "永豐金",
    "1650": "日盛",
    "8880": "國泰",
}


def _fetch_one_day(stock_id: str, d: str) -> list[dict]:
    return fetch_finmind("TaiwanStockTradingDailyReport", data_id=stock_id,
                         start_date=d, end_date=d) or []


def fetch_branch_daily(stock_id: str, dates: list[str], max_workers: int = 8) -> pd.DataFrame:
    """抓指定交易日清單的分點買賣。回傳 long 表:date, trader_id, trader, buy, sell(股)。

    ⚠️ 必須**逐日**抓:單日就有 ~16000 列(分公司×成交價),給日期區間會被 FinMind 以
    400(size too large)拒絕。故用 ThreadPool 平行逐日抓,讓 serverless 仍在可接受秒數內。
    任何失敗回空 DataFrame(呼叫端該面板不畫,絕不讓整頁掛掉)。"""
    if not dates:
        return pd.DataFrame()
    from concurrent.futures import ThreadPoolExecutor
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(dates))) as ex:
        for part in ex.map(lambda d: _fetch_one_day(stock_id, d), dates):
            rows.extend(part)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    need = {"securities_trader_id", "buy", "sell", "date"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    for c in ("buy", "sell"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    # 同一分點同日會依成交價拆成多列 → 先加總回「該分點當日買/賣」
    g = (df.groupby(["date", "securities_trader_id", "securities_trader"], as_index=False)
           [["buy", "sell"]].sum())
    g = g.rename(columns={"securities_trader_id": "trader_id", "securities_trader": "trader"})
    g["net"] = g["buy"] - g["sell"]
    g["churn"] = g[["buy", "sell"]].min(axis=1)     # 同日雙邊 = 當沖行為量
    return g.sort_values("date")


def breadth_ratio_from_rows(rows: list[dict]) -> float | None:
    """單一 (股票, 日) 的分點買賣「廣度比」= (買超家數 − 賣超家數) / 總家數,範圍 −1~+1。

    為什麼用比例而非絕對家數:驗證時用的絕對 net_breadth 隨個股熱度/分點總數浮動很大,
    跨個股無法比較;除以總家數後才是可跨股比較的「擁擠度」。

    語意(已驗證,見 branch_validation):**比例越高 = 越多分點在買 = 越擁擠 = 後續越差**
    (逆向訊號)。台股是反轉市場,連八大行庫買超都要 fade(validate_govbank 亦同向)。
    """
    if not rows:
        return None
    agg: dict[str, list[float]] = {}
    for r in rows:
        tid = r.get("securities_trader_id")
        a = agg.setdefault(tid, [0.0, 0.0])
        a[0] += float(r.get("buy") or 0)
        a[1] += float(r.get("sell") or 0)
    if not agg:
        return None
    nets = [b - s for b, s in agg.values()]
    n_buy = sum(1 for x in nets if x > 0)
    n_sell = sum(1 for x in nets if x < 0)
    tot = n_buy + n_sell
    if tot == 0:
        return None
    return (n_buy - n_sell) / tot


def fetch_breadth_for(stock_ids: list[str], d: str, max_workers: int = 6) -> dict[str, float]:
    """對一批候選股抓「當日」分點並算廣度比。回傳 {stock_id: ratio}(抓不到的不放進 dict)。

    ⚠️ 分點當晚 21:00 才發布 —— 批次若在 21:00 前跑,這裡拿到的是**昨天**的分點。
    逐檔各 1 次呼叫(候選約 30 檔 → 30 次),平行後數秒。失敗個股略過,不影響其他。
    """
    if not stock_ids:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    out: dict[str, float] = {}

    def one(sid: str):
        try:
            return sid, breadth_ratio_from_rows(_fetch_one_day(sid, d))
        except Exception:
            return sid, None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(stock_ids))) as ex:
        for sid, r in ex.map(one, stock_ids):
            if r is not None:
                out[sid] = r
    return out


def branch_signal(breadth: float | None, risk_on: bool | None, cfg: dict | None = None) -> float | None:
    """把廣度比轉成 stage-2 加成訊號(−1~+1),直接編碼已驗證的多空 regime 差異。

    驗證結論([[twse-branch-factor-validated]]):
      · **Q4「一窩蜂買」= 最差,且強盤弱盤都成立** → 擁擠買**一律扣分**(regime-robust 的半邊)。
      · **Q1「低廣度/分點在賣」= 最好,但只在強盤成立**;弱盤時 Q1 執行淨 −1.66% 反而最差
        → 低廣度加分**只在 risk-on 時給**,risk-off/未知時把正訊號夾成 0(只留扣分)。
    """
    if breadth is None:
        return None
    cfg = cfg or {}
    sig = -float(breadth)                     # 廣度越高(越擁擠)→ 訊號越負
    if sig > 0 and not risk_on:               # 逆向「加分」那半邊只在順風時採用
        sig = 0.0
    lo = float(cfg.get("clamp_lo", -1.0)); hi = float(cfg.get("clamp_hi", 1.0))
    return max(lo, min(hi, sig))


def _reversal_score(sub: pd.DataFrame) -> float | None:
    """該分點在這檔股票的「今買明賣」程度:取淨額序列的 lag-1 相關係數的負值。
    越接近 +1 = 買完隔天就倒(典型隔日沖);≈0 = 無此規律;<0 = 買了會續買(波段)。
    序列太短(<5 天有交易)回 None(不臆測)。"""
    s = sub.sort_values("date")["net"].reset_index(drop=True)
    # 需要夠長的序列:lag-1 自相關在 <8 點時變異極大,會把雜訊誤判成隔日沖。
    if len(s) < 8 or s.std() == 0:
        return None
    try:
        c = s.autocorr(lag=1)
    except Exception:
        return None
    if c is None or pd.isna(c):
        return None
    return float(-c)          # 負自相關 = 反轉 = 隔日沖


def analyze_branch(df: pd.DataFrame, volume_by_date: dict[str, float] | None = None,
                   top_n: int = 10, recent_days: int = 5) -> dict:
    """把 long 表轉成前端要的:每日主力進出、分點排行、短沖主力標記與隔日賣壓判讀。

    volume_by_date: {YYYY-MM-DD: 當日成交股數} —— 用來把「主力買超」換算成佔成交量%,
    這是社群判定「隔天賣壓」的關鍵門檻(買超達當日量 ~20% 即需警戒)。缺就不算比例。
    """
    if df is None or df.empty:
        return {}
    dates = sorted(df["date"].unique())
    recent = dates[-recent_days:] if len(dates) >= recent_days else dates
    last_day = dates[-1]

    # 有意義的量門檻:分點近期毛量需達全體毛量的 min_share,才納入「資料驅動」判定,
    # 否則 817 家裡的小分點雜訊會被自相關誤判成隔日沖(實測未設限時 128/817 被標記,過寬)。
    gross_all = float((df["buy"] + df["sell"]).sum()) or 1.0
    min_gross = gross_all * 0.005          # 0.5% 全體毛量

    # ---- 每家分點:近期淨額、當沖量、反轉分數、是否短沖主力 ----
    traders = []
    for (tid, tname), sub in df.groupby(["trader_id", "trader"]):
        rec = sub[sub["date"].isin(recent)]
        rv = _reversal_score(sub)
        known = tid in KNOWN_SHORT_TERM
        gross = float((sub["buy"] + sub["sell"]).sum())
        # 資料驅動門檻:反轉分數 > 0.35(負自相關夠強)且該分點在這檔有足夠份量
        data_short = (rv is not None and rv > 0.35 and gross >= min_gross)
        traders.append({
            "trader_id": tid, "trader": tname,
            "net_recent": float(rec["net"].sum()),
            "net_last": float(sub[sub["date"] == last_day]["net"].sum()),
            "churn_recent": float(rec["churn"].sum()),
            "gross": gross,
            "reversal": round(rv, 3) if rv is not None else None,
            "is_short_term": bool(known or data_short),
            "why_short": ("名單+行為" if (known and data_short) else
                          "名單" if known else "行為" if data_short else ""),
        })
    traders.sort(key=lambda x: x["net_recent"], reverse=True)
    top_buy = traders[:top_n]
    top_sell = sorted(traders, key=lambda x: x["net_recent"])[:top_n]

    # ---- 短沖主力整體動向 ----
    st_ids = {t["trader_id"] for t in traders if t["is_short_term"]}
    st_df = df[df["trader_id"].isin(st_ids)]
    by_date = []
    for d in dates:
        day = st_df[st_df["date"] == d]
        net = float(day["net"].sum())
        vol = (volume_by_date or {}).get(d)
        by_date.append({
            "date": d,
            "st_net": net,
            "st_net_pct_vol": round(net / vol * 100, 2) if vol else None,
        })

    st_net_last = by_date[-1]["st_net"] if by_date else 0.0
    st_pct_last = by_date[-1]["st_net_pct_vol"] if by_date else None
    st_net_recent = float(st_df[st_df["date"].isin(recent)]["net"].sum())

    alert = _next_day_alert(st_net_last, st_pct_last, st_net_recent)

    return {
        "last_date": last_day,
        "top_buy": top_buy, "top_sell": top_sell,
        "short_term_ids": sorted(st_ids),
        "st_by_date": by_date,
        "st_net_last": st_net_last, "st_net_pct_vol_last": st_pct_last,
        "st_net_recent": st_net_recent,
        "alert": alert,
        "recent_days": len(recent),
    }


def _next_day_alert(st_net_last: float, st_pct_last: float | None, st_net_recent: float) -> dict:
    """隔日賣壓 / 拉抬 判讀 —— 輸出等級 + 白話依據,**不輸出假精準機率**。

    依據(公開的分點判讀慣例):短沖主力當日買超佔成交量越高,隔天獲利了結賣壓越大;
    若主力已在倒貨(淨賣),則賣壓多半已釋放。門檻寫在文字裡讓使用者自行斟酌。
    """
    pct = st_pct_last
    if st_net_last > 0:
        if pct is not None and pct >= 20:
            return {"level": "high", "dir": "dump",
                    "text": f"短沖主力今日大買,淨買超佔成交量 {pct:.1f}%(≥20% 為高度警戒)。"
                            f"隔日獲利了結賣壓明顯偏高,追高風險大。"}
        if pct is not None and pct >= 8:
            return {"level": "medium", "dir": "dump",
                    "text": f"短沖主力今日買超佔成交量 {pct:.1f}%(8~20% 中度)。隔日可能有調節賣壓,留意開高走低。"}
        return {"level": "low", "dir": "dump",
                "text": "短沖主力今日小幅買超,隔日賣壓壓力有限,但仍留意其動向。"}
    if st_net_last < 0:
        if st_net_recent < 0:
            return {"level": "low", "dir": "released",
                    "text": "短沖主力今日與近期皆為淨賣,短線倒貨壓力多已釋放;若股價未破線,籌碼反而轉乾淨。"}
        return {"level": "medium", "dir": "released",
                "text": "短沖主力今日轉為淨賣(近期仍累積買超),可能正在出貨,留意賣壓延續。"}
    return {"level": "none", "dir": "none", "text": "今日無明顯短沖主力進出。"}
