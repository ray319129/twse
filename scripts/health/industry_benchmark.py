"""同業平均(Industry Benchmark)— 個股健檢用,零額外 API 成本。

全市場 ~1900 檔不可能天天對每檔抓 FinMind 財報(402 教訓,見專案記憶)。改吃「批次路徑
本來就會發生」的副產品:`data/financials|balance/*.parquet` 會隨著核心榜每天輪動,逐漸
累積出越來越多支股票的快取。本模組每次批次跑完,把當下本地已快取、且查得到產業分類的
股票財務比率彙總成 `data/health/industry_benchmarks.json`,隨 GitHub Actions commit、
隨 docs/ 一起發布成公開靜態檔 —— 即時路徑(serverless)直接讀這份檔案當同業基準,
不需要自己重新聚合全市場。

樣本不足(< MIN_SAMPLE)的產業整個跳過,不產生「假裝有同業平均」的數字。
"""
from __future__ import annotations
import json
from datetime import date

import pandas as pd

from ..storage import FINANCIALS_DIR, BALANCE_DIR, load_financials, load_balance, load_cashflow
from ..fetchers import fetch_stock_info
from ..config import DATA_DIR
from . import quarterly as q

HEALTH_DATA_DIR = DATA_DIR / "health"
try:
    HEALTH_DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # Vercel serverless: read-only filesystem
BENCHMARK_PATH = HEALTH_DATA_DIR / "industry_benchmarks.json"

MIN_SAMPLE = 3
_RATIO_COLS = ("gross_margin", "operating_margin", "net_margin", "roe", "roa",
               "debt_ratio", "current_ratio", "interest_coverage")


def _industry_map() -> dict[str, str]:
    try:
        df = fetch_stock_info()
    except Exception:
        return {}
    if df is None or df.empty or "industry_category" not in df.columns or "stock_id" not in df.columns:
        return {}
    return dict(zip(df["stock_id"], df["industry_category"]))


def _cached_stock_ids() -> list[str]:
    ids: set[str] = set()
    if FINANCIALS_DIR.exists():
        ids.update(p.stem for p in FINANCIALS_DIR.glob("*.parquet"))
    if BALANCE_DIR.exists():
        ids.update(p.stem for p in BALANCE_DIR.glob("*.parquet"))
    return sorted(ids)


def _row_for_stock(stock_id: str, industry: str) -> dict | None:
    fin = load_financials(stock_id)
    bal = load_balance(stock_id)
    if (fin is None or fin.empty) and (bal is None or bal.empty):
        return None
    ni = q.last(fin, "net_income"); eq = q.last(bal, "equity")
    ta = q.last(bal, "total_assets"); tl = q.last(bal, "total_liab")
    # 利息保障倍數用近12個月:營業利益(損益表單季滾動4季)÷ 利息費用(現金流量表YTD去累計滾動4季)。
    # 利息費用不在損益表,故需另讀現金流快取(與 financial_engine 同基期,同業均值才可比)。
    cf = load_cashflow(stock_id)
    oi_ttm = q.ttm(fin, "operating_income"); ie_ttm = q.ttm_flow(cf, "interest_expense")
    return {
        "stock_id": stock_id, "industry": industry,
        "gross_margin": q.ratio(fin, "gross_profit", "revenue", scale=100),
        "operating_margin": q.ratio(fin, "operating_income", "revenue", scale=100),
        "net_margin": q.ratio(fin, "net_income", "revenue", scale=100),
        "roe": (ni / eq * 100) if (ni is not None and eq) else None,
        "roa": (ni / ta * 100) if (ni is not None and ta) else None,
        "debt_ratio": (tl / ta * 100) if (tl is not None and ta) else None,
        "current_ratio": q.ratio(bal, "current_assets", "current_liab", scale=100),
        # 速動比(需扣存貨)同業彙總層級先略過,避免重算邏輯分岔;個股卡片仍由 financial_engine 自己算。
        "interest_coverage": (oi_ttm / abs(ie_ttm)) if (oi_ttm is not None and ie_ttm) else None,
    }


def build_benchmarks(min_count: int = MIN_SAMPLE) -> dict:
    """掃描本地已快取的所有股票,依產業彙總平均值。回傳 {industry: {sample_size, <ratio>: avg, ...}}。"""
    ind_map = _industry_map()
    if not ind_map:
        return {}
    rows = []
    for sid in _cached_stock_ids():
        industry = ind_map.get(sid)
        if not industry:
            continue
        row = _row_for_stock(sid, industry)
        if row:
            rows.append(row)
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    out: dict[str, dict] = {}
    for industry, g in df.groupby("industry"):
        n = len(g)
        if n < min_count:
            continue
        avgs: dict = {"sample_size": int(n)}
        for col in _RATIO_COLS:
            if col not in g.columns:
                continue
            s = g[col].dropna()
            if len(s) >= min_count:
                avgs[col] = round(float(s.mean()), 2)
        out[industry] = avgs
    return out


def save_benchmarks(benchmarks: dict, *, updated_at: str | None = None) -> dict:
    payload = {
        "updated_at": updated_at or date.today().isoformat(),
        "min_sample": MIN_SAMPLE,
        "industries": benchmarks,
    }
    with open(BENCHMARK_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def load_benchmarks() -> dict:
    if not BENCHMARK_PATH.exists():
        return {}
    try:
        with open(BENCHMARK_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def lookup(industry: str | None, benchmarks: dict | None = None) -> dict:
    """給各 Engine 的 ctx['industry_benchmarks'] 用:回傳該產業 {metric_key: avg},
    查無資料(產業未知或樣本不足)一律回 {},Metric 契約會自動把 industry_avg 顯示成 None。"""
    if not industry:
        return {}
    benchmarks = benchmarks if benchmarks is not None else load_benchmarks()
    rec = (benchmarks.get("industries") or {}).get(industry, {})
    return {k: v for k, v in rec.items() if k != "sample_size"}
