from __future__ import annotations
import os
import json
from pathlib import Path
from datetime import datetime
import pytz
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PRICES_DIR = DATA_DIR / "prices"
META_DIR = DATA_DIR / "meta"
SIGNALS_DIR = DATA_DIR / "signals"
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"

try:
    for _d in (PRICES_DIR, META_DIR, SIGNALS_DIR):
        _d.mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # Vercel serverless: read-only filesystem, skip dir creation

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")   # 選用:新聞事件 AI 分類;沒設就自動略過
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
MAIL_TO = os.environ.get("MAIL_TO") or GMAIL_USER
# Discord 盤中通知(2026-07-21)。沒設就自動退回 Email —— 通知管道壞掉不該讓盯盤壞掉。
# 取得方式:Discord 頻道 → 編輯頻道 → 整合 → Webhook → 新增 → 複製 Webhook 網址。
# 這是一條「知道網址就能發文」的密鑰,**務必存成 GitHub secret,不要進 repo**。
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

TZ_TPE = pytz.timezone("Asia/Taipei")


def now_tpe() -> datetime:
    return datetime.now(TZ_TPE)


def load_screeners() -> dict:
    with open(CONFIG_DIR / "screeners.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_watchlist() -> dict[str, str]:
    path = CONFIG_DIR / "watchlist.json"
    if not path.exists():
        path = CONFIG_DIR / "watchlist.example.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("stocks", {})


def assert_env(*, require_mail: bool = True, require_finmind: bool = True) -> None:
    needed = {}
    if require_finmind:
        needed["FINMIND_TOKEN"] = FINMIND_TOKEN
    if require_mail:
        needed.update({
            "GMAIL_USER": GMAIL_USER,
            "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
            "MAIL_TO": MAIL_TO,
        })
    missing = [k for k, v in needed.items() if not v]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")
