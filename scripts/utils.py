from __future__ import annotations
import logging
import time
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("twse")

UA = "Mozilla/5.0 (compatible; twse-screener/0.1)"


def http_get_json(url: str, params: dict | None = None, retries: int = 3, delay: float = 3.0):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            log.warning(f"http_get_json {url} fail {i+1}/{retries}: {e}")
            time.sleep(delay * (i + 1))
    raise RuntimeError(f"http_get_json failed: {last}")


def chunked(seq, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
