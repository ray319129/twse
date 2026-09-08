"""當沖法規閘門 —— 今天這檔「能不能當沖」「能不能做空」的唯一判斷來源。

## 為什麼這支必須存在

做空(先賣後買)如果收盤沒買回 → 券差 → 券商代為借券 → **當日 15:30 前沒補款
就報違約交割**。那不是虧錢,是信用問題。所以空方訊號在推出去之前一定要先過閘門,
不能只看技術面。

## 資料源(全部免費、免金鑰,2026-09-08 實測可用)

- 上市可當沖 + 暫停先賣後買:openapi.twse.com.tw/v1/exchangeReport/TWTB4U(Suspension 欄)
- 上市處置股:openapi.twse.com.tw/v1/announcement/punish
- 上市注意股:openapi.twse.com.tw/v1/announcement/notice
- 上櫃可當沖 + 暫停先賣後買:tpex.org.tw/openapi/v1/tpex_securities
- 上櫃處置股:tpex_disposal_information
- 上櫃注意股:tpex_trading_warning_information
- 上櫃券差借券費率:tpex_intraday_fee

⚠️ Suspension="Y" 是**暫停先賣後買**,不是不能當沖 —— 實測名單裡有台泥、瑞昱
這種大型股,原因是除權息停止過戶。所以它只擋空方,不擋多方。
把它當成「不能當沖」會少掉一堆可做的標的。

## 失敗策略:fail closed

抓不到清單時一律回「不可做」而不是「可做」。這一層錯的代價不對稱 ——
少推幾個機會只是少賺,推了一個不能做空的標的可能變成違約交割。
"""
from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from ..config import DATA_DIR
from ..utils import log

CACHE_DIR = DATA_DIR / "daytrade"
_TIMEOUT = 30
_UA = {"accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; twse-daytrade/1.0)"}

TWSE_ELIGIBLE = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
TWSE_PUNISH = "https://openapi.twse.com.tw/v1/announcement/punish"
TWSE_NOTICE = "https://openapi.twse.com.tw/v1/announcement/notice"
TPEX_ELIGIBLE = "https://www.tpex.org.tw/openapi/v1/tpex_securities"
TPEX_DISPOSAL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
TPEX_WARNING = "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information"
TPEX_BORROW_FEE = "https://www.tpex.org.tw/openapi/v1/tpex_intraday_fee"


@dataclass
class Gate:
    """單一標的的閘門判定結果。long_ok / short_ok 是給訊號層的唯一依據。"""
    stock_id: str
    name: str = ""
    market: str = ""                 # twse / tpex
    eligible: bool = False           # 今日是否為可當沖標的
    short_suspended: bool = False    # 暫停先賣後買(除權息停過戶等)
    disposed: bool = False           # 處置股
    warned: bool = False             # 注意股
    borrow_fee_pct: float | None = None   # 券差借券費率(%),None = 無資料
    reasons: list[str] = field(default_factory=list)

    @property
    def long_ok(self) -> bool:
        return self.eligible and not self.disposed

    @property
    def short_ok(self) -> bool:
        return self.eligible and not self.disposed and not self.short_suspended

    def explain(self) -> str:
        if not self.eligible:
            return "非今日可當沖標的"
        bits = []
        if self.disposed:
            bits.append("處置股")
        if self.short_suspended:
            bits.append("暫停先賣後買")
        if self.warned:
            bits.append("注意股")
        if self.borrow_fee_pct:
            bits.append(f"借券費 {self.borrow_fee_pct:g}%")
        return "・".join(bits) if bits else "閘門全過"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["long_ok"], d["short_ok"] = self.long_ok, self.short_ok
        d["explain"] = self.explain()
        return d


class _RelaxedTLSAdapter(HTTPAdapter):
    """櫃買中心(tpex.org.tw)的憑證缺 Subject Key Identifier 擴充欄位。

    Python **3.13 起 `ssl.create_default_context()` 預設開啟 `VERIFY_X509_STRICT`**,
    會因此直接拒連 —— 本機 Python 3.14 實測全部上櫃 feed 掛掉,而 curl 同一台機器
    HTTP 200。CI 目前跑 3.11 所以看不出來,但**哪天 CI 升到 3.13+,所有上櫃股會
    因為 fail closed 被靜默排除在當沖標的池外**,而且不會有任何錯誤浮上來。

    這裡只關掉「X509 擴充欄位嚴格檢查」,**憑證鏈與主機名驗證都保留** ——
    不是 verify=False。只掛在 tpex 這個 host 上,不影響其他請求。
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_UA)
    s.mount("https://www.tpex.org.tw", _RelaxedTLSAdapter())
    return s


_SESSION: requests.Session | None = None


def _get(url: str) -> list[dict]:
    """抓一支 OpenAPI。失敗回空 list —— 由呼叫端決定 fail closed 的行為。"""
    global _SESSION
    if _SESSION is None:
        _SESSION = _session()
    try:
        r = _SESSION.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"當沖閘門抓取失敗 {url.rsplit('/', 1)[-1]}:{e}")
        return []


def _sid(row: dict, *keys) -> str:
    for k in keys:
        v = row.get(k)
        if v:
            return str(v).strip()
    return ""


def _parse_roc(x: str) -> date | None:
    digits = "".join(ch for ch in str(x) if ch.isdigit())
    if len(digits) != 7:
        return None
    try:
        return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
    except ValueError:
        return None


def in_roc_period(period: str, today: date) -> bool:
    """處置期間字串(民國)判斷今天是否在期間內。

    格式實測有兩種:「115/09/07～115/09/15」與「1150908~1150914」。
    **看不懂的格式一律回 True**(fail closed:寧可誤擋也不要誤放一檔處置股)。
    """
    if not period:
        return False
    s = str(period).replace("～", "~").replace("　", "").strip()
    parts = [p.strip() for p in s.split("~") if p.strip()]
    if len(parts) != 2:
        return True
    a, b = _parse_roc(parts[0]), _parse_roc(parts[1])
    if a is None or b is None:
        return True
    return a <= today <= b


def fetch_gates(today: date | None = None) -> dict[str, Gate]:
    """抓齊六個 feed,組成 {stock_id: Gate}。純網路 I/O,不寫檔。"""
    today = today or date.today()
    gates: dict[str, Gate] = {}

    # ── 可當沖標的(白名單:沒出現在這裡的一律不可當沖)──
    for row in _get(TWSE_ELIGIBLE):
        sid = _sid(row, "Code")
        if not sid:
            continue
        gates[sid] = Gate(stock_id=sid, name=_sid(row, "Name"), market="twse",
                          eligible=True,
                          short_suspended=(str(row.get("Suspension", "")).strip().upper() == "Y"))
    for row in _get(TPEX_ELIGIBLE):
        sid = _sid(row, "證券代號", "SecuritiesCompanyCode", "Code")
        if not sid:
            continue
        flag = str(row.get("暫停現股賣出後現款買進當沖註記", "")).strip()
        gates[sid] = Gate(stock_id=sid, name=_sid(row, "證券名稱", "CompanyName"), market="tpex",
                          eligible=True, short_suspended=bool(flag))

    # ── 處置股:只有在處置期間內才算 ──
    for row in _get(TWSE_PUNISH):
        sid = _sid(row, "Code")
        if sid in gates and in_roc_period(row.get("DispositionPeriod", ""), today):
            gates[sid].disposed = True
            gates[sid].reasons.append("處置:" + _sid(row, "ReasonsOfDisposition")[:40])
    for row in _get(TPEX_DISPOSAL):
        sid = _sid(row, "SecuritiesCompanyCode", "Code")
        if sid in gates and in_roc_period(row.get("DispositionPeriod", ""), today):
            gates[sid].disposed = True
            gates[sid].reasons.append("處置:" + _sid(row, "DispositionReasons")[:40])

    # ── 注意股(不擋單,只標示:注意股常伴隨處置風險升高)──
    for row in _get(TWSE_NOTICE):
        sid = _sid(row, "Code")
        if sid in gates:
            gates[sid].warned = True
    for row in _get(TPEX_WARNING):
        sid = _sid(row, "SecuritiesCompanyCode", "Code")
        if sid in gates:
            gates[sid].warned = True

    # ── 券差借券費率:同一檔有多筆(不同數量級距),取最高的最保守 ──
    # 注意:實測 JSON 的欄位名前面帶一個空格(" LendingFee"),兩種都要試。
    for row in _get(TPEX_BORROW_FEE):
        sid = _sid(row, "SecuritiesCompanyCode", "Code")
        if sid not in gates:
            continue
        raw = row.get("LendingFee", row.get(" LendingFee"))
        try:
            fee = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        cur = gates[sid].borrow_fee_pct
        gates[sid].borrow_fee_pct = fee if cur is None else max(cur, fee)

    log.info(f"當沖閘門:{len(gates)} 檔可當沖,"
             f"暫停先賣後買 {sum(1 for g in gates.values() if g.short_suspended)} 檔,"
             f"處置 {sum(1 for g in gates.values() if g.disposed)} 檔,"
             f"有借券費資料 {sum(1 for g in gates.values() if g.borrow_fee_pct is not None)} 檔")
    return gates


def cache_path(day: date) -> Path:
    return CACHE_DIR / f"gates-{day.isoformat()}.json"


def load_or_fetch(today: date | None = None, *, force: bool = False) -> dict[str, Gate]:
    """當日快取優先。閘門清單一天只變一次,盤中反覆抓沒有意義,還可能被上游擋。"""
    today = today or date.today()
    p = cache_path(today)
    if not force and p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            fields = Gate.__dataclass_fields__
            return {k: Gate(**{kk: vv for kk, vv in v.items() if kk in fields})
                    for k, v in raw.get("gates", {}).items()}
        except Exception as e:
            log.warning(f"當沖閘門快取讀取失敗,改為重抓:{e}")
    gates = fetch_gates(today)
    if gates:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"date": today.isoformat(),
             "fetched_at": datetime.now().isoformat(timespec="seconds"),
             "gates": {k: v.to_dict() for k, v in gates.items()}},
            ensure_ascii=False), encoding="utf-8")
    return gates


def gate_for(gates: dict[str, Gate], stock_id: str) -> Gate:
    """查不到一律回 eligible=False(fail closed)。"""
    return gates.get(str(stock_id), Gate(stock_id=str(stock_id), eligible=False))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    g = load_or_fetch(force="--force" in sys.argv)
    print(f"\n可當沖 {len(g)} 檔")
    print(f"  多方可做 {sum(1 for x in g.values() if x.long_ok)}")
    print(f"  空方可做 {sum(1 for x in g.values() if x.short_ok)}")
    for sid in ("2330", "2449", "1101", "2379", "0050"):
        x = gate_for(g, sid)
        print(f"  {sid} {x.name:<10} long={x.long_ok} short={x.short_ok}  {x.explain()}")
