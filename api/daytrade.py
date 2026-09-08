"""Vercel Serverless — 當沖偏好與部位同步(2026-09-08)。

    GET  /api/daytrade?what=prefs      → config/daytrade_prefs.json
    GET  /api/daytrade?what=positions  → data/daytrade/positions-YYYY-MM-DD.json
    GET  /api/daytrade?check=1         → 環境變數自檢(只回有沒有設,絕不回值)
    POST /api/daytrade  {"prefs": {...}, "secret": "..."}
    POST /api/daytrade  {"positions": [...], "day": "YYYY-MM-DD", "secret": "..."}

## 為什麼要這支

盯盤跑在 GitHub Actions 上,讀不到你瀏覽器的 localStorage。而
**強制回補提醒需要知道你手上有什麼部位** —— 沒有部位資料,那一層(整個當沖層裡
價值最高、最不受延遲影響的一層)就完全沒東西可提醒。

沿用 `api/watchlist.py` 已經驗證過的模式:GitHub Contents API + 共用 secret,
沒設 secret 一律拒絕寫入(fail closed)。

## 隱私界線(2026-09-08 使用者裁示方案 B)

- `prefs` 只收白名單欄位,**其中包含 `quota_twd`(額度上限)** ——
  這放寬了 2026-07-19「不上傳成本損益」的界線,但只放寬到「額度上限」這一個數字。
- `positions` 收代號/方向/進場價/張數/停損 —— 這些是**強制回補提醒必需的**
  (要算未實現損益、要知道哪幾筆是先賣後買)。
- **不收**任何累計損益、總資產、帳戶餘額欄位(`_clean_positions` 只取白名單)。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PREFS_PATH = "config/daytrade_prefs.json"
API = "https://api.github.com"
MAX_POSITIONS = 50

# 只接受這些偏好欄位,其餘一律丟棄(前端多送不會寫進 repo)
_PREF_NUM = {
    "quota_twd": (0, 100_000_000),
    "price_min": (0, 10_000), "price_max": (0, 10_000),
    "min_dollar_volume_m": (0, 1_000_000),
    "min_atr_pct": (0, 100), "max_atr_pct": (0, 100),
    "max_cost_pct": (0, 100),
    "max_borrow_fee_pct": (0, 100),
    "min_edge_ratio": (0, 100),
    "max_universe": (1, 200),
}
_PREF_BOOL = {"allow_long", "allow_short"}
_PREF_LIST = {"exclude", "include_always"}


def _clean_prefs(raw: dict) -> dict:
    out: dict = {}
    for k, (lo, hi) in _PREF_NUM.items():
        if k not in (raw or {}):
            continue
        try:
            v = float(raw[k])
        except (TypeError, ValueError):
            continue
        if v != v or not (lo <= v <= hi):      # NaN 或超出合理範圍就丟掉
            continue
        out[k] = int(v) if k in ("quota_twd", "max_universe") else v
    for k in _PREF_BOOL:
        if k in (raw or {}):
            out[k] = bool(raw[k])
    for k in _PREF_LIST:
        if k in (raw or {}):
            ids = [str(x).strip() for x in (raw[k] or []) if str(x).strip()]
            out[k] = [x for x in ids if re.fullmatch(r"\d{4,6}[A-Z]?", x)][:100]
    # 內部一致性:價格區間反了就交換,不要寫進一組永遠篩不到東西的設定
    if "price_min" in out and "price_max" in out and out["price_min"] > out["price_max"]:
        out["price_min"], out["price_max"] = out["price_max"], out["price_min"]
    return out


def _clean_positions(raw: list) -> list:
    """只取強制回補提醒真正需要的欄位。任何損益/餘額欄位一律不收。"""
    out = []
    for r in (raw or []):
        if not isinstance(r, dict):
            continue
        sid = str(r.get("stock_id") or "").strip()
        if not re.fullmatch(r"\d{4,6}[A-Z]?", sid):
            continue
        side = str(r.get("side") or "").strip()
        if side not in ("long", "short"):
            continue
        try:
            entry = float(r.get("entry_price"))
            lots = int(r.get("lots") or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or lots <= 0 or lots > 10_000:
            continue
        stop = r.get("stop_price")
        try:
            stop = float(stop) if stop is not None else None
            if stop is not None and stop <= 0:
                stop = None
        except (TypeError, ValueError):
            stop = None
        out.append({
            "stock_id": sid,
            "name": str(r.get("name") or "")[:20],
            "side": side,
            "entry_price": round(entry, 4),
            "lots": lots,
            "stop_price": round(stop, 4) if stop is not None else None,
            "opened_at": str(r.get("opened_at") or "")[:19],
            "closed": bool(r.get("closed")),
        })
        if len(out) >= MAX_POSITIONS:
            break
    return out


def _positions_path(day: str) -> str:
    return f"data/daytrade/positions-{day}.json"


def _valid_day(d: str) -> str:
    return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d or "")) else date.today().isoformat()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("check"):
                env = {k: bool(os.environ.get(k))
                       for k in ("WATCHLIST_SECRET", "GITHUB_TOKEN", "GITHUB_REPO")}
                return self._send(200, {
                    "env": env, "ready": all(env.values()),
                    "hint": ("全部就緒。" if all(env.values()) else
                             "缺少變數。Vercel 的環境變數**要重新部署才生效** —— "
                             "到 Deployments 點最新一筆的 Redeploy。"),
                })
            what = (qs.get("what") or ["prefs"])[0]
            if what == "positions":
                day = _valid_day((qs.get("day") or [""])[0])
                cur, _ = self._read(_positions_path(day))
                return self._send(200, {"day": day,
                                        "positions": cur.get("positions", [])})
            cur, _ = self._read(PREFS_PATH)
            return self._send(200, {"prefs": {k: v for k, v in cur.items()
                                              if not k.startswith("_")}})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "body 不是合法 JSON"})

        secret = os.environ.get("WATCHLIST_SECRET", "")
        if not secret:
            return self._send(403, {"error":
                "伺服器讀不到 WATCHLIST_SECRET,拒絕寫入。"
                "Vercel 環境變數要 Redeploy 才生效;可用 /api/daytrade?check=1 確認。"})
        if body.get("secret") != secret:
            return self._send(403, {"error": "secret 不正確"})

        try:
            if "positions" in body:
                return self._write_positions(body)
            if "prefs" in body:
                return self._write_prefs(body)
            return self._send(400, {"error": "body 要帶 prefs 或 positions"})
        except KeyError as e:
            return self._send(500, {"error": f"缺少環境變數 {e}"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # ---- writers ----
    def _write_prefs(self, body: dict):
        incoming = _clean_prefs(body.get("prefs"))
        if not incoming:
            return self._send(400, {"error": "prefs 沒有任何合法欄位"})
        cur, sha = self._read(PREFS_PATH)
        merged = dict(cur)
        merged.update(incoming)          # 部分更新:前端只送改動的欄位也可以
        if {k: v for k, v in merged.items() if not k.startswith("_")} == \
           {k: v for k, v in cur.items() if not k.startswith("_")}:
            return self._send(200, {"ok": True, "changed": False, "prefs": merged})
        merged.setdefault("_comment",
                          "當沖個人偏好。網頁可改(api/daytrade.py),也可直接手改。")
        ok, err = self._put(PREFS_PATH, merged, sha,
                            f"daytrade: prefs 更新 (web)")
        if not ok:
            return self._send(502, {"error": err})
        return self._send(200, {"ok": True, "changed": True,
                                "prefs": {k: v for k, v in merged.items()
                                          if not k.startswith("_")}})

    def _write_positions(self, body: dict):
        day = _valid_day(body.get("day"))
        positions = _clean_positions(body.get("positions"))
        path = _positions_path(day)
        cur, sha = self._read(path)
        if cur.get("positions") == positions:
            return self._send(200, {"ok": True, "changed": False,
                                    "day": day, "n": len(positions)})
        payload = {
            "_comment": ("當沖部位(供強制回補與停損提醒)。由網頁標記同步。"
                         "只有代號/方向/進場價/張數/停損 —— 無累計損益、無帳戶餘額。"),
            "date": day, "positions": positions,
        }
        ok, err = self._put(path, payload, sha,
                            f"daytrade: positions {day} {len(positions)} 筆 (web)")
        if not ok:
            return self._send(502, {"error": err})
        return self._send(200, {"ok": True, "changed": True,
                                "day": day, "n": len(positions),
                                "positions": positions})

    # ---- helpers(與 api/watchlist.py 同一套,含 403 權限不足的白話說明)----
    def _gh_headers(self):
        return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json"}

    def _read(self, path: str):
        try:
            r = requests.get(f"{API}/repos/{os.environ['GITHUB_REPO']}/contents/{path}",
                             headers=self._gh_headers(), timeout=20)
            if r.status_code != 200:
                return {}, None
            j = r.json()
            return json.loads(base64.b64decode(j["content"]).decode()), j.get("sha")
        except Exception:
            return {}, None

    def _put(self, path: str, payload: dict, sha, message: str):
        content = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
        r = requests.put(
            f"{API}/repos/{os.environ['GITHUB_REPO']}/contents/{path}",
            headers=self._gh_headers(),
            json={"message": message,
                  "content": base64.b64encode(content.encode()).decode(),
                  **({"sha": sha} if sha else {})},
            timeout=25)
        if r.status_code == 403 and "not accessible by personal access token" in r.text:
            return False, ("GitHub token 權限不足(缺 Contents 寫入權)。"
                           "Fine-grained token 要把 Repository permissions → "
                           "**Contents 設為 Read and write**;Classic token 勾 **repo**。")
        if r.status_code not in (200, 201):
            return False, f"GitHub 寫入失敗 {r.status_code}: {r.text[:200]}"
        return True, ""

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
