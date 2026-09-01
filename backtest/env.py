"""Minimal .env loader — no external dependency.

Reads KEY=VALUE lines from a .env file next to the repo root and returns them.
Real process environment variables always win, so a shell export overrides the
file rather than the other way round.

Nothing here ever prints a secret: use ``describe()`` to show what is
configured without revealing the values.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

_SECRET_HINTS = ("KEY", "SECRET", "PASSPHRASE", "TOKEN", "PASSWORD")


def load_env(path: Optional[str] = None) -> Dict[str, str]:
    """Parse .env into a dict, without mutating os.environ."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    values: Dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if val:
                    values[key.strip()] = val
    # a real environment variable beats the file
    for key in list(values) + [
        "BINANCE_API_KEY", "BINANCE_API_SECRET",
        "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE", "EXCHANGE_VENUE",
    ]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def describe(values: Dict[str, str]) -> str:
    """Report which credentials are set, WITHOUT revealing them."""
    names = ["BINANCE_API_KEY", "OKX_API_KEY", "OKX_PASSPHRASE"]
    found = [n for n in names if values.get(n)]
    if not found:
        return "no API keys configured (fine — candle endpoints are public)"
    return "configured: " + ", ".join(f"{n}=***{values[n][-4:]}" for n in found)


def redact(text: str, values: Dict[str, str]) -> str:
    """Strip any known secret out of a string before it is displayed."""
    for key, val in values.items():
        if val and any(h in key.upper() for h in _SECRET_HINTS) and len(val) > 4:
            text = text.replace(val, f"***{val[-4:]}")
    return text
