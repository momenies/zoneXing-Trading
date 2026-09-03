"""Optional Telegram alerts. Silent no-op when the env vars are unset."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("zonexing.notify")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as resp:
                json.load(resp)
        except Exception as exc:  # never let alerting break trading
            log.warning("telegram alert failed: %s", exc)
