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
    df.to_parquet(cache, index=False)
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
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
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
}
_BS_TYPES = {
    "total_assets": ("TotalAssets",),
    "total_liab": ("TotalLiabilities", "Liabilities"),
    "equity": ("Equity", "TotalEquity", "EquityAttributableToOwnersOfParent", "TotalEquityAndLiabilities"),
}
_CF_TYPES = {
    "op_cashflow": ("CashFlowsProvidedFromOperatingActivities", "CashFlowsFromOperatingActivities",
                    "NetCashProvidedByOperatingActivities", "CashProvidedByOperatingActivities"),
    "invest_cashflow": ("CashFlowsProvidedFromInvestingActivities", "CashFlowsFromInvestingActivities"),
    "capex": ("AcquisitionOfPropertyPlantAndEquipment", "PaymentsToAcquirePropertyPlantAndEquipment"),
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
        items.append({
            "title": getattr(e, "title", ""),
            "link": getattr(e, "link", ""),
            "published": getattr(e, "published", ""),
            "source": getattr(getattr(e, "source", None), "title", ""),
        })
    return items
