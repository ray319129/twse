"""量 TWSE MIS 的真實盤中延遲 —— 決定盯盤輪詢間隔該設多少的唯一依據。

## 為什麼一定要量

當沖層第 2 層(價位穿越推播)的價值完全取決於延遲:
若 MIS 落後 >60 秒,「剛穿過 105.2」這種訊號到手時價格早就跑掉,那一層就該降級
成純記錄,重心移到標的池 / 風控 / 台帳(那三層不受延遲影響)。

**這件事不能用猜的**,而且只有交易時段量得到 —— 盤後 MIS 回的是上一個交易日的收盤,
`t` 永遠是 13:30:00,量出來的「延遲」是幾小時,毫無意義。

## 量什麼

MIS 每檔回傳:
  `t`        最後一筆成交的時間(HH:MM:SS)
  `tlong`    同上,epoch 毫秒
  `queryTime.sysTime`  交易所主機的當下時間

  **資料落後 = sysTime − t** —— 這是直接量測,不是推估。

⚠️ 但 `t` 是「最後成交時間」,冷門股本來就可能好幾分鐘沒成交,那不是系統延遲。
所以一律取**多檔的中位數**,並且只用流動性高的標的(當沖池本來就是),
同時回報分布讓人看得出來是系統延遲還是個股沒量。

另外量 `tlong` 多久變一次 → 上游實際的更新頻率。輪詢比它密沒有意義(拿到同一份資料)。
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import date, datetime
from pathlib import Path

import requests

from ..config import DATA_DIR, now_tpe
from ..utils import log

MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_INDEX = "https://mis.twse.com.tw/stock/index.jsp"
DOCS_DIR = Path(DATA_DIR).parent / "docs"
OUT_DIR = DATA_DIR / "daytrade"


def _channel(sid: str, market: str) -> str:
    return ("otc_" if market == "tpex" else "tse_") + f"{sid}.tw"


def _hhmmss_to_sec(t: str) -> int | None:
    try:
        h, m, s = (int(x) for x in str(t).split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def probe(symbols: list[tuple[str, str]], session: requests.Session) -> dict | None:
    """打一次 MIS,回傳這一輪的觀測。失敗回 None。"""
    ex_ch = "|".join(_channel(s, m) for s, m in symbols[:50])
    try:
        r = session.get(MIS_API, params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                                         "_": str(int(time.time() * 1000))}, timeout=20)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        log.warning(f"MIS 延遲量測:抓取失敗 {e}")
        return None

    sys_time = ((j.get("queryTime") or {}).get("sysTime") or "").strip()
    sys_sec = _hhmmss_to_sec(sys_time)
    rows = []
    for it in (j.get("msgArray") or []):
        t = (it.get("t") or "").strip()
        t_sec = _hhmmss_to_sec(t)
        if t_sec is None or sys_sec is None:
            continue
        rows.append({
            "stock_id": str(it.get("c") or ""),
            "t": t,
            "tlong": it.get("tlong"),
            "price": it.get("z"),
            "acc_vol": it.get("v"),
            "lag_s": sys_sec - t_sec,
        })
    if not rows:
        return None
    return {"sys_time": sys_time, "rows": rows,
            "wall": now_tpe().strftime("%H:%M:%S")}


def measure(symbols: list[tuple[str, str]], *, seconds: int = 120,
            interval: float = 3.0) -> dict:
    """量 `seconds` 秒。回傳可直接寫檔的結果。"""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": MIS_INDEX})
    try:
        session.get(MIS_INDEX, timeout=15)      # 先取 cookie,MIS 常擋無 cookie 的請求
    except Exception:
        pass

    samples: list[dict] = []
    # {stock_id: [(wall_epoch, tlong)]} —— 用來算上游多久換一次資料
    changes: dict[str, list[float]] = {}
    last_tlong: dict[str, object] = {}

    t_end = time.time() + seconds
    while time.time() < t_end:
        t0 = time.time()
        s = probe(symbols, session)
        if s:
            samples.append(s)
            for row in s["rows"]:
                sid, tl = row["stock_id"], row["tlong"]
                if sid and tl and last_tlong.get(sid) != tl:
                    if sid in last_tlong:           # 第一次只記基準
                        changes.setdefault(sid, []).append(t0)
                    last_tlong[sid] = tl
        time.sleep(max(0.0, interval - (time.time() - t0)))

    all_lags = [r["lag_s"] for s in samples for r in s["rows"]]
    # 每檔各自的中位延遲 —— 用來分辨「系統延遲」與「這檔沒量」
    per_stock: dict[str, list[int]] = {}
    for s in samples:
        for r in s["rows"]:
            per_stock.setdefault(r["stock_id"], []).append(r["lag_s"])
    per_med = {k: statistics.median(v) for k, v in per_stock.items() if v}

    gaps: list[float] = []
    for sid, ts in changes.items():
        gaps.extend(round(b - a, 1) for a, b in zip(ts, ts[1:]))

    def q(vals, p):
        if not vals:
            return None
        v = sorted(vals)
        return round(v[min(int(len(v) * p), len(v) - 1)], 1)

    result = {
        "measured_at": now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": seconds, "poll_interval": interval,
        "polls": len(samples), "symbols": len(symbols),
        "lag_seconds": {
            "note": ("資料落後 = 交易所 sysTime − 該檔最後成交時間。"
                     "冷門股沒成交也會拉高,所以看中位數與 P25 較準。"),
            "p25": q(all_lags, 0.25), "median": q(all_lags, 0.50),
            "p75": q(all_lags, 0.75), "p90": q(all_lags, 0.90),
            "min": min(all_lags) if all_lags else None,
            "samples": len(all_lags),
        },
        "best_stock_median_lag_s": (min(per_med.values()) if per_med else None),
        "upstream_update_gap_s": {
            "note": "同一檔 tlong 改變的間隔 = 上游實際更新頻率。輪詢比它密只會拿到同一份資料。",
            "median": (round(statistics.median(gaps), 1) if gaps else None),
            "p90": q(gaps, 0.90), "samples": len(gaps),
        },
    }
    result["verdict"] = _verdict(result)
    return result


def _verdict(r: dict) -> dict:
    """把數字翻成「第 2 層(價位穿越)還值不值得做」的結論。"""
    med = (r.get("lag_seconds") or {}).get("median")
    best = r.get("best_stock_median_lag_s")
    ref = best if best is not None else med
    if ref is None:
        return {"level": "unknown", "text": "量不到有效樣本(可能非交易時段或 MIS 被擋)。"}
    if ref <= 15:
        return {"level": "good",
                "text": f"資料落後約 {ref:.0f} 秒 —— 價位穿越推播可用,"
                        f"建議輪詢 {max(10, int(ref)):d}~30 秒。"}
    if ref <= 60:
        return {"level": "marginal",
                "text": f"資料落後約 {ref:.0f} 秒 —— 勉強可用,但只適合「水位穿越」"
                        f"這種不追價的訊號,不要拿來追動能。"}
    return {"level": "poor",
            "text": f"資料落後約 {ref:.0f} 秒 —— **第 2 層價值有限**,"
                    f"建議把價位穿越降級成純記錄,重心放在標的池/風控/台帳"
                    f"(那三層不受延遲影響)。"}


def save(result: dict, day: date | None = None) -> Path:
    day = day or now_tpe().date()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"latency-{day.isoformat()}.json"
    p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "daytrade_latency.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"docs/daytrade_latency.json 寫檔失敗:{e}")
    return p


def measure_from_pool(seconds: int = 120, interval: float = 3.0) -> dict:
    """用今日當沖標的池當樣本(它們本來就是高流動性的,最適合量系統延遲)。"""
    from .universe import scan_ids
    from ..fetchers import fetch_stock_info
    today = now_tpe().date()
    pp = OUT_DIR / f"pool-{today.isoformat()}.json"
    ids: list[str] = []
    if pp.exists():
        try:
            ids = scan_ids(json.loads(pp.read_text(encoding="utf-8")))
        except Exception:
            ids = []
    if not ids:
        ids = ["2330", "2317", "2454", "2603", "3231", "2382"]   # 退而求其次:大型權值
    try:
        info = fetch_stock_info()
        mk = dict(zip(info["stock_id"].astype(str), info["type"].astype(str)))
    except Exception:
        mk = {}
    symbols = [(s, mk.get(s, "twse")) for s in ids[:40]]
    r = measure(symbols, seconds=seconds, interval=interval)
    save(r)
    log.info(f"MIS 延遲量測:中位 {r['lag_seconds']['median']}s、"
             f"最佳檔 {r['best_stock_median_lag_s']}s、"
             f"上游更新間隔中位 {r['upstream_update_gap_s']['median']}s → "
             f"{r['verdict']['text']}")
    return r


if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="量 MIS 盤中真實延遲(只有交易時段有意義)")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--interval", type=float, default=3.0)
    a = ap.parse_args()
    from ..quotes import in_trading_session
    if not in_trading_session():
        print(json.dumps({"skipped": "非交易時段(09:00~13:30 之外),量出來的延遲沒有意義"},
                         ensure_ascii=False))
        raise SystemExit(0)
    print(json.dumps(measure_from_pool(a.seconds, a.interval), ensure_ascii=False, indent=1))
