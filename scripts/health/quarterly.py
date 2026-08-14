"""季資料存取小工具 — financial/growth/risk engine 共用,避免三個模組各自重寫
「拿最新一筆/拿N期前/算YoY/算趨勢序列」這幾個一定會重複的小函式。

所有函式對「資料不足」一律回 None / [],不丟例外 — 健檢模組的核心原則是
「缺資料就誠實標缺,不要讓整個 Engine 當掉」。
"""
from __future__ import annotations
import pandas as pd

from .metric import trend_point


def _period_label(idx) -> str:
    try:
        ts = pd.Timestamp(idx)
        q = (ts.month - 1) // 3 + 1
        return f"{ts.year}Q{q}"
    except Exception:
        return str(idx)


def last(df: pd.DataFrame | None, col: str) -> float | None:
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    return float(s.iloc[-1]) if not s.empty else None


def last_period(df: pd.DataFrame | None, col: str) -> str | None:
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    return _period_label(s.index[-1]) if not s.empty else None


def at(df: pd.DataFrame | None, col: str, offset: int) -> float | None:
    """offset=1→上一期,offset=4→4期前(季資料=去年同季)。不足回 None。"""
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    if len(s) <= offset:
        return None
    return float(s.iloc[-1 - offset])


def yoy(df: pd.DataFrame | None, col: str) -> float | None:
    """去年同季 ≤ 0 時回 None —— 除以「負基期的絕對值」算出來的不是成長率。
    (2026Q2 實例:旺宏去年同季營業利益 −10.79 億、今年 +89.68 億,舊寫法得 +931.1%;
     華邦電 EPS −0.29 → +5.40 得 +1962.1%。那是「由虧轉盈」,不是成長 931%/1962%,
     而且會一路污染 PEG 與成長分。)要區分是哪一種缺法時用 yoy_basis()。"""
    cur, prev = last(df, col), at(df, col, 4)
    if cur is None or prev is None or prev <= 0:
        return None
    return (cur - prev) / prev


def yoy_basis(df: pd.DataFrame | None, col: str) -> str | None:
    """yoy() 回 None 的原因分類,給 Engine 標成人看得懂的說明:
    'no_base' 沒有去年同季資料 / 'turnaround' 去年虧今年賺 / 'still_negative' 兩期都虧。
    回 None 代表基期正常、yoy() 算得出來。"""
    cur, prev = last(df, col), at(df, col, 4)
    if cur is None or prev is None:
        return "no_base"
    if prev > 0:
        return None
    return "turnaround" if cur > 0 else "still_negative"


def qoq(df: pd.DataFrame | None, col: str) -> float | None:
    cur, prev = last(df, col), at(df, col, 1)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def trend(df: pd.DataFrame | None, col: str, periods: int = 8) -> list[dict]:
    if df is None or df.empty or col not in df.columns:
        return []
    s = df[col].dropna().tail(periods)
    return [trend_point(_period_label(idx), v) for idx, v in s.items()]


def ratio(df: pd.DataFrame | None, num_col: str, den_col: str, *, offset: int = 0, scale: float = 1.0) -> float | None:
    """同一期 num_col/den_col 的比值(如毛利率=gross_profit/revenue)。offset 同 at()。"""
    if df is None or df.empty or num_col not in df.columns or den_col not in df.columns:
        return None
    sub = df[[num_col, den_col]].dropna()
    if len(sub) <= offset:
        return None
    row = sub.iloc[-1 - offset]
    d = row[den_col]
    if d == 0 or pd.isna(d):
        return None
    return float(row[num_col]) / float(d) * scale


def ratio_trend(df: pd.DataFrame | None, num_col: str, den_col: str, *, periods: int = 8, scale: float = 1.0) -> list[dict]:
    if df is None or df.empty or num_col not in df.columns or den_col not in df.columns:
        return []
    sub = df[[num_col, den_col]].dropna().tail(periods)
    out = []
    for idx, row in sub.iterrows():
        d = row[den_col]
        v = (float(row[num_col]) / float(d) * scale) if d != 0 and not pd.isna(d) else None
        out.append(trend_point(_period_label(idx), v))
    return out


def cagr(df: pd.DataFrame | None, col: str, years: int = 5) -> float | None:
    """近 years 年 CAGR(季資料,首尾各一筆;首/尾任一 <=0 視為不可比,回 None)。"""
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    n_q = years * 4
    if len(s) <= n_q:
        return None
    start, end = float(s.iloc[-1 - n_q]), float(s.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def ttm(df: pd.DataFrame | None, col: str, *, offset: int = 0) -> float | None:
    """近12個月(trailing twelve months)= 最近連續 4 個「單季」值相加。
    適用損益表(FinMind 的 TaiwanStockFinancialStatements 本來就是單季值)。offset 同 at()。
    不足 4 季回 None。"""
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    end = len(s) - offset
    if end < 4:
        return None
    return float(s.iloc[end - 4:end].sum())


def _to_single_quarter(s: pd.Series) -> pd.Series:
    """把「當年度累計(YTD)」序列還原成單季:Q1 維持原值,Q2~Q4 = 本期累計 − 同年上一季累計。
    僅在同一年、且上一季存在時才相減;缺季或跨年首季維持累計值(即該年 Q1)。現金流量表專用。"""
    if s is None or s.empty:
        return s
    s = s.dropna().sort_index()
    out = {}
    prev_val = None
    prev_q = None
    prev_year = None
    for idx, val in s.items():
        ts = pd.Timestamp(idx)
        qtr = (ts.month - 1) // 3 + 1
        if qtr == 1 or prev_val is None or prev_year != ts.year or prev_q != qtr - 1:
            out[idx] = float(val)          # 該年首季 / 無法對齊上一季 → 視為單季起點
        else:
            out[idx] = float(val) - float(prev_val)
        prev_val, prev_q, prev_year = val, qtr, ts.year
    return pd.Series(out).sort_index()


def ttm_flow(df: pd.DataFrame | None, col: str, *, offset: int = 0) -> float | None:
    """現金流量表(YTD 累計)專用的 TTM:先去累計成單季,再取最近 4 季相加。不足 4 季回 None。"""
    if df is None or df.empty or col not in df.columns:
        return None
    sq = _to_single_quarter(df[col])
    end = len(sq) - offset
    if end < 4:
        return None
    return float(sq.iloc[end - 4:end].sum())


def nonoperating_dominant(df: pd.DataFrame | None) -> dict:
    """判斷「本季淨利是不是主要來自業外」。financial/growth/value 三個 Engine 共用同一份判定,
    避免各自寫一套門檻而互相矛盾。回傳 {hit, kind, nonop, nonop_ratio, gross_margin, net_margin}。

    為什麼需要這條:2026Q2 聯電(2303)營收 687.3 億、營業利益 149.5 億、淨利 422.2 億 —— 業外
    +272.7 億。舊版把「淨利率 61.43%」當獲利能力算進財務體質(96.1 分),又用含這筆一次性的
    TTM EPS 推出 PE 18.72 / EPS年增率 377.5% / PEG 0.05,一路變成「便宜又高成長」。
    一次性業外不具延續性,不該用來評估體質與估值。

    三種命中方式(任一即 hit):
      structural  淨利率 > 毛利率 —— 本業結構上不可能達成,差額必然來自業外
      ratio       |淨利 − 營業利益| ≥ 營業利益 —— 業外規模已不小於本業
      loss_cover  營業利益 ≤ 0 但淨利 > 0 —— 本業虧損,獲利全靠業外撐
    """
    out = {"hit": False, "kind": None, "nonop": None, "nonop_ratio": None,
           "gross_margin": None, "net_margin": None}
    ni = last(df, "net_income"); oi = last(df, "operating_income")
    gm = ratio(df, "gross_profit", "revenue", scale=100.0)
    nm = ratio(df, "net_income", "revenue", scale=100.0)
    out["gross_margin"], out["net_margin"] = gm, nm
    if ni is None or oi is None:
        return out
    out["nonop"] = ni - oi
    if oi > 0:
        out["nonop_ratio"] = abs(ni - oi) / oi

    if gm is not None and nm is not None and nm > gm:
        out.update(hit=True, kind="structural")
    elif oi <= 0 and ni > 0:
        out.update(hit=True, kind="loss_cover")
    elif out["nonop_ratio"] is not None and out["nonop_ratio"] >= 1.0:
        out.update(hit=True, kind="ratio")
    return out


def flow_at(df: pd.DataFrame | None, col: str, offset: int = 0) -> float | None:
    """現金流量表(YTD 累計)的「單季」值。offset=0→最新季,offset=1→上一季。

    直接對現金流量表用 last()/at() 會拿到 YTD 累計數,再互相比較就會得到假趨勢:
    Q1(3個月)→Q2(6個月)→Q3(9個月)→Q4(12個月)本來就一路變大,Q4→Q1 又必然變小。
    (2026Q2 實例:四檔的「營業現金流」全部標 ↑,純粹是 H1 累計 > Q1 累計造成的。)"""
    if df is None or df.empty or col not in df.columns:
        return None
    sq = _to_single_quarter(df[col])
    if len(sq) <= offset:
        return None
    return float(sq.iloc[-1 - offset])


def flow_trend(df: pd.DataFrame | None, col: str, periods: int = 8) -> list[dict]:
    """現金流量表的單季趨勢序列(先去累計再取近 N 季),避免把 3M/6M/9M/12M 累計值並排。"""
    if df is None or df.empty or col not in df.columns:
        return []
    sq = _to_single_quarter(df[col]).tail(periods)
    return [trend_point(_period_label(idx), v) for idx, v in sq.items()]


def consecutive(df: pd.DataFrame | None, col: str, *, negative: bool = True, max_check: int = 12) -> int:
    """近幾期連續為負(negative=True)或連續為正(negative=False)的期數,從最新一期往回數。"""
    if df is None or df.empty or col not in df.columns:
        return 0
    s = df[col].dropna().tail(max_check)
    if s.empty:
        return 0
    count = 0
    for v in reversed(s.tolist()):
        hit = (v < 0) if negative else (v > 0)
        if hit:
            count += 1
        else:
            break
    return count
