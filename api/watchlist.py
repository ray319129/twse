"""Vercel Serverless — 自選池雲端同步(2026-07-19)。

GET  /api/watchlist            → 讀目前 repo 裡的 config/watchlist.json
POST /api/watchlist            → 寫回 repo(GitHub Contents API commit)
     body: {"stocks": {"2330": "台積電", ...}, "secret": "..."}

## 為什麼要這支

自選池原本存 localStorage,**雲端批次看不到** —— 盤後籌碼補抓、分點掃描、
連買連賣都只認 repo 裡的 `config/watchlist.json`,使用者得手動複製貼上再 commit。
使用者 2026-07-19 表示「其實全部都上傳也可以,因為不會涉及到真錢,這只是網頁,
而且這個網頁也只有我在用」,所以改成網頁直接寫回 repo。

## ⚠️ 界線:只上傳「看什麼」,不上傳「賺賠多少」

使用者明確要求:**不要上傳總成本總損益**。所以這支**只接受 stocks 對照表**
(代號 → 備註),**任何 shares / cost / 損益欄位一律丟棄**(見 `_clean`)。
持倉的張數與成本永遠只留在 localStorage,不經過這裡。

## 安全

這是公開端點,沒有保護的話任何人都能改你的 repo。所以:
- 設 `WATCHLIST_SECRET` 環境變數後,POST 必須帶對的 secret 才寫得進去
- **沒設 secret 時 POST 一律拒絕**(fail closed)—— 忘了設就變成任何人可寫,
  那比不能用嚴重得多
- GET 不需要 secret(自選池代號本來就不是機密)

需要的環境變數:`GITHUB_TOKEN`(repo 寫入權)、`GITHUB_REPO`(如 ray319129/twse)、
`WATCHLIST_SECRET`。
"""
from __future__ import annotations
import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PATH = "config/watchlist.json"
MARKS_PATH = "config/my_marks.json"
API = "https://api.github.com"
MAX_STOCKS = 300          # 防呆:自選池不該有幾千檔,而且批次跑不完
MAX_MARKS = 3000          # 買/跳標記上限(一天 ~10 筆,夠用好幾年)
MARK_TTL_DAYS = 400       # 墓碑(取消的標記)保留天數,過了就清掉控檔案大小


def _clean_marks(marks: dict) -> dict:
    """買/跳標記(2026-07-23,使用者要求跨裝置統一)。
    格式 {"YYYY-MM-DD|代號": {"v": "bought"|"skipped"|None, "t": epoch_ms}}。
    v=None 是墓碑(=取消標記)—— 多裝置合併靠每鍵的 t 決定新舊,沒有墓碑的話
    A 裝置取消的標記會被 B 裝置的舊資料復活。
    與自選池同一條隱私界線:只有「哪天、哪檔、買或跳」,**沒有張數/成本/損益**。"""
    import re
    out = {}
    for k, o in (marks or {}).items():
        if not re.match(r"^\d{4}-\d{2}-\d{2}\|\d{4,6}[A-Z]?$", str(k)):
            continue
        if not isinstance(o, dict):
            continue
        v = o.get("v")
        if v not in ("bought", "skipped", None):
            continue
        try:
            t = int(o.get("t") or 0)
        except (TypeError, ValueError):
            t = 0
        out[str(k)] = {"v": v, "t": t}
        if len(out) >= MAX_MARKS:
            break
    return out


def _clean(stocks: dict) -> dict:
    """只留「代號 → 備註字串」。**任何持倉相關欄位一律丟棄** —— 使用者明確要求
    不上傳成本損益,所以就算前端不小心送上來也不會被寫進 repo。"""
    out = {}
    for k, v in (stocks or {}).items():
        sid = str(k).strip()
        if not sid or not sid[0].isdigit():
            continue
        if isinstance(v, dict):                 # 前端的 {name, note, ts} → 只取名稱備註
            v = " ".join(str(v.get(x) or "") for x in ("name", "note")).strip()
        out[sid] = str(v or "")[:60]
        if len(out) >= MAX_STOCKS:
            break
    return out


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("check"):
                # 設定自檢:只回「有沒有設」,**絕不回值**。
                # 這支存在的理由:2026-07-19 使用者在 Vercel 設好了變數卻仍被拒絕,
                # 原因是 **Vercel 環境變數要重新部署才生效**,舊部署讀到的是空值。
                # 沒有自檢就只能猜「是我打錯還是沒生效」。
                env = {k: bool(os.environ.get(k))
                       for k in ("WATCHLIST_SECRET", "GITHUB_TOKEN", "GITHUB_REPO")}
                return self._send(200, {
                    "env": env,
                    "ready": all(env.values()),
                    "hint": ("全部就緒。" if all(env.values()) else
                             "缺少變數。若你在 Vercel 已經設好卻仍顯示 false,"
                             "那是**環境變數需要重新部署才生效** —— 到 Vercel "
                             "Deployments 點最新一筆的 Redeploy,或推一個 commit。"),
                })
            if (qs.get("what") or [""])[0] == "marks":
                cur, _ = self._read(MARKS_PATH)
                return self._send(200, {"marks": cur.get("marks", {}), "source": "repo"})
            cur, _ = self._read()
            self._send(200, {"stocks": cur.get("stocks", {}), "source": "repo"})
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
            # fail closed:寧可不能用,也不要變成任何人都能改 repo
            return self._send(403, {"error":
                "伺服器讀不到 WATCHLIST_SECRET,拒絕寫入。"
                "若你在 Vercel 已經設好 —— **環境變數要重新部署才生效**,"
                "請到 Deployments 點最新一筆的 Redeploy。"
                "可用 /api/watchlist?check=1 確認變數是否已被讀到。"})
        if body.get("secret") != secret:
            return self._send(403, {"error": "secret 不正確"})

        # ---- 買/跳標記同步(帶 marks 就走這條;與自選池共用 secret 與 token)----
        if "marks" in body:
            marks = _clean_marks(body.get("marks"))
            try:
                cur, sha = self._read(MARKS_PATH)
                # 伺服器端也做一次合併(每鍵 t 新者勝)—— 兩台裝置幾乎同時 POST 時,
                # 後到的不會把先到的整包蓋掉
                merged = dict(cur.get("marks") or {})
                for k, o in marks.items():
                    old = merged.get(k)
                    if not isinstance(old, dict) or int(o.get("t") or 0) >= int(old.get("t") or 0):
                        merged[k] = o
                # 清掉過期墓碑
                import time as _time
                cutoff = int(_time.time() * 1000) - MARK_TTL_DAYS * 86400_000
                merged = {k: o for k, o in merged.items()
                          if not (o.get("v") is None and int(o.get("t") or 0) < cutoff)}
                if merged == (cur.get("marks") or {}):
                    return self._send(200, {"ok": True, "changed": False,
                                            "n": len(merged), "marks": merged})
                payload = {"_comment": "買/跳標記(我的實戰紀錄)。由網頁同步(api/watchlist.py?what=marks);"
                                       "只有日期|代號與買/跳,無任何金額。",
                           "marks": merged}
                content = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
                r = requests.put(
                    f"{API}/repos/{os.environ['GITHUB_REPO']}/contents/{MARKS_PATH}",
                    headers=self._gh_headers(),
                    json={"message": f"marks: sync {len(merged)} 筆 (web)",
                          "content": base64.b64encode(content.encode()).decode(),
                          **({"sha": sha} if sha else {})},
                    timeout=25)
                if r.status_code not in (200, 201):
                    return self._send(502, {"error": f"GitHub 寫入失敗 {r.status_code}: {r.text[:200]}"})
                # 回合併後的全量,前端直接採用 → 兩台裝置各自 POST 後都會拿到同一份
                return self._send(200, {"ok": True, "changed": True,
                                        "n": len(merged), "marks": merged})
            except KeyError as e:
                return self._send(500, {"error": f"缺少環境變數 {e}"})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        stocks = _clean(body.get("stocks"))
        if not stocks:
            return self._send(400, {"error": "stocks 是空的或格式不對"})

        try:
            cur, sha = self._read()
            if cur.get("stocks") == stocks:
                return self._send(200, {"ok": True, "changed": False, "n": len(stocks)})
            payload = {
                "_comment": "自選股清單。由網頁自選池同步(api/watchlist.py),也可直接手改。",
                "stocks": stocks,
            }
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            r = requests.put(
                f"{API}/repos/{os.environ['GITHUB_REPO']}/contents/{PATH}",
                headers=self._gh_headers(),
                json={"message": f"watchlist: sync {len(stocks)} 檔 (web)",
                      "content": base64.b64encode(content.encode()).decode(),
                      **({"sha": sha} if sha else {})},
                timeout=25)
            if r.status_code == 403 and "not accessible by personal access token" in r.text:
                # 2026-07-20 實際踩到:token 有效但缺 Contents 寫入權。
                # GitHub 的原文訊息完全沒說要開哪個權限,所以這裡直接寫清楚。
                return self._send(403, {"error":
                    "GitHub token 權限不足(缺 Contents 寫入權)。"
                    "若用 Fine-grained token:Repository access 選這個 repo,"
                    "Permissions → Repository permissions → **Contents 設為 Read and write**,"
                    "存檔後把新 token 更新到 Vercel 並 Redeploy。"
                    "若用 Classic token:勾選 **repo** 這個 scope。"})
            if r.status_code not in (200, 201):
                return self._send(502, {"error": f"GitHub 寫入失敗 {r.status_code}: {r.text[:200]}"})
            self._send(200, {"ok": True, "changed": True, "n": len(stocks)})
        except KeyError as e:
            self._send(500, {"error": f"缺少環境變數 {e}"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # ---- helpers ----
    def _gh_headers(self):
        return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json"}

    def _read(self, path: str = PATH):
        """回 (內容 dict, sha)。抓不到就回 ({}, None) —— 第一次寫入沒有 sha 是正常的。"""
        try:
            r = requests.get(f"{API}/repos/{os.environ['GITHUB_REPO']}/contents/{path}",
                             headers=self._gh_headers(), timeout=20)
            if r.status_code != 200:
                return {}, None
            j = r.json()
            return json.loads(base64.b64decode(j["content"]).decode()), j.get("sha")
        except Exception:
            return {}, None

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
