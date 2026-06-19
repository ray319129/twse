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

for _d in (PRICES_DIR, META_DIR, SIGNALS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
MAIL_TO = os.environ.get("MAIL_TO") or GMAIL_USER

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
