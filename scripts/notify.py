"""通知管道:Email(盤後長報告)+ Discord(盤中即時)。

## 為什麼盤中改走 Discord(2026-07-21)

Email 是**單向**而且**只能一直寄新的**。盤中訊號的本質是「同一件事整天在更新」,
用 Email 表達就會變成洗版 —— 本專案為此已經加了當日去重、單輪上限 8 筆等一堆補丁,
但那是在跟媒介的形狀對抗。

Discord webhook 可以 **PATCH 已發出的訊息**,所以整天只有**一則**「今日訊號」,
新訊號進來就把它更新掉。洗版問題從根本消失,不需要任何節流補丁。

**刻意只用 webhook,不做 bot。** webhook 就是一個 URL、POST JSON,沒有 token 生命週期、
沒有簽章驗證、沒有 3 秒回應死線。代價是**收不到按鈕點擊**(那要 Interactions Endpoint
+ Ed25519 驗簽),所以這裡只發**連結型按鈕**(style 5)。
「我買了/我跳過」那種要回寫的按鈕是之後的階段二,再決定要不要付那個複雜度。

## 降級

沒設 `DISCORD_WEBHOOK_URL` → `send_discord` 回 None,呼叫端自動退回 Email。
通知管道壞掉絕對不該讓盯盤或選股壞掉,所有函式都不 raise。
"""
from __future__ import annotations
import json
import smtplib
from email.message import EmailMessage

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import (GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO, TEMPLATES_DIR,
                     DISCORD_WEBHOOK_URL)
from .utils import log


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_email(template: str, ctx: dict) -> str:
    return _env.get_template(template).render(**ctx)


def send_email(subject: str, html: str, text_fallback: str = "") -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = MAIL_TO
    msg.set_content(text_fallback or "請以 HTML 模式查看本信。")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
    log.info(f"Email sent to {MAIL_TO}: {subject}")


# ============================================================
# Discord
# ============================================================

discord_enabled = lambda: bool(DISCORD_WEBHOOK_URL)      # noqa: E731

# Discord 硬上限:一則訊息最多 10 個 embed、每個 embed 最多 25 個 field、
# description 4096 字、**整則訊息所有 embed 的文字加總 6000 字**。超過直接 400。
#
# ⚠️ 那個 6000 是最容易踩到的一條(2026-07-21 就是死在這):單張卡看起來不長,
# 但 8 張一起送就爆了。而且錯誤訊息不看 response body 根本看不出來。
MAX_EMBEDS = 10
MAX_DESC = 4000          # 留 96 字餘裕給截斷標記
MAX_TOTAL_CHARS = 5500   # 對 6000 留 500 餘裕(content 與 footer 也算在某些情況)


def _embed_chars(e: dict) -> int:
    n = (len(e.get("title") or "") + len(e.get("description") or "")
         + len((e.get("author") or {}).get("name") or "")
         + len((e.get("footer") or {}).get("text") or ""))
    for f in e.get("fields") or []:
        n += len(f.get("name") or "") + len(f.get("value") or "")
    return n


def fit_embeds(embeds: list[dict], drop_fields: tuple[str, ...] = ()) -> list[dict]:
    """把整批 embed 壓進 Discord 的 6000 字總量限制。

    **降級順序是刻意的**:先砍 `drop_fields` 指定的次要欄位(通常是新聞),
    因為訊號本身的價格/量比/理由才是決策要的;真的還是超標才整張卡不送。
    寧可少幾行新聞,也不要整批訊號變成一封 Email。
    """
    out = [dict(e) for e in embeds[:MAX_EMBEDS]]
    if sum(_embed_chars(e) for e in out) <= MAX_TOTAL_CHARS:
        return out
    # ① 由後往前砍次要欄位(先保住最前面那幾張,那是最強的訊號)
    for name in drop_fields:
        for e in reversed(out):
            if sum(_embed_chars(x) for x in out) <= MAX_TOTAL_CHARS:
                break
            fs = [f for f in (e.get("fields") or []) if not (f.get("name") or "").startswith(name)]
            if len(fs) != len(e.get("fields") or []):
                e["fields"] = fs
    # ② 還是超標就整張丟掉(從最後一張開始)
    while out and sum(_embed_chars(e) for e in out) > MAX_TOTAL_CHARS:
        out.pop()
    return out


def _post(url: str, payload: dict, method: str = "POST",
          files: list[tuple[str, bytes]] | None = None) -> dict | None:
    """`files` = [(檔名, PNG bytes), ...]。

    ⚠️ **圖片一律用附件上傳,不要用外部 URL。** 圖床方案(commit 到 repo 再引用
    raw.githubusercontent)有約 5 分鐘 CDN 快取 —— 訊號都過期了圖才出現。
    附件是隨訊息一起送的,沒有這個問題,也不需要任何 hosting。

    embed 用 `attachment://<檔名>` 引用同一則訊息裡的附件。
    編輯時要帶 `attachments` 陣列宣告「這則訊息現在有哪些附件」;
    只列新的 = 舊的會被移除,正好是我們要的(整批重畫)。
    """
    try:
        if files:
            payload = dict(payload)
            payload["attachments"] = [{"id": i, "filename": fn}
                                      for i, (fn, _) in enumerate(files)]
            fd = {f"files[{i}]": (fn, b, "image/png")
                  for i, (fn, b) in enumerate(files)}
            r = requests.request(method, url, data={"payload_json": json.dumps(payload)},
                                 files=fd, timeout=45)
        else:
            r = requests.request(method, url, json=payload, timeout=20)
        if r.status_code == 404:
            # 訊息被手動刪掉了 —— 呼叫端要據此重發一則新的,不是無限重試
            log.info("Discord 訊息不存在(可能已被刪除)。")
            return None
        if r.status_code >= 400:
            # ⚠️ **一定要把回應內容印出來。** 2026-07-21 踩到:只印 "400 Bad Request"
            # 完全看不出哪裡錯,而 Discord 的回應 body 會明講是哪個欄位超限。
            # 沒有這行就只能猜,那次猜了很久。
            log.warning(f"Discord {method} {r.status_code}:{r.text[:600]}")
            return None
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        log.warning(f"Discord 發送失敗(不影響主流程):{e}")
        return None


def send_discord(embeds: list[dict], content: str = "",
                 components: list[dict] | None = None,
                 files: list[tuple[str, bytes]] | None = None) -> str | None:
    """發一則新訊息。回傳 message_id(之後要就地編輯它就靠這個),失敗回 None。

    `?wait=true` 是**必要的** —— 沒有它 Discord 回 204 空 body,拿不到 message_id,
    也就沒辦法再編輯這則訊息(整個「一則訊息整天更新」的設計就垮了)。
    """
    if not DISCORD_WEBHOOK_URL:
        return None
    url = DISCORD_WEBHOOK_URL + ("&" if "?" in DISCORD_WEBHOOK_URL else "?") + "wait=true"
    if components:
        url += "&with_components=true"    # 沒帶這個參數,components 會被靜默忽略
    payload = {"embeds": embeds[:MAX_EMBEDS], "content": content[:2000]}
    if components:
        payload["components"] = components
    j = _post(url, payload, files=files)
    return (j or {}).get("id")


def edit_discord(message_id: str, embeds: list[dict], content: str = "",
                 components: list[dict] | None = None,
                 files: list[tuple[str, bytes]] | None = None) -> bool:
    """就地更新已發出的訊息 —— 這是整個「零洗版」設計的關鍵。
    回 False 代表訊息不在了(被刪),呼叫端應該重發一則新的。"""
    if not DISCORD_WEBHOOK_URL or not message_id:
        return False
    url = f"{DISCORD_WEBHOOK_URL}/messages/{message_id}"
    if components:
        url += "?with_components=true"
    payload = {"embeds": embeds[:MAX_EMBEDS], "content": content[:2000]}
    if components:
        payload["components"] = components
    return _post(url, payload, method="PATCH", files=files) is not None


def link_buttons(items: list[tuple[str, str]]) -> list[dict]:
    """連結按鈕列(style 5)。**webhook 只能發這種** —— 會回寫狀態的按鈕需要 bot
    接 Interactions Endpoint 並做 Ed25519 驗簽,是之後的階段二。
    一列最多 5 個,最多 5 列。"""
    rows, cur = [], []
    for label, url in items[:25]:
        cur.append({"type": 2, "style": 5, "label": label[:80], "url": url})
        if len(cur) == 5:
            rows.append({"type": 1, "components": cur})
            cur = []
    if cur:
        rows.append({"type": 1, "components": cur})
    return rows[:5]
