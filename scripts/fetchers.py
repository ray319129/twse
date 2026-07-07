from __future__ import annotations
import logging as _logging
import time
import urllib.parse
from datetime import date, datetime, timedelta
import pandas as pd
import requests
import feedparser
import yfinance as yf

from .config import FINMIND_TOKEN, META_DIR
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
    # 個股健檢(利息保障倍數)新增,2026-06-30。候選名稱未實測驗證,缺資料時 interest_expense
    # 會是 None(健檢模組自動跳過該指標,不影響其他面向),不影響既有 fundamental_bonus 流程。
    "interest_expense": ("InterestExpense", "FinanceCosts", "InterestExpenseNet"),
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
    "op_cashflow": ("CashFlowsProvidedFromOperatingActivities", "CashFlowsFromOperatingActivities",
                    "NetCashProvidedByOperatingActivities", "CashProvidedByOperatingActivities"),
    "invest_cashflow": ("CashFlowsProvidedFromInvestingActivities", "CashFlowsFromInvestingActivities"),
    "capex": ("AcquisitionOfPropertyPlantAndEquipment", "PaymentsToAcquirePropertyPlantAndEquipment"),
    # 個股健檢新增,2026-06-30(EV/EBITDA 用)。
    "depreciation_amortization": ("DepreciationAmortizationExpense", "DepreciationDepletionAndAmortisation",
                                   "DepreciationAndAmortisationExpenseContinuingOperations"),
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
    """隔夜國際盤(已收)當『大盤盤前閘門』:費半 SOX、NASDAQ 期貨、S&P 期貨、VIX。
    回傳 {sox/nasdaq/sp500/vix: {last, prev, pct}};抓不到的鍵略過。"""
    out: dict[str, dict] = {}
    for key, ticker in (("sox", "^SOX"), ("nasdaq", "NQ=F"), ("sp500", "ES=F"), ("vix", "^VIX")):
        d = _yf_overnight(ticker)
        if d:
            out[key] = d
    return out


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
