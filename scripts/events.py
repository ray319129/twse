"""選股當晚事件行事曆 — 確定性(公式推算 + 固定清單),不爬任何即時網頁。

痛點:選股當晚下的單會留倉到隔天/隔幾天,若剛好撞上 FOMC/CPI/非農/台積法說/台指結算,
盤中波動與開盤跳空風險陡升,卻沒有任何提示。本模組在信裡預告『未來幾個交易視窗內』的重大事件。

兩類來源合併:
  A. 公式推算(永不過期、零維護):
     - 台指期結算日 = 每月第三個星期三(台灣期交所規則)
     - 美國非農就業 NFP = 每月第一個星期五(美東)
  B. 固定行事曆(config/events.yaml,官方年初公布、無公式可推,一年補一次):
     - FOMC 利率決議 / 美國 CPI 發布 / 台積電法說會

對『未來 horizon_days 個日曆天』內的事件,標記距今天數/星期/影響級別/做多提示。
若行事曆(B)已用罄(名單裡不存在任何 >= 今天 的未來項)→ 附一則「需補下年度」的到期提醒。

全部純函式(可離線餵合成資料驗證),不打任何網路。
"""
from __future__ import annotations
import calendar as _cal
from datetime import date, datetime, timedelta

import yaml

from .config import CONFIG_DIR


# 每種事件的預設中繼資料。yaml 只需填 date + type,title/impact 走這裡預設(可在 yaml 覆寫)。
# impact: high 會觸發整體風控提示(caution);med 只列出、不升警。
_TYPE_META: dict[str, dict] = {
    "fomc": {"impact": "high", "label": "FOMC 利率決議", "region": "US",
             "note": "美國利率決議 + 主席談話,盤中易劇烈波動,留意當晚台指夜盤跳空。"},
    "cpi": {"impact": "high", "label": "美國 CPI", "region": "US",
            "note": "通膨數據(美東 08:30 公布),美股與台股開盤常大幅跳動。"},
    "nfp": {"impact": "med", "label": "美國非農就業", "region": "US",
            "note": "非農就業(每月第一個週五),影響 Fed 路徑預期;週末發酵、下週一台股易補反應。"},
    "settlement": {"impact": "med", "label": "台指期結算", "region": "TW",
                   "note": "台指期/選擇權結算(每月第三個週三),尾盤指數易被期現套利拉抬或摜壓,權值股波動放大。"},
    "tsmc": {"impact": "high", "label": "台積電法說會", "region": "TW",
             "note": "台積電法說(佔大盤約三成),為電子權值股後市方向指標,前後易先卡位觀望。"},
}

_WD_TW = ["一", "二", "三", "四", "五", "六", "日"]   # 0=Mon..6=Sun → 中文星期


def _as_date(d) -> date:
    """接受 date / datetime / 'YYYY-MM-DD' 字串,一律轉成 date。"""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """該月第 n 個指定星期幾的日期(weekday: 0=週一..6=週日,n 從 1 起)。"""
    first_wd, days_in_month = _cal.monthrange(year, month)   # first_wd: 該月1號星期幾(0=Mon)
    offset = (weekday - first_wd) % 7                         # 到當月第一個該星期幾的天數位移
    day = 1 + offset + (n - 1) * 7
    if day > days_in_month:
        raise ValueError(f"{year}-{month} 沒有第 {n} 個星期{_WD_TW[weekday]}")
    return date(year, month, day)


def settlement_day(year: int, month: int) -> date:
    """台指期結算日 = 每月第三個星期三。"""
    return _nth_weekday(year, month, 2, 3)   # 週三=2


def nfp_day(year: int, month: int) -> date:
    """美國非農就業公布日 = 每月第一個星期五。"""
    return _nth_weekday(year, month, 4, 1)   # 週五=4


def _iter_months(start: date, end: date):
    """走訪 [start, end] 觸及的每個 (year, month)(含頭尾月份)。"""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _formula_events(start: date, end: date) -> list[dict]:
    """公式事件(台指結算 + 非農),落在 [start, end] 內的。"""
    out: list[dict] = []
    for y, m in _iter_months(start, end):
        for typ, fn in (("settlement", settlement_day), ("nfp", nfp_day)):
            d = fn(y, m)
            if start <= d <= end:
                out.append({"date": d, "type": typ})
    return out


def load_events_config(path=None) -> list[dict]:
    """讀 config/events.yaml 的固定行事曆(B 類)。缺檔/格式異常 → 空清單(不報錯)。"""
    p = path or (CONFIG_DIR / "events.yaml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return []
    except Exception:
        return []
    items = data.get("events") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _calendar_max_date(calendar: list[dict]) -> date | None:
    """固定行事曆裡最後一筆事件日期(判斷是否已到期需補下年度)。"""
    dates = []
    for e in calendar or []:
        try:
            dates.append(_as_date(e.get("date")))
        except Exception:
            continue
    return max(dates) if dates else None


def _decorate(typ: str, d: date, today: date, override: dict | None = None) -> dict:
    """把 (type, date) 補上 label/impact/region/note/星期/距今天數。override 可覆寫 title/impact。"""
    meta = _TYPE_META.get(typ, {"impact": "med", "label": typ, "region": "", "note": ""})
    override = override or {}
    return {
        "date": d.isoformat(),
        "weekday": _WD_TW[d.weekday()],
        "days_ahead": (d - today).days,
        "type": typ,
        "title": override.get("title") or meta["label"],
        "impact": override.get("impact") or meta["impact"],
        "region": meta.get("region", ""),
        "note": meta.get("note", ""),
    }


def upcoming_events(today, horizon_days: int = 7, calendar: list[dict] | None = None) -> dict:
    """未來 horizon_days 個日曆天(含今天)內的重大事件,依日期排序。

    today: date / datetime / 'YYYY-MM-DD'。
    calendar: 覆寫用(離線測),None 時讀 config/events.yaml。
    回傳 {horizon_days, events:[...], has_high, calendar_exhausted, caution}。
    """
    today = _as_date(today)
    end = today + timedelta(days=int(horizon_days))
    calendar = load_events_config() if calendar is None else calendar

    events = list(_formula_events(today, end))
    events_out: list[dict] = [_decorate(e["type"], _as_date(e["date"]), today) for e in events]

    for e in calendar:
        try:
            d = _as_date(e.get("date"))
        except Exception:
            continue
        typ = str(e.get("type", "")).lower()
        if today <= d <= end and typ:
            events_out.append(_decorate(typ, d, today, override=e))

    events_out.sort(key=lambda x: (x["days_ahead"], 0 if x["impact"] == "high" else 1))
    has_high = any(x["impact"] == "high" for x in events_out)

    # 固定行事曆到期:名單裡已沒有任何 >= 今天 的未來項(公式事件不會過期,故只看 B 類)
    cal_max = _calendar_max_date(calendar)
    calendar_exhausted = bool(calendar) and (cal_max is not None) and (cal_max < today)

    caution = None
    if has_high:
        names = "、".join(sorted({x["title"] for x in events_out if x["impact"] == "high"}))
        caution = (f"未來 {horizon_days} 日內有重大事件({names}):波動與開盤跳空風險升高 — "
                   f"留倉部位控管、進場縮量、嚴設停損,別在事件前一天重押。")

    return {
        "horizon_days": int(horizon_days),
        "events": events_out,
        "has_high": has_high,
        "calendar_exhausted": calendar_exhausted,
        "caution": caution,
    }
