from __future__ import annotations
import logging as _logging
import time
import urllib.parse
from datetime import date, datetime, timedelta
import pandas as pd
import requests
import feedparser
import yfinance as yf

from .config import FINMIND_TOKEN, META_DIR, now_tpe
from .utils import http_get_json, log, chunked, UA

# yfinance prints its own ERROR-level "possibly delisted" lines for every miss.
# We already retry + log via our own helpers, so suppress yfinance's noise.
_logging.getLogger("yfinance").setLevel(_logging.CRITICAL)

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"

# Datasets that returned 4xx during this run; future calls short-circuit.
_DEAD_DATASETS: set[str] = set()


# ---------- FinMind ----------

def fetch_finmind(dataset: str, **params) -> list[dict]:
    """Resilient FinMind GET. Returns [] on any failure (never raises).
    402/404/400 short-circuits future calls to the same dataset for this run.
    """
    if dataset in _DEAD_DATASETS:
        return []
    payload = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
    try:
        j = http_get_json(FINMIND_API, params=payload, retries=2, delay=2.0)
    except Exception as e:
        msg = str(e)
        if any(code in msg for code in ("400", "402", "404")):
            _DEAD_DATASETS.add(dataset)
            log.warning(f"FinMind {dataset} 不可用(免費版受限或名稱失效);本 run 後續跳過。")
        else:
            log.warning(f"FinMind {dataset} fetch error: {e}")
        return []
    if isinstance(j, dict) and "data" in j:
        return j["data"]
    return []


def fetch_stock_info(force: bool = False) -> pd.DataFrame:
    """List of all listed (TWSE) and OTC (TPEX) stocks, with name and industry.
    Cached on disk; refresh once a month.
    """
    cache = META_DIR / "stock_info.parquet"
    if cache.exists() and not force:
        try:
            df = pd.read_parquet(cache)
            mtime = datetime.fromtimestamp(cache.stat().st_mtime)
            if (datetime.now() - mtime).days < 25:
                return df
        except Exception as e:
            log.warning(f"stock_info cache read failed: {e}")

    rows = fetch_finmind("TaiwanStockInfo")
    if not rows:
        if cache.exists():
            log.warning("FinMind stock_info empty; using stale cache")
            return pd.read_parquet(cache)
        raise RuntimeError("Cannot fetch TaiwanStockInfo and no cache available")

    df = pd.DataFrame(rows)
    # FinMind returns columns: industry_category, stock_id, stock_name, type, date
    # type: 'twse' = 上市, 'tpex' = 上櫃
    try:
        df.to_parquet(cache, index=False)
    except Exception as e:
        # 唯讀檔案系統(個股健檢即時查詢路徑跑在 Vercel serverless 會踩到)→ 只記錄,
        # 仍回傳這次抓到的資料,不影響呼叫端。
        log.warning(f"stock_info cache 寫入失敗(唯讀檔案系統?略過快取):{e}")
    return df


def filter_tradable_stocks(df: pd.DataFrame) -> pd.DataFrame:
    """Keep 4-digit numeric codes only.
    Excludes:
    - 00xx: ETFs / 槓反 ETF
    - 91xx: TDR (Taiwan Depository Receipts), yfinance 沒收錄
    """
    out = df.copy()
    out = out[out["stock_id"].str.match(r"^\d{4}$")]
    out = out[~out["stock_id"].str.startswith("00")]
    out = out[~out["stock_id"].str.startswith("91")]
    if "type" in out.columns:
        out = out[out["type"].isin(["twse", "tpex"])]
    out = out.drop_duplicates(subset=["stock_id"]).reset_index(drop=True)
    return out


# ---------- yfinance (price history) ----------

def yf_ticker(stock_id: str, market: str) -> str:
    suffix = ".TW" if market == "twse" else ".TWO"
    return f"{stock_id}{suffix}"


def fetch_price_history(stock_id: str, market: str, days: int = 400) -> pd.DataFrame:
    """Single-stock OHLCV via yfinance. Returns DataFrame indexed by date with
    columns: open, high, low, close, volume. Empty DataFrame on failure.
    """
    ticker = yf_ticker(stock_id, market)
    try:
        df = yf.download(
            ticker,
            period=f"{days}d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as e:
        log.warning(f"yfinance {ticker} failed: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
    })
    # 雙軌價格:保留 adj_close(yfinance 已含除息+分割還原)供 compute_all 算指標去除除息假跳空;
    # close 維持原始成交價供漲停判定 / 停損價 / 顯示。舊快取沒有 adj_close 欄,compute_all 會自動退回用 close。
    keep = [c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in df.columns]
    df = df[keep].copy()
    # yfinance 有時對「剛收盤/未定」的最新一根回傳 NaN 收盤,丟掉(否則均線全毀、評分當掉)
    if "close" in df.columns:
        df = df[df["close"].notna()]
    # yfinance 在台股休市日(颱風假/補假)填回 volume=0、close=前收的假 K 棒,必須排除:
    # 1. _is_trading_day 若看到假K棒最新日 != today 會錯誤跳出; 2. 假K棒進 parquet 會汙染 vol_ratio/均量。
    if "volume" in df.columns:
        df = df[df["volume"] > 0]
    df.index.name = "date"
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def fetch_index_history(days: int = 400, ticker: str = "^TWII") -> pd.DataFrame:
    """大盤加權指數 (TAIEX) OHLCV,用來算個股相對強度。空 DataFrame on failure."""
    try:
        df = yf.download(
            ticker, period=f"{days}d", progress=False,
            auto_adjust=False, threads=False,
        )
    except Exception as e:
        log.warning(f"index {ticker} failed: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
    })
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    if "close" in df.columns:
        df = df[df["close"].notna()]
    if "volume" in df.columns:
        df = df[df["volume"] > 0]
    df.index.name = "date"
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def bulk_fetch_history(stocks: list[tuple[str, str]], days: int = 400, sleep: float = 0.3) -> dict[str, pd.DataFrame]:
    """Fetch history for many (stock_id, market) pairs. Returns {stock_id: df}."""
    out: dict[str, pd.DataFrame] = {}
    for i, (sid, mkt) in enumerate(stocks):
        df = fetch_price_history(sid, mkt, days=days)
        if not df.empty:
            out[sid] = df
        if (i + 1) % 50 == 0:
            log.info(f"history fetched {i+1}/{len(stocks)}")
        time.sleep(sleep)
    return out


# ---------- FinMind chips (daily institutional + margin + foreign holding) ----------

def _fetch_institutional(stock_id: str, start: date, end: date) -> pd.DataFrame:
    rows = fetch_finmind(
        "TaiwanStockInstitutionalInvestorsBuySell",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if not {"buy", "sell", "name"}.issubset(df.columns):
        return pd.DataFrame()
    df["net"] = df["buy"].fillna(0) - df["sell"].fillna(0)

    def bucket(n: str) -> str:
        if not isinstance(n, str):
            return ""
        s = n.lower()
        if "foreign" in s: return "foreign"
        if "investment" in s or "trust" in s: return "invest"
        if "dealer" in s: return "dealer"
        return ""

    df["bucket"] = df["name"].apply(bucket)
    df = df[df["bucket"] != ""]
    if df.empty:
        return pd.DataFrame()
    pivot = df.pivot_table(index="date", columns="bucket", values="net", aggfunc="sum", fill_value=0)
    pivot = pivot.rename(columns={c: f"inst_{c}" for c in pivot.columns})
    for col in ("inst_foreign", "inst_invest", "inst_dealer"):
        if col not in pivot.columns:
            pivot[col] = 0
    pivot["inst_total"] = pivot[["inst_foreign", "inst_invest", "inst_dealer"]].sum(axis=1)
    pivot.index = pd.to_datetime(pivot.index).tz_localize(None).normalize()
    return pivot[["inst_foreign", "inst_invest", "inst_dealer", "inst_total"]].sort_index()


def _fetch_margin(stock_id: str, start: date, end: date) -> pd.DataFrame:
    rows = fetch_finmind(
        "TaiwanStockMarginPurchaseShortSale",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    margin_col = next((c for c in ("MarginPurchaseTodayBalance", "MarginBalance") if c in df.columns), None)
    short_col = next((c for c in ("ShortSaleTodayBalance", "ShortBalance") if c in df.columns), None)
    if not margin_col and not short_col:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    if margin_col:
        out["margin_balance"] = df[margin_col].astype(float)
    if short_col:
        out["short_balance"] = df[short_col].astype(float)
    out = out.set_index("date").sort_index()
    return out


def _fetch_holding(stock_id: str, start: date, end: date) -> pd.DataFrame:
    """Foreign holding ratio. FinMind has renamed/restructured this several
    times; we try a few dataset+column combos, return empty if none works.
    """
    candidate_columns = (
        "ForeignInvestmentSharesRatio", "ForeignInvestmentRemainRatio",
        "ForeignInvestmentRatio", "HoldingSharesPer", "PercentageHeld",
        "ForeignInvestmentSharesPer", "Foreign_Investment_Ratio",
    )
    for ds in ("TaiwanStockShareholding", "TaiwanStockHoldingSharesPer"):
        rows = fetch_finmind(
            ds, data_id=stock_id,
            start_date=start.isoformat(), end_date=end.isoformat(),
        )
        if not rows:
            continue
        df = pd.DataFrame(rows)
        col = next((c for c in candidate_columns if c in df.columns), None)
        if not col:
            continue
        out = pd.DataFrame()
        out["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        out["foreign_holding_pct"] = pd.to_numeric(df[col], errors="coerce")
        out = out.dropna().set_index("date").sort_index()
        if not out.empty:
            return out
    return pd.DataFrame()


def fetch_chips_history(stock_id: str, start: date, end: date) -> pd.DataFrame:
    """Daily chips: institutional + margin + foreign holding. Empty if all sources fail."""
    parts = []
    for fn in (_fetch_institutional, _fetch_margin, _fetch_holding):
        try:
            df = fn(stock_id, start, end)
        except Exception as e:
            log.warning(f"chips fetch {fn.__name__} {stock_id} failed: {e}")
            df = pd.DataFrame()
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = parts[0]
    for p in parts[1:]:
        out = out.join(p, how="outer")
    return out.sort_index()


# Back-compat alias used by older main.py / storage paths
fetch_institutional_history = fetch_chips_history


# ---------- FinMind 集保庫存股權分散表(大戶持股 / 股東人數)— 個股健檢 Chip Engine 用,2026-06-30 ----------

def _parse_level_lower_bound(level: str) -> float | None:
    """'400,001-1,000,000' / '1,000,001以上' 之類的級距字串 → 取下界數字(股數)。
    解析失敗回 None。用『取數字下界』而非比對完整字串,對 FinMind 格式微調較不脆弱。"""
    import re
    if not isinstance(level, str):
        return None
    m = re.search(r"[\d,]+", level)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def fetch_holder_distribution(stock_id: str, start: date, end: date,
                              big_holder_floor_shares: float = 400_000) -> pd.DataFrame:
    """集保庫存股權分散表(TaiwanStockHoldingSharesPer):依持股級距回傳大戶持股比例與股東人數。
    big_holder_floor_shares 預設 40 萬股(=400張,籌碼圈慣用的「大戶」門檻)。

    回傳 DataFrame(index=date):
        big_holder_pct      該日級距下界 >= big_holder_floor_shares 的 percent 加總
        shareholders_total   該日所有級距 people 加總(股東人數)

    FinMind 此資料集欄位名稱未經本機實測驗證(可行性研究見專案文件);任何一步解析失敗
    就回傳空 DataFrame,呼叫端(chip_engine)需把它當「可能缺資料」處理,不可假設必有值。
    """
    rows = fetch_finmind(
        "TaiwanStockHoldingSharesPer", data_id=stock_id,
        start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    level_col = next((c for c in ("HoldingSharesLevel", "holding_shares_level") if c in df.columns), None)
    people_col = next((c for c in ("people", "People", "HoldingSharesPeople") if c in df.columns), None)
    pct_col = next((c for c in ("percent", "Percent", "HoldingSharesPercent") if c in df.columns), None)
    if not level_col or not pct_col:
        return pd.DataFrame()
    df["_lower"] = df[level_col].apply(_parse_level_lower_bound)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df["percent"] = pd.to_numeric(df[pct_col], errors="coerce")
    if people_col:
        df["people"] = pd.to_numeric(df[people_col], errors="coerce")

    # 避免 groupby().apply 在不同 pandas 版本(本專案曾遇 2.x/3.0 混跑)行為不一致,
    # 改用純 groupby().sum() 向量化聚合。
    all_dates = pd.Index(sorted(df["date"].unique()), name="date")
    out = pd.DataFrame(index=all_dates)
    big = df.loc[df["_lower"] >= big_holder_floor_shares].groupby("date")["percent"].sum()
    out["big_holder_pct"] = big.reindex(all_dates).fillna(0.0)
    if people_col:
        out["shareholders_total"] = df.groupby("date")["people"].sum().reindex(all_dates)
    return out.sort_index()


def fetch_holder_distribution_latest(stock_id: str, start: date, end: date) -> dict:
    """集保股權分散表(TaiwanStockHoldingSharesPer)最近一個更新日的原始持股級距,
    供個股詳情頁畫「股權分散圓餅」。集保週更(通常週五),故呼叫端給近一個月窗即可。

    回傳 {"date": "YYYY-MM-DD",
          "levels": [{"lower": 下界股數, "label": 級距字串, "pct": 佔比%, "people": 股東人數|None}, ...]}
    只保留能解析出級距下界的列 → 自動排除『差異數』『合計』彙總列(避免圓餅重複計數)。
    欄位名稱同 fetch_holder_distribution 未經本機實測驗證;任一步失敗回 {}(呼叫端當缺資料)。"""
    rows = fetch_finmind(
        "TaiwanStockHoldingSharesPer", data_id=stock_id,
        start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    level_col = next((c for c in ("HoldingSharesLevel", "holding_shares_level") if c in df.columns), None)
    pct_col = next((c for c in ("percent", "Percent", "HoldingSharesPercent") if c in df.columns), None)
    people_col = next((c for c in ("people", "People", "HoldingSharesPeople") if c in df.columns), None)
    if not level_col or not pct_col:
        return {}
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    df = df.dropna(subset=["date"])
    if df.empty:
        return {}
    latest = df["date"].max()
    sub = df[df["date"] == latest]
    levels = []
    for _, r in sub.iterrows():
        lower = _parse_level_lower_bound(r[level_col])
        pct = pd.to_numeric(r[pct_col], errors="coerce")
        if lower is None or pd.isna(pct):
            continue   # 『差異數/合計』等無級距下界的彙總列
        people = None
        if people_col and pd.notna(r.get(people_col)):
            try:
                people = int(float(r[people_col]))
            except (ValueError, TypeError):
                people = None
        levels.append({"lower": float(lower), "label": str(r[level_col]),
                       "pct": float(pct), "people": people})
    if not levels:
        return {}
    levels.sort(key=lambda x: x["lower"])
    return {"date": latest.strftime("%Y-%m-%d"), "levels": levels}


# ---------- FinMind fundamentals (monthly revenue, EPS, PER/yield) ----------

def fetch_monthly_revenue(stock_id: str, months: int = 18) -> pd.DataFrame:
    """Monthly revenue history with YoY computed.
    Returns DataFrame indexed by year_month (string YYYY-MM), columns: revenue, revenue_yoy.
    """
    end = date.today()
    start = (end.replace(day=1) - timedelta(days=months * 32))
    rows = fetch_finmind(
        "TaiwanStockMonthRevenue",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "revenue" not in df.columns:
        return pd.DataFrame()
    if "revenue_year" in df.columns and "revenue_month" in df.columns:
        df["ym"] = df["revenue_year"].astype(int).astype(str).str.zfill(4) + "-" + \
                   df["revenue_month"].astype(int).astype(str).str.zfill(2)
    else:
        df["ym"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    df = df.drop_duplicates(subset=["ym"]).sort_values("ym")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df = df.dropna(subset=["revenue"])
    df = df.set_index("ym")[["revenue"]]
    df["revenue_yoy"] = df["revenue"].pct_change(periods=12)
    return df


def fetch_eps_quarterly(stock_id: str, quarters: int = 6) -> pd.DataFrame:
    """Quarterly EPS history. Returns DataFrame indexed by quarter_end date, column: eps."""
    end = date.today()
    start = end - timedelta(days=quarters * 100)
    rows = fetch_finmind(
        "TaiwanStockFinancialStatements",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if not {"type", "value", "date"}.issubset(df.columns):
        return pd.DataFrame()
    eps_keys = {"EPS", "EarningsPerShare", "EPS_Quarter", "BasicEPS", "EarningsPerShareBasic"}
    eps = df[df["type"].isin(eps_keys)].copy()
    if eps.empty:
        return pd.DataFrame()
    eps["value"] = pd.to_numeric(eps["value"], errors="coerce")
    eps = eps.dropna(subset=["value"])
    eps = eps.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(eps["date"]).dt.tz_localize(None).dt.normalize()
    out["eps"] = eps["value"].values
    return out.set_index("date").sort_index()


def _fetch_statement(dataset: str, stock_id: str, type_map: dict[str, tuple], quarters: int = 8) -> pd.DataFrame:
    """通用財報 pivot:FinMind 的 type/value/date 長表 → 以季末日為 index、type_map 鍵為欄的寬表。
    type_map = {欄名: (可能的 FinMind type 名稱...)}。任何缺漏回空欄,整個失敗回空 DataFrame。"""
    end = date.today()
    start = end - timedelta(days=quarters * 100)
    rows = fetch_finmind(dataset, data_id=stock_id,
                         start_date=start.isoformat(), end_date=end.isoformat())
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if not {"type", "value", "date"}.issubset(df.columns):
        return pd.DataFrame()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    df = df.dropna(subset=["date"])
    out = pd.DataFrame(index=pd.Index(sorted(df["date"].unique()), name="date"))
    for col, candidates in type_map.items():
        sub = df[df["type"].isin(candidates)]
        if not sub.empty:
            out[col] = sub.groupby("date")["value"].last()
    return out.sort_index().dropna(how="all")


# FinMind 財報 type 名稱在不同期間/版本略有差異,各給幾個候選。
_FS_TYPES = {
    "revenue": ("Revenue", "OperatingRevenue", "TotalOperatingRevenue"),
    "gross_profit": ("GrossProfit", "GrossProfitLoss", "GrossProfitFromOperations"),
    "operating_income": ("OperatingIncome", "OperatingIncomeLoss", "OperatingProfit", "NetOperatingIncome"),
    "net_income": ("IncomeAfterTaxes", "ProfitAfterTax", "NetIncome", "ProfitLoss",
                   "IncomeAfterTax", "NetIncomeLoss"),
    "eps": ("EPS", "BasicEarningsPerShare", "EarningsPerShare"),
    # 註:利息費用(interest_expense)原放這裡,但 2026-07-24 以真實 FinMind 回應實測發現
    # TaiwanStockFinancialStatements(損益表)根本沒有利息費用這一列 —— 真正的 InterestExpense
    # 在現金流量表(TaiwanStockCashFlowsStatement),故已移到下方 _CF_TYPES。
}
_BS_TYPES = {
    "total_assets": ("TotalAssets",),
    "total_liab": ("TotalLiabilities", "Liabilities"),
    "equity": ("Equity", "TotalEquity", "EquityAttributableToOwnersOfParent", "TotalEquityAndLiabilities"),
    # 個股健檢新增,2026-06-30(流動比/速動比/應收應付異常/EV 用)。候選名稱未實測驗證,
    # 缺資料時對應指標回 None(健檢 Metric 契約的 missing_reason),不影響既有信心分流程。
    "current_assets": ("CurrentAssets",),
    "current_liab": ("CurrentLiabilities",),
    "inventory": ("Inventories", "Inventory"),
    "accounts_receivable": ("AccountsReceivableNet", "NotesAccountsReceivableNet", "AccountsReceivable"),
    "cash": ("CashAndCashEquivalents", "Cash"),
    "short_term_debt": ("ShortTermBorrowings", "ShortTermLoans"),
    "long_term_debt": ("LongTermBorrowings", "BondsPayable", "LongTermLoansPayable"),
}
_CF_TYPES = {
    # 現金流量表的數字是「當年度累計(YTD)」—— Q1=3個月、Q2=6個月...Q4=全年。健檢模組取用前會先
    # 去累計還原成單季(見 scripts/health/quarterly.py 的 ttm_flow),才能跟損益表的單季值同基期相除。
    "op_cashflow": ("CashFlowsFromOperatingActivities", "NetCashInflowFromOperatingActivities",
                    "CashFlowsProvidedFromOperatingActivities", "NetCashProvidedByOperatingActivities",
                    "CashProvidedByOperatingActivities"),
    "invest_cashflow": ("CashProvidedByInvestingActivities", "CashFlowsProvidedFromInvestingActivities",
                        "CashFlowsFromInvestingActivities"),
    # 2026-07-24 以真實 FinMind 回應實測:資本支出實名為 PropertyAndPlantAndEquipment(投資活動段的
    # 不動產廠房設備支出,通常為負);折舊/攤銷是分開的兩列 Depreciation / AmortizationExpense
    # (原候選 DepreciationAmortizationExpense 等一個都對不上 → EV/EBITDA 恆缺);利息費用 InterestExpense
    # 也在現金流表(損益表沒有)。三者原候選名稱全部命中失敗,是健檢三個指標長期 0% 覆蓋率的真因。
    "capex": ("PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment",
              "PaymentsToAcquirePropertyPlantAndEquipment"),
    "depreciation": ("Depreciation", "DepreciationExpense"),
    "amortization": ("AmortizationExpense", "Amortization"),
    "interest_expense": ("InterestExpense", "FinanceCosts", "InterestExpenseNet"),
}


def fetch_financial_statements(stock_id: str, quarters: int = 8) -> pd.DataFrame:
    """季財報明細(營收/毛利/營益/淨利/EPS)。空 DataFrame on failure."""
    return _fetch_statement("TaiwanStockFinancialStatements", stock_id, _FS_TYPES, quarters)


def fetch_balance_sheet(stock_id: str, quarters: int = 8) -> pd.DataFrame:
    """季資產負債(總資產/總負債/股東權益)。空 DataFrame on failure."""
    return _fetch_statement("TaiwanStockBalanceSheet", stock_id, _BS_TYPES, quarters)


def fetch_cashflow(stock_id: str, quarters: int = 8) -> pd.DataFrame:
    """季現金流(營業/投資現金流、資本支出)。空 DataFrame on failure."""
    return _fetch_statement("TaiwanStockCashFlowsStatement", stock_id, _CF_TYPES, quarters)


def fetch_day_trade_ratio(stock_id: str, days: int = 10) -> float | None:
    """個股「當日沖銷比」= 當沖成交量 / 總成交量(取最近一個有值的交易日)。
    來源 FinMind TaiwanStockDayTrading(當日沖銷交易標的及成交量值,免費)。當沖比過高 = 隔日沖對手盤多、
    隔天賣壓重(3.4-2)。任一步失敗(無 token / dataset 失效 / 欄位對不上)回 None → 呼叫端當「無資料」不扣分不報錯。

    FinMind 欄位名稱在不同期間略有差異,給幾個候選;抓不到就回 None(與全檔其餘 FinMind 存取一致的優雅降級)。"""
    end = date.today()
    start = end - timedelta(days=days * 2)
    rows = fetch_finmind(
        "TaiwanStockDayTrading",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return None
    # 當沖量欄位候選 / 總量欄位候選(FinMind 版本差異)
    dt_col = next((c for c in ("DayTradingVolume", "Volume", "day_trading_volume") if c in df.columns), None)
    tot_col = next((c for c in ("TotalVolume", "StockVolume", "total_volume", "TradeVolume") if c in df.columns), None)
    # 有些版本直接給比率
    ratio_col = next((c for c in ("DayTradingRatio", "day_trading_ratio", "Ratio") if c in df.columns), None)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    if df.empty:
        return None
    last = df.iloc[-1]
    try:
        if ratio_col and pd.notna(last.get(ratio_col)):
            r = float(last[ratio_col])
            return r / 100.0 if r > 1.5 else r          # 若是百分比(>1.5 視為 %)換算成 0~1
        if dt_col and tot_col:
            dt = float(last[dt_col]); tot = float(last[tot_col])
            if tot > 0:
                return max(0.0, min(1.0, dt / tot))
    except (ValueError, TypeError):
        return None
    return None


def fetch_per_yield(stock_id: str, days: int = 10) -> pd.DataFrame:
    """Recent days of PER / dividend yield / PBR. Returns DataFrame indexed by date."""
    end = date.today()
    start = end - timedelta(days=days * 2)
    rows = fetch_finmind(
        "TaiwanStockPER",
        data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    for src, dst in (("PER", "pe"), ("PBR", "pb"), ("dividend_yield", "yield_pct")):
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    return out.set_index("date").sort_index().dropna(how="all")


# ---------- TWSE valuation snapshot (PE / yield / PB, free, official, bulk) ----------

_TWSE_BWIBBU_URLS = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
    "https://www.twse.com.tw/exchangeReport/BWIBBU_d",
)


def _to_float(x):
    try:
        s = str(x).replace(",", "").strip()
        if s in ("", "-", "N/A", "--"):
            return None
        return float(s)
    except Exception:
        return None


def fetch_valuation_snapshot(d: date | None = None) -> dict[str, dict]:
    """All listed (TWSE) stocks' PE / yield / PB for one day, in ONE official
    free call. Returns {stock_id: {pe, yield_pct, pb}}. Walks back a few days
    to skip holidays. Empty dict on failure (caller degrades gracefully).
    """
    d = d or date.today()
    for back in range(4):
        ymd = (d - timedelta(days=back)).strftime("%Y%m%d")
        for url in _TWSE_BWIBBU_URLS:
            try:
                j = http_get_json(url, params={"date": ymd, "selectType": "ALL", "response": "json"},
                                  retries=1, delay=2.0)
            except Exception:
                continue
            if not isinstance(j, dict):
                continue
            fields = j.get("fields") or []
            data = j.get("data") or []
            if not fields or not data:
                continue

            def find_idx(*names):
                for i, f in enumerate(fields):
                    if any(n in str(f) for n in names):
                        return i
                return None

            i_id = find_idx("證券代號", "代號")
            i_pe = find_idx("本益比")
            i_yield = find_idx("殖利率")
            i_pb = find_idx("股價淨值比", "淨值比")
            if i_id is None:
                continue
            out: dict[str, dict] = {}
            for row in data:
                try:
                    sid = str(row[i_id]).strip()
                except Exception:
                    continue
                if not sid:
                    continue
                rec = {}
                if i_pe is not None:
                    rec["pe"] = _to_float(row[i_pe])
                if i_yield is not None:
                    rec["yield_pct"] = _to_float(row[i_yield])
                if i_pb is not None:
                    rec["pb"] = _to_float(row[i_pb])
                out[sid] = rec
            if out:
                log.info(f"Valuation snapshot: {len(out)} stocks ({ymd})")
                return out
    log.warning("Valuation snapshot empty (TWSE BWIBBU unavailable)")
    return {}


_TPEX_PERATIO_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"


def fetch_valuation_snapshot_tpex() -> dict[str, dict]:
    """All OTC(上櫃,TPEx)stocks' PE / yield / PB,最新可用交易日,ONE official free call(無需日期參數,
    遇假日會自動回最近交易日)。回傳格式跟 fetch_valuation_snapshot(TWSE 上市)一致,供合併使用 —
    解決品質面向「上櫃股估值快照常缺,只能拿中性0.5」的問題(2026-06-27 加)。失敗回空字典,呼叫端降級不死。
    """
    try:
        j = http_get_json(_TPEX_PERATIO_URL, retries=1, delay=2.0)
    except Exception as e:
        log.warning(f"TPEx valuation snapshot failed: {e}")
        return {}
    if not isinstance(j, list):
        return {}
    out: dict[str, dict] = {}
    for row in j:
        sid = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not sid:
            continue
        rec = {}
        pe = _to_float(row.get("PriceEarningRatio"))
        if pe is not None:
            rec["pe"] = pe
        yld = _to_float(row.get("YieldRatio"))
        if yld is not None:
            rec["yield_pct"] = yld
        pb = _to_float(row.get("PriceBookRatio"))
        if pb is not None:
            rec["pb"] = pb
        if rec:
            out[sid] = rec
    if out:
        log.info(f"TPEx valuation snapshot: {len(out)} stocks")
    else:
        log.warning("TPEx valuation snapshot empty")
    return out


# ---------- 受限股名單(全額交割 / 處置 / 管理 / 停止買賣)----------
# 全部免費、官方 OpenAPI。任一來源失敗只記錄、不影響其他來源;全掛掉回空集合 = 不過濾
# (不會誤殺,只是少一層保護)。用途:把「採分盤撮合(約 5~20 分鐘一次)、流動性瞬間歸零」
# 的股票排除出選股池 —— 對隔日沖是實務大忌(twse 審查 Bug 4)。

_TWSE_ALTERED_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT85U"       # 集中市場證券變更交易(全額交割)
_TWSE_PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"          # 集中市場處置股票
_TPEX_CMODE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_cmode"                # 上櫃變更交易/分盤/管理/停止買賣


def _is_4digit_stock(code) -> bool:
    """只認 4 位純數字股票代號(與 filter_tradable_stocks 的 universe 一致);
    權證/可轉債(5~6 位)、ETF 皆非本系統選股標的,不需納入受限判定。"""
    return isinstance(code, str) and code.isdigit() and len(code) == 4


def _roc_period_end(period) -> "date | None":
    """處置期間字串的『迄日』→ 西元 date。支援兩種官方格式:
      TWSE '115/07/03～115/07/16'(民國年/月/日,全形波浪號)
      TPEX '1150707~1150720'(民國 yyymmdd,半形波浪號)
    解析失敗回 None(呼叫端對 None 採保守處理:視為仍在處置)。"""
    import re
    if not isinstance(period, str) or not period.strip():
        return None
    # 取分隔符後半段(迄日);涵蓋常見的波浪號/破折號/「至」「到」
    tail = re.split(r"[~～〰〜\-–—－至到]", period)[-1].strip()
    nums = re.findall(r"\d+", tail)
    try:
        if len(nums) >= 3:                       # '115','07','16'
            roc_y, mo, d = int(nums[0]), int(nums[1]), int(nums[2])
        elif len(nums) == 1 and len(nums[0]) >= 7:  # '1150720' → 民國115年07月20日
            s = nums[0]
            roc_y, mo, d = int(s[:-4]), int(s[-4:-2]), int(s[-2:])
        else:
            return None
        return date(roc_y + 1911, mo, d)
    except (ValueError, IndexError):
        return None


def fetch_restricted_stocks(today: "date | None" = None) -> set[str]:
    """當前『不宜短線進場』的 4 位數股票代號集合:全額交割(變更交易)、處置(分盤撮合)、
    管理股票、停止買賣。來源為 TWSE/TPEX 官方免費 OpenAPI。

    處置類帶『處置期間』→ 迄日 < today(已結束)不排除,解析不出迄日則保守排除;
    變更交易/管理/停止買賣為當日狀態快照 → 直接排除。任一來源失敗只記錄、續跑其他來源。
    """
    today = today or date.today()
    out: set[str] = set()

    # 1) TWSE 變更交易(全額交割)— 當日快照,全數排除
    try:
        rows = http_get_json(_TWSE_ALTERED_URL, retries=1, delay=2.0)
        if isinstance(rows, list):
            for r in rows:
                c = str(r.get("Code", "")).strip()
                if _is_4digit_stock(c):
                    out.add(c)
    except Exception as e:
        log.warning(f"TWSE 變更交易名單抓取失敗(略過此來源):{e}")

    # 2) TWSE 處置(分盤)— 依處置期間迄日過濾
    try:
        rows = http_get_json(_TWSE_PUNISH_URL, retries=1, delay=2.0)
        if isinstance(rows, list):
            for r in rows:
                c = str(r.get("Code", "")).strip()
                if not _is_4digit_stock(c):
                    continue
                end = _roc_period_end(r.get("DispositionPeriod", ""))
                if end is None or end >= today:   # 解析不出迄日 → 保守排除
                    out.add(c)
    except Exception as e:
        log.warning(f"TWSE 處置名單抓取失敗(略過此來源):{e}")

    # 3) TPEX 變更交易/分盤/管理/停止買賣 — 當日狀態快照(欄位為 'Ｙ'/'' 旗標)
    try:
        rows = http_get_json(_TPEX_CMODE_URL, retries=1, delay=2.0)
        if isinstance(rows, list):
            for r in rows:
                c = str(r.get("SecuritiesCompanyCode", "")).strip()
                if not _is_4digit_stock(c):
                    continue
                flags = (r.get("AlteredTrading"), r.get("PeriodicTrading"),
                         r.get("ManagedStock"), r.get("SuspensionOfTrading"))
                if any(str(v).strip() in ("Y", "Ｙ") for v in flags):
                    out.add(c)
    except Exception as e:
        log.warning(f"TPEX 變更交易/分盤名單抓取失敗(略過此來源):{e}")

    if out:
        log.info(f"受限股名單(全額交割/處置/管理/停止買賣):{len(out)} 檔")
    else:
        log.warning("受限股名單為空(來源皆不可用或今日確無);本次不過濾受限股")
    return out


# ---------- 盤前 / 即時(premarket)----------
# 全部免費、非官方/盡力而為;任何失敗都回空(呼叫端降級不整包死)。

def _yf_overnight(ticker: str) -> dict | None:
    """抓某商品最近兩個收盤算隔夜漲跌%。給美股盤前閘門(SOX/NQ/VIX)與 ADR 用。"""
    try:
        df = yf.download(ticker, period="7d", progress=False, auto_adjust=False, threads=False)
    except Exception as e:
        log.warning(f"yf overnight {ticker} failed: {e}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if len(close) < 2:
        return None
    last = float(close.iloc[-1]); prev = float(close.iloc[-2])
    pct = (last / prev - 1) * 100 if prev else None
    return {"last": round(last, 2), "prev": round(prev, 2),
            "pct": round(pct, 2) if pct is not None else None}


def fetch_market_gate() -> dict:
    """隔夜國際盤(已收/仍在交易)當『大盤盤前閘門』:費半 SOX、NASDAQ 期貨、S&P 期貨、VIX,
    再加台積電 ADR(TSM)——台積電佔大盤約 3 成權重,其 ADR 隔夜跳空是整個指數與電子的強領先。
    回傳 {sox/nasdaq/sp500/vix/tsm: {last, prev, pct}};抓不到的鍵略過。"""
    out: dict[str, dict] = {}
    for key, ticker in (("sox", "^SOX"), ("nasdaq", "NQ=F"), ("sp500", "ES=F"),
                        ("vix", "^VIX"), ("tsm", "TSM")):
        d = _yf_overnight(ticker)
        if d:
            out[key] = d
    return out


def fetch_tx_night() -> dict | None:
    """台指期(TX)夜盤(盤後 15:00–翌日 05:00)隔夜漲跌 —— 台股『自己』對隔夜消息的重定價,
    盤前閘門最直接的訊號(涵蓋美股開盤前段,且是台股本身的籃子)。

    FinMind TaiwanFuturesDaily 中 trading_session='after_market' 的那筆,經實測:
    標記日期 D 的夜盤實際是『D-1 傍晚 15:00 開 → D 清晨 05:00 收』,其 spread_per 即
    『夜盤收盤 vs 前一交易日日盤收盤』的漲跌%(近月合約 = 同日同 session 成交量最大者)。

    回傳 {date, pct, close, volume, is_today};抓不到 / token 缺 / 夜盤未更新 → None。
    ⚠ is_today=False 代表今晨的夜盤尚未被 FinMind 發布(08:45 可能有發布延遲),
    呼叫端應忽略夜盤、改用美股代理(ES/NQ 在盤前仍在交易,本就涵蓋美股隔夜)。"""
    today = now_tpe().date()
    rows = fetch_finmind("TaiwanFuturesDaily", data_id="TX",
                         start_date=(today - timedelta(days=7)).isoformat(),
                         end_date=today.isoformat())
    if not rows:
        return None
    # 只留夜盤 + 純月份合約(排除價差單如 '202607/202608')
    night = [r for r in rows
             if r.get("trading_session") == "after_market"
             and "/" not in str(r.get("contract_date", ""))
             and len(str(r.get("contract_date", ""))) == 6]
    if not night:
        return None
    latest = max(r["date"] for r in night)
    front = max((r for r in night if r["date"] == latest),
               key=lambda r: r.get("volume", 0) or 0)  # 近月 = 成交量最大
    pct = front.get("spread_per")
    return {
        "date": latest,
        "pct": round(float(pct), 2) if pct is not None else None,
        "close": front.get("close"),
        "volume": front.get("volume"),
        "is_today": latest == today.isoformat(),
    }


def fetch_futures_inst_net_oi(days: int = 90, futures_id: str = "TX") -> pd.Series:
    """台指期(TX)三大法人「未平倉淨口數」時間序列 —— 大盤 risk-on/off 判讀用。

    多空未平倉餘額相減後加總三大法人 = 法人在期貨的**部位方向**(非當日買賣),
    實證(2026-07-18 整合回測)比「當日買賣超」更能分辨後續選股表現:
    淨 OI 高於 20 日均 → risk-on 段的選股平均淨報酬 +0.11%,反之 −0.51%。

    回傳 index=date 的 Series(口數);抓不到回空 Series(呼叫端降級成只看指數均線)。
    """
    end = date.today()
    start = end - timedelta(days=days)
    rows = fetch_finmind("TaiwanFuturesInstitutionalInvestors", data_id=futures_id,
                         start_date=start.isoformat(), end_date=end.isoformat())
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    lo, sh = "long_open_interest_balance_volume", "short_open_interest_balance_volume"
    if not {lo, sh, "date"}.issubset(df.columns):
        return pd.Series(dtype=float)
    df["net"] = pd.to_numeric(df[lo], errors="coerce").fillna(0) - \
                pd.to_numeric(df[sh], errors="coerce").fillna(0)
    s = df.groupby("date")["net"].sum()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s.sort_index()


def fetch_adr_changes(adr_map: dict[str, str]) -> dict[str, dict]:
    """台股代號 → 美股 ADR 的隔夜漲跌(個股級盤前佐證)。回傳 {stock_id: {ticker, last, pct}}."""
    out: dict[str, dict] = {}
    for sid, tk in (adr_map or {}).items():
        d = _yf_overnight(tk)
        if d:
            out[sid] = {"ticker": tk, **d}
    return out


def fetch_intraday_1m(stock_id: str, market: str) -> pd.DataFrame:
    """yfinance 當日 1 分K(開盤區間/ORB 用)。回傳 index 為台北時區的 OHLCV;空 DataFrame on failure。
    用歷史分K(非即時快照)→ 即使 Actions 延遲到開盤後才跑,09:00–09:15 區間仍在,能正確重建。"""
    ticker = yf_ticker(stock_id, market)
    try:
        df = yf.download(ticker, period="1d", interval="1m",
                         progress=False, auto_adjust=False, threads=False)
    except Exception as e:
        log.warning(f"intraday 1m {ticker} failed: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume": "volume"})
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    try:
        idx = df.index
        df.index = idx.tz_convert("Asia/Taipei") if idx.tz is not None else idx.tz_localize("Asia/Taipei")
    except Exception:
        pass
    return df


_MIS_INDEX = "https://mis.twse.com.tw/stock/index.jsp"
_MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"


def _mis_num(x):
    try:
        s = str(x).replace(",", "").strip()
        if s in ("", "-", "--", "N/A"):
            return None
        return float(s)
    except Exception:
        return None


def fetch_mis_quotes(symbols: list[tuple[str, str]]) -> dict[str, dict]:
    """TWSE MIS 即時/盤前試撮報價(免金鑰、非官方)。symbols=[(stock_id, market)]。
    08:30–09:00 試撮時 z 可能為 '-',改用最佳買賣中價 / 開盤 / 昨收估『預估開盤價』。
    回傳 {stock_id: {price, src, open, high, low, prev_close, acc_vol, name}}。失敗回 {}。"""
    if not symbols:
        return {}

    def ch(sid: str, mkt: str) -> str:
        return ("otc_" if mkt == "tpex" else "tse_") + f"{sid}.tw"

    out: dict[str, dict] = {}
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": _MIS_INDEX})
    try:
        sess.get(_MIS_INDEX, timeout=15)   # 先取 cookie,MIS 對無 cookie 的請求常擋
    except Exception:
        pass
    for batch in chunked(list(symbols), 50):
        ex_ch = "|".join(ch(s, m) for s, m in batch)
        params = {"ex_ch": ex_ch, "json": "1", "delay": "0", "_": str(int(time.time() * 1000))}
        try:
            r = sess.get(_MIS_API, params=params, timeout=20)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            log.warning(f"MIS fetch failed: {e}")
            continue
        for it in (j.get("msgArray") or []):
            sid = str(it.get("c", "")).strip()
            if not sid:
                continue
            z = _mis_num(it.get("z")); o = _mis_num(it.get("o"))
            h = _mis_num(it.get("h")); l = _mis_num(it.get("l")); y = _mis_num(it.get("y"))
            bid = _mis_num((it.get("b") or "").split("_")[0])
            ask = _mis_num((it.get("a") or "").split("_")[0])
            if z is not None:
                price, src = z, "成交/試撮"
            elif bid is not None and ask is not None:
                price, src = round((bid + ask) / 2, 2), "買賣中價"
            elif o is not None:
                price, src = o, "開盤"
            else:
                price, src = y, "昨收"
            out[sid] = {"price": price, "src": src, "open": o, "high": h, "low": l,
                        "prev_close": y, "acc_vol": _mis_num(it.get("v")), "name": it.get("n", "")}
        time.sleep(0.4)
    return out


# ---------- Google News RSS ----------

def fetch_news(stock_id: str, name: str, limit: int = 10) -> list[dict]:
    q = urllib.parse.quote(f"{stock_id} {name}")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning(f"news {stock_id} failed: {e}")
        return []
    items = []
    for e in feed.entries[:limit]:
        # published_parsed 是 feedparser 已解析好的 time.struct_time,比自己再 parse
        # 原始 RFC822 字串(如 catalyst.py 舊作法 [:16] 截字串)可靠;個股健檢 News Engine
        # 要把新聞按 7/30/90 天分桶,需要這個乾淨日期。失敗就留 None,不影響既有 published 欄位。
        published_date = None
        pp = getattr(e, "published_parsed", None)
        if pp:
            try:
                published_date = date(pp.tm_year, pp.tm_mon, pp.tm_mday).isoformat()
            except Exception:
                published_date = None
        items.append({
            "title": getattr(e, "title", ""),
            "link": getattr(e, "link", ""),
            "published": getattr(e, "published", ""),
            "published_date": published_date,
            "source": getattr(getattr(e, "source", None), "title", ""),
        })
    return items


# ---------- 新聞內文抓取(讀內文版新聞分析用,2026-07-09)----------
# 個股健檢 News Engine 原本只把「標題」餵給 AI,但台股新聞標題常誇大/與內文不符。
# 這裡 best-effort 抓每則新聞的實際內文摘要(發布者 og:description + 內文段落),
# 讓 AI 依內容判斷。任何一步失敗(Google News 轉址解不開/發布者擋爬蟲/逾時)→ 該則
# 只留標題,絕不讓整個健檢失敗。全程 time-box,serverless(Vercel)也能在 timeout 內收斂。

# 一般瀏覽器 UA:多數新聞站對預設 requests UA / 我們的 twse-screener UA 會擋或給空殼頁。
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


_GNEWS_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def _decode_gnews_url(url: str) -> "str | None":
    """快路徑:舊版 Google News link(`/articles/CBMi...`)的 base64 路徑段解開後(protobuf)
    直接內嵌原始文章網址時,用 RFC3986 字元集正則抓出來(遇非網址位元組即停)。新版路徑段內
    只是不可解的內部 ID(如 'AU_yq...',非 http)→ 回 None,呼叫端改走 batchexecute RPC。"""
    import base64, re
    m = re.search(r"/articles/([A-Za-z0-9_\-]+)", url)
    if not m:
        return None
    seg = m.group(1)
    seg += "=" * (-len(seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(seg)
    except Exception:
        return None
    text = raw.decode("latin-1", "ignore")
    m2 = re.search(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", text)
    if not m2:
        return None
    cand = m2.group(0)
    if len(cand) < 12 or "." not in cand:
        return None
    return cand


def _resolve_gnews_batchexecute(url: str, session: "requests.Session", timeout: float) -> "str | None":
    """解新版 Google News opaque URL:GET 文章殼頁取 c-wiz 的簽章(data-n-a-sg)、時間戳
    (data-n-a-ts)、內部 id(data-n-a-id),再 POST Google 私有的 batchexecute RPC 換回
    真實發布者網址。這是 Google 未公開的內部協定,未來 Google 若改版可能失效 → 全程包 try,
    失敗回 None(該則新聞退回只用標題,不影響健檢)。"""
    import json, re
    try:
        r = session.get(url, timeout=timeout)
        html = r.text
    except Exception:
        return None

    def _attr(name: str) -> "str | None":
        m = re.search(name + r'="([^"]+)"', html)
        return m.group(1) if m else None

    gid, sig, ts = _attr("data-n-a-id"), _attr("data-n-a-sg"), _attr("data-n-a-ts")
    if not (gid and sig and ts):
        return None
    try:
        inner = ["garturlreq",
                 [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                   None, None, None, None, None, 0, 1],
                  "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                 gid, int(ts), sig]
        freq = [[["Fbv4je", json.dumps(inner), None, "generic"]]]
        data = "f.req=" + urllib.parse.quote(json.dumps(freq))
        r2 = session.post(_GNEWS_BATCH, data=data, timeout=timeout,
                          headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
    except Exception:
        return None
    m = re.search(r'\[\\"garturlres\\",\\"(https?:.*?)\\"', r2.text)
    if not m:
        return None
    try:
        real = m.group(1).encode("latin-1", "ignore").decode("unicode_escape")
    except Exception:
        real = m.group(1).replace("\\/", "/")
    return real if real.startswith("http") and "google.com" not in urllib.parse.urlparse(real).netloc.lower() else None


def _resolve_article_url(url: str, session: "requests.Session | None" = None,
                         timeout: float = 5.0) -> "str | None":
    """把新聞連結解成可直接抓的真實文章網址。非 Google News 直接用;是 Google News 轉址就
    先試 base64 快路徑(舊格式),失敗再走 batchexecute RPC(新格式)。"""
    if not url:
        return None
    host = urllib.parse.urlparse(url).netloc.lower()
    if "news.google.com" not in host:
        return url
    decoded = _decode_gnews_url(url)
    if decoded:
        return decoded
    sess = session or requests.Session()
    if session is None:
        sess.headers.update({"User-Agent": _BROWSER_UA})
    return _resolve_gnews_batchexecute(url, sess, timeout)


def _extract_article_text(html: str, max_chars: int = 600) -> str:
    """從文章 HTML 抽乾淨的內文摘要:優先取發布者 meta 摘要(og:description /
    description —— 這些在靜態 HTML 就有,即使內文是 JS 渲染或半付費牆也拿得到,且是
    發布者自己寫的摘要而非誇大標題),再補內文 <p> 段落到 max_chars 為止。"""
    from bs4 import BeautifulSoup
    import re
    soup = BeautifulSoup(html[:200_000], "lxml")
    parts: list[str] = []
    seen: set[str] = set()

    def _add(t: str):
        t = re.sub(r"\s+", " ", (t or "")).strip()
        if len(t) >= 20 and t not in seen:
            seen.add(t)
            parts.append(t)

    for attrs in ({"property": "og:description"}, {"name": "description"},
                  {"name": "twitter:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            _add(tag["content"])
            break

    container = soup.find("article") or soup.body or soup
    for p in container.find_all("p"):
        _add(p.get_text(" ", strip=True))
        if sum(len(x) for x in parts) >= max_chars:
            break

    return " ".join(parts)[:max_chars].strip()


def _fetch_one_article(item: dict, timeout: float, max_chars: int) -> str:
    # 每則自帶一個 Session:對 news.google.com 的殼頁 GET + batchexecute POST 共用連線,
    # 且執行緒之間不共享 Session(requests.Session 非執行緒安全)。
    sess = requests.Session()
    sess.headers.update({"User-Agent": _BROWSER_UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    try:
        real = _resolve_article_url(item.get("link", ""), session=sess, timeout=timeout)
        if not real:
            return ""
        r = sess.get(real, timeout=timeout)
        if r.status_code != 200 or not r.text:
            return ""
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding      # 台股新聞常 UTF-8/Big5,標頭沒宣告時自動偵測
        return _extract_article_text(r.text, max_chars=max_chars)
    except Exception:
        return ""
    finally:
        sess.close()


def enrich_news_content(news_items: list[dict], *, limit: int = 10, timeout: float = 5.0,
                        max_workers: int = 8, max_chars: int = 600, budget: float = 30.0) -> int:
    """對前 limit 則新聞 best-effort 抓內文,成功者就地寫入 item['content']。回傳成功則數。
    平行抓取 + 整體 wall-clock budget 上限(保護 serverless timeout);任一則失敗只是沒 content。"""
    if not news_items:
        return 0
    from concurrent.futures import ThreadPoolExecutor
    targets = [n for n in news_items[:limit] if n.get("link")]
    if not targets:
        return 0
    start = time.monotonic()
    count = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
        futs = {ex.submit(_fetch_one_article, it, timeout, max_chars): it for it in targets}
        for fut, it in futs.items():
            remaining = budget - (time.monotonic() - start)
            if remaining <= 0:
                break
            try:
                content = fut.result(timeout=remaining)
            except Exception:
                content = ""
            if content:
                it["content"] = content
                count += 1
    return count
