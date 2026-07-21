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
# description 4096 字、整則訊息所有文字加總 6000 字。超過會直接 400。
MAX_EMBEDS = 10
MAX_DESC = 4000          # 留 96 字餘裕給截斷標記


def _post(url: str, payload: dict, method: str = "POST") -> dict | None:
    try:
        r = requests.request(method, url, json=payload, timeout=20)
        if r.status_code == 404:
            # 訊息被手動刪掉了 —— 呼叫端要據此重發一則新的,不是無限重試
            log.info("Discord 訊息不存在(可能已被刪除)。")
            return None
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        log.warning(f"Discord 發送失敗(不影響主流程):{e}")
        return None


def send_discord(embeds: list[dict], content: str = "",
                 components: list[dict] | None = None) -> str | None:
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
    j = _post(url, payload)
    return (j or {}).get("id")


def edit_discord(message_id: str, embeds: list[dict], content: str = "",
                 components: list[dict] | None = None) -> bool:
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
    return _post(url, payload, method="PATCH") is not None


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
