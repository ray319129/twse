from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path
from .config import PRICES_DIR, DATA_DIR, META_DIR

CHIPS_DIR = DATA_DIR / "chips"
REVENUE_DIR = DATA_DIR / "revenue"
EPS_DIR = DATA_DIR / "eps"
PER_DIR = DATA_DIR / "per"
FINANCIALS_DIR = DATA_DIR / "financials"
BALANCE_DIR = DATA_DIR / "balance"
CASHFLOW_DIR = DATA_DIR / "cashflow"
try:
    for _d in (CHIPS_DIR, REVENUE_DIR, EPS_DIR, PER_DIR, FINANCIALS_DIR, BALANCE_DIR, CASHFLOW_DIR):
        _d.mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # Vercel serverless: read-only filesystem


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.name != "date":
        if "date" in df.columns:
            df = df.set_index("date")
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.sort_index()


def _try_write_parquet(df: pd.DataFrame, path: Path) -> None:
    """寫入失敗(例如 Vercel serverless 唯讀檔案系統,個股健檢即時查詢路徑會踩到)
    只記錄、不拋例外 —— 呼叫端永遠拿得到記憶體內算好的 DataFrame,只是這次沒能落地快取。"""
    try:
        df.to_parquet(path)
    except Exception as e:
        import logging
        logging.getLogger("twse").warning(f"parquet 寫入失敗(唯讀檔案系統?略過快取):{path.name}: {e}")


def _upsert(path: Path, new_df: pd.DataFrame, loader) -> pd.DataFrame:
    if new_df.empty:
        return loader(path)
    cur = loader(path)
    new_df = _normalize_index(new_df.copy())
    if cur.empty:
        _try_write_parquet(new_df, path)
        return new_df
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    _try_write_parquet(combined, path)
    return combined


def _load_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return _normalize_index(pd.read_parquet(path))


# Prices
#
# ─── 儲存分層:base(每檔一份,凍結) + tail(每月一份,全市場) ─────────────────────
#
# 為什麼要分層:原本每檔一個 parquet、每天各補一根 K 棒,等於**每個交易日重寫 1,888 個檔**。
# parquet 是 binary,git 無法 delta 壓縮 → 單一「daily update」commit 就產生 **56 MB** 新物件,
# 約 1.1 GB/月。實測 .git 已 329 MB、每天長 46 MB,照這速度兩週破 1 GB、3.5 個月破 5 GB,
# 免費自動化會直接撞牆(且愈晚修、要重寫的歷史愈大)。
#
# 分層後:
#   base  `data/prices/{sid}.parquet`        —— 既有檔案原封不動,之後**不再逐日重寫**
#   tail  `data/prices/tail/YYYY-MM.parquet` —— 當月新增的 K 棒,全市場共一份(約 1~2 MB)
# 每天只重寫「當月那一份 tail」,日增量從 56 MB 降到 1~2 MB。
#
# 刻意**不做資料遷移**:base 檔一行都不動,所以沒有「搬一半掛掉就毀了 5 年歷史」的風險。
# tail 不存在時 load_prices 的行為與改版前完全一致。
#
# 寫入是**緩衝式**的:upsert_prices 只更新記憶體,月檔在 flush_prices() 時才落地
# —— 否則一輪 1,900 次 upsert 會把同一個 tail 檔重寫 1,900 次,比原本更慢。
# daily_run 結尾會顯式呼叫,另有 atexit 保險。

TAIL_DIR = PRICES_DIR / "tail"

_TAIL: dict[str, pd.DataFrame] | None = None   # stock_id -> 該檔的 tail 列(date 為索引)
_TAIL_DIRTY: set[str] = set()                  # 待寫回的月份 key(YYYY-MM)


def price_path(stock_id: str) -> Path:
    return PRICES_DIR / f"{stock_id}.parquet"


def _tail_path(month: str) -> Path:
    return TAIL_DIR / f"{month}.parquet"


def _load_tail() -> dict[str, pd.DataFrame]:
    """把所有 tail 月檔讀成 {stock_id: DataFrame}。只做一次,之後走記憶體。"""
    global _TAIL
    if _TAIL is not None:
        return _TAIL
    frames = []
    try:
        paths = sorted(TAIL_DIR.glob("*.parquet"))
    except OSError:
        paths = []
    for p in paths:
        try:
            frames.append(_normalize_index(pd.read_parquet(p)))
        except Exception as e:
            import logging
            logging.getLogger("twse").warning(f"tail 讀取失敗(略過):{p.name}: {e}")
    if frames:
        allt = pd.concat(frames)
        allt["stock_id"] = allt["stock_id"].astype(str)
        _TAIL = {sid: g.drop(columns=["stock_id"]).sort_index()
                 for sid, g in allt.groupby("stock_id", sort=False)}
    else:
        _TAIL = {}
    return _TAIL


def _merge_base_tail(base: pd.DataFrame, tail: pd.DataFrame | None) -> pd.DataFrame:
    """tail 疊在 base 之上(同一天以 tail 為準 —— 它比較新)。"""
    if tail is None or tail.empty:
        return base
    if base is None or base.empty:
        return tail.sort_index()
    combined = pd.concat([base, tail])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def _row_differs(base: pd.DataFrame, inc: pd.DataFrame, d) -> bool:
    """該日期在 base 中不存在,或存在但數值與增量不同 → True(需要寫進 tail)。
    只比兩邊共有的欄位;NaN 視為相等(避免 NaN != NaN 讓每天都判定成有異動)。"""
    if d not in base.index:
        return True
    cols = [c for c in inc.columns if c in base.columns]
    if not cols:
        return True
    a, b = base.loc[d, cols], inc.loc[d, cols]
    if isinstance(a, pd.DataFrame):      # 理論上不該有重複索引,保險起見
        return True
    for c in cols:
        x, y = a[c], b[c]
        if pd.isna(x) and pd.isna(y):
            continue
        if pd.isna(x) or pd.isna(y):
            return True
        try:
            if not np.isclose(float(x), float(y), rtol=1e-9, atol=1e-9):
                return True
        except (TypeError, ValueError):
            if x != y:
                return True
    return False


def load_prices(stock_id: str) -> pd.DataFrame:
    df = _merge_base_tail(_load_parquet(price_path(stock_id)), _load_tail().get(str(stock_id)))
    # 安全網:忽略收盤為 NaN 的壞 K 棒(yfinance 偶爾寫入未定收盤),否則均線/評分全毀
    if not df.empty and "close" in df.columns:
        df = df[df["close"].notna()]
    return df


def save_prices(stock_id: str, df: pd.DataFrame) -> None:
    """整段覆蓋 base(減資/分割重抓走這條)。既然 base 已是完整正確的序列,
    就要把該檔殘留的 tail 清掉,否則舊尺度的 tail 會疊回來、把剛修好的序列again弄壞。"""
    if df.empty:
        return
    out = _normalize_index(df.copy())
    _try_write_parquet(out, price_path(stock_id))
    sid = str(stock_id)
    tail = _load_tail()
    if sid in tail:
        for m in sorted({d.strftime("%Y-%m") for d in tail[sid].index}):
            _TAIL_DIRTY.add(m)
        del tail[sid]


def upsert_prices(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    """新增/更新 K 棒 → 只進 tail 緩衝(不碰 base)。回傳合併後的完整序列,與改版前語意相同。"""
    sid = str(stock_id)
    base = _load_parquet(price_path(sid))
    tail_map = _load_tail()
    if new_df is None or new_df.empty:
        return _merge_base_tail(base, tail_map.get(sid))

    inc = _normalize_index(new_df.copy())
    if "stock_id" in inc.columns:
        inc = inc.drop(columns=["stock_id"])
    # 只有「base 沒有」或「base 有但值不同」的列才進 tail。
    # ⚠️ 不能只用 `index.isin(base.index)` 排除 —— 原本 _upsert 是 keep="last",
    #    新抓的資料會**覆蓋**既有日期(yfinance 會事後修正已發布的 K 棒,例如未定收盤補上)。
    #    只看日期就整列丟掉,等於默默放棄修正。改成比對內容:相同才跳過。
    if not base.empty:
        inc = inc[[_row_differs(base, inc, d) for d in inc.index]]
    if inc.empty:
        return _merge_base_tail(base, tail_map.get(sid))

    cur = tail_map.get(sid)
    merged = inc if (cur is None or cur.empty) else \
        pd.concat([cur, inc])[lambda d: ~d.index.duplicated(keep="last")].sort_index()
    tail_map[sid] = merged
    for d in inc.index:
        _TAIL_DIRTY.add(d.strftime("%Y-%m"))
    return _merge_base_tail(base, merged)


def flush_prices() -> int:
    """把緩衝中的 tail 依月份落地。回傳寫出的檔案數。"""
    if not _TAIL_DIRTY:
        return 0
    tail_map = _load_tail()
    try:
        TAIL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    months = sorted(_TAIL_DIRTY)
    written = 0
    for m in months:
        rows = []
        for sid, df in tail_map.items():
            sub = df[[d.strftime("%Y-%m") == m for d in df.index]]
            if not sub.empty:
                sub = sub.copy()
                sub["stock_id"] = sid
                rows.append(sub)
        p = _tail_path(m)
        if not rows:
            # 該月已無資料(例如整檔被 save_prices 收編回 base)→ 刪掉空月檔
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            continue
        _try_write_parquet(pd.concat(rows).sort_index(), p)
        written += 1
    _TAIL_DIRTY.clear()
    return written


import atexit as _atexit
_atexit.register(lambda: flush_prices())   # 保險:呼叫端忘了 flush 也不會掉資料


def compact_prices() -> dict:
    """把所有 tail 併回 base,並清空 tail(定期維護,建議半年~一年跑一次)。

    這一次會重寫全部 base 檔(約 56 MB 的 commit),但之後 tail 重新從 0 開始長。
    不跑也不會壞,只是 tail 月檔愈積愈多、每次讀取要多合併幾份。
    跑法:`python -m scripts.storage --compact`
    """
    tail = _load_tail()
    n = 0
    for sid, rows in list(tail.items()):
        if rows is None or rows.empty:
            continue
        base = _load_parquet(price_path(sid))
        _try_write_parquet(_merge_base_tail(base, rows), price_path(sid))
        n += 1
    removed = 0
    try:
        for p in TAIL_DIR.glob("*.parquet"):
            p.unlink(); removed += 1
        TAIL_DIR.rmdir()
    except OSError:
        pass
    global _TAIL
    _TAIL = {}
    _TAIL_DIRTY.clear()
    return {"compacted": n, "tail_files_removed": removed}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="價格儲存維護")
    ap.add_argument("--compact", action="store_true", help="把 tail 月檔併回 base 並清空 tail")
    a = ap.parse_args()
    if a.compact:
        print(compact_prices())
    else:
        ap.print_help()


# Index cache (大盤 ^TWII)
#
# 回測層(backtest.py / score_validate.py)只讀這份快取當基準與相對強度來源,
# 但在此之前沒有任何流程寫它 —— 它會停在最後一次手動補史的日期。
# 每日流程抓完大盤就 upsert 一次,研究層才不會拿舊基準算超額。

INDEX_CACHE_PATH = META_DIR / "twii.parquet"


def load_index_cache() -> pd.DataFrame:
    return _load_parquet(INDEX_CACHE_PATH)


def upsert_index_cache(new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(INDEX_CACHE_PATH, new_df, _load_parquet)


def prices_scale_shift(cur: pd.DataFrame, new_df: pd.DataFrame, threshold: float = 0.03) -> bool:
    """偵測增量價格與既有快取在『重疊日收盤』的尺度偏移。

    yfinance 遇股票分割/減資會回溯調整整條序列,但每日只抓最近 10 天增量 → 快取是舊尺度、
    增量是新尺度,直接 concat 合併後均線/動能/一切指標全毀,且不會自我修復(除非快取被刪)。
    台股減資不罕見,除權息旺季尤甚。重疊日收盤差異 > threshold(預設 3%)即視為偏移,
    呼叫端應整段重抓覆蓋,而非增量合併。

    只抓分割/減資,不會被除息誤觸發:auto_adjust=False 的原始收盤不因『配息』回溯調整
    (除息只造成序列內的自然跳空,是另一個坑,需雙軌 adj_close 解,不在本偵測範圍)。"""
    if cur is None or new_df is None or cur.empty or new_df.empty:
        return False
    if "close" not in cur.columns or "close" not in new_df.columns:
        return False
    a = _normalize_index(cur.copy())
    b = _normalize_index(new_df.copy())
    overlap = a.index.intersection(b.index)
    if len(overlap) == 0:
        return False
    denom = a.loc[overlap, "close"]
    ratio = (b.loc[overlap, "close"] / denom.where(denom != 0)).dropna()
    if ratio.empty:
        return False
    return bool(((ratio - 1).abs() > threshold).any())


# Chips (institutional + margin + foreign holding history per stock)

def chips_path(stock_id: str) -> Path:
    return CHIPS_DIR / f"{stock_id}.parquet"


def load_chips(stock_id: str) -> pd.DataFrame:
    return _load_parquet(chips_path(stock_id))


def upsert_chips(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    """籌碼合併採 combine_first 語意:新值優先,但「新資料的 NaN 不覆蓋舊有值」。
    三大法人(~16:00)/融資券(~21:00)/外資持股(隔日)出表時間不同,當天 16:30 跑時後兩者是 NaN。
    若用一般 concat + duplicated(keep="last"),隔天重疊回補時新的那格若仍缺,會把先前抓到的值蓋成 NaN,
    造成永久缺洞。combine_first 讓「有值優先、缺值退回舊值」,配合 _update_chips 的 last-4d 重疊回補補回缺格。"""
    if new_df.empty:
        return load_chips(stock_id)
    cur = load_chips(stock_id)
    new_df = _normalize_index(new_df.copy())
    if cur.empty:
        _try_write_parquet(new_df, chips_path(stock_id))
        return new_df
    # combine_first:對齊 index/columns 的聯集,每格取 new_df 的非 NaN 值,否則退回 cur。
    combined = new_df.combine_first(cur).sort_index()
    _try_write_parquet(combined, chips_path(stock_id))
    return combined


# Monthly revenue (index = "YYYY-MM" string)

def revenue_path(stock_id: str) -> Path:
    return REVENUE_DIR / f"{stock_id}.parquet"


def load_revenue(stock_id: str) -> pd.DataFrame:
    p = revenue_path(stock_id)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    if df.index.name != "ym" and "ym" in df.columns:
        df = df.set_index("ym")
    return df.sort_index()


def upsert_revenue(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    if new_df.empty:
        return load_revenue(stock_id)
    cur = load_revenue(stock_id)
    new_df = new_df.copy()
    if cur.empty:
        _try_write_parquet(new_df, revenue_path(stock_id))
        return new_df
    combined = pd.concat([cur, new_df])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    _try_write_parquet(combined, revenue_path(stock_id))
    return combined


# Quarterly EPS

def eps_path(stock_id: str) -> Path:
    return EPS_DIR / f"{stock_id}.parquet"


def load_eps(stock_id: str) -> pd.DataFrame:
    return _load_parquet(eps_path(stock_id))


def upsert_eps(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(eps_path(stock_id), new_df, _load_parquet)


# PER / dividend yield / PB

def per_path(stock_id: str) -> Path:
    return PER_DIR / f"{stock_id}.parquet"


def load_per(stock_id: str) -> pd.DataFrame:
    return _load_parquet(per_path(stock_id))


def upsert_per(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(per_path(stock_id), new_df, _load_parquet)


# Quarterly fundamentals: financial statements / balance sheet / cash flow (index = quarter-end date)

def financials_path(stock_id: str) -> Path:
    return FINANCIALS_DIR / f"{stock_id}.parquet"


def load_financials(stock_id: str) -> pd.DataFrame:
    return _load_parquet(financials_path(stock_id))


def upsert_financials(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(financials_path(stock_id), new_df, _load_parquet)


def balance_path(stock_id: str) -> Path:
    return BALANCE_DIR / f"{stock_id}.parquet"


def load_balance(stock_id: str) -> pd.DataFrame:
    return _load_parquet(balance_path(stock_id))


def upsert_balance(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(balance_path(stock_id), new_df, _load_parquet)


def cashflow_path(stock_id: str) -> Path:
    return CASHFLOW_DIR / f"{stock_id}.parquet"


def load_cashflow(stock_id: str) -> pd.DataFrame:
    return _load_parquet(cashflow_path(stock_id))


def upsert_cashflow(stock_id: str, new_df: pd.DataFrame) -> pd.DataFrame:
    return _upsert(cashflow_path(stock_id), new_df, _load_parquet)
