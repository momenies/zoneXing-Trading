"""Data sources for the backtest.

Three ways to get OHLCV, in order of preference:

1. ``load_csv_dir``   – real candles you already have on disk (best).
2. ``fetch_exchange`` – pull real candles from OKX / Binance (needs network).
3. ``synthetic``      – reproducible simulated candles, for mechanics testing
                        only. Results from synthetic data say NOTHING about
                        whether the strategy has a real edge.

Every source returns ``Dict[code, DataFrame]`` with columns
``open/high/low/close/volume`` on a shared DatetimeIndex.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .env import load_env

OHLCV = ["open", "high", "low", "close", "volume"]

_INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


# ── 1. CSV on disk ───────────────────────────────────────────────────────

def load_csv_dir(path: str, codes: List[str]) -> Dict[str, pd.DataFrame]:
    """Load ``<path>/<CODE>.csv`` for each code.

    Each CSV needs a timestamp column (``timestamp``/``time``/``date``/
    ``open_time``, epoch ms/s or an ISO string) plus open/high/low/close and
    optionally volume. Column names are matched case-insensitively.
    """
    data_map: Dict[str, pd.DataFrame] = {}
    for code in codes:
        fp = os.path.join(path, f"{code}.csv")
        if not os.path.exists(fp):
            fp_alt = os.path.join(path, f"{code.replace('-', '')}.csv")
            if not os.path.exists(fp_alt):
                raise FileNotFoundError(f"no CSV for {code} in {path}")
            fp = fp_alt
        data_map[code] = _read_one_csv(fp)
    return align(data_map)


def _read_one_csv(fp: str) -> pd.DataFrame:
    df = pd.read_csv(fp)
    df.columns = [str(c).strip().lower() for c in df.columns]

    ts_col = next(
        (c for c in ("timestamp", "time", "date", "datetime", "open_time") if c in df.columns),
        None,
    )
    if ts_col is None:
        raise ValueError(f"{fp}: no timestamp column found (looked for timestamp/time/date/open_time)")

    ts = df[ts_col]
    if pd.api.types.is_numeric_dtype(ts):
        # epoch seconds vs milliseconds
        unit = "ms" if float(ts.iloc[0]) > 1e11 else "s"
        idx = pd.to_datetime(ts, unit=unit, utc=True)
    else:
        idx = pd.to_datetime(ts, utc=True, format="mixed")

    missing = [c for c in OHLCV[:4] if c not in df.columns]
    if missing:
        raise ValueError(f"{fp}: missing column(s) {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0

    out = df[OHLCV].astype(float)
    out.index = pd.DatetimeIndex(idx, name="timestamp")
    return out[~out.index.duplicated(keep="last")].sort_index()


# ── 2. Live exchange fetch ───────────────────────────────────────────────

def fetch_exchange(
    codes: List[str],
    interval: str = "5m",
    bars: int = 20_000,
    end: Optional[pd.Timestamp] = None,
    venue: str = "auto",
) -> Dict[str, pd.DataFrame]:
    """Fetch real candles. Raises ``ConnectionError`` if no venue is reachable."""
    if venue == "auto":
        venue = load_env().get("EXCHANGE_VENUE", "auto")
    venues = ["okx", "binance"] if venue == "auto" else [venue]
    last_err: Optional[Exception] = None
    for v in venues:
        try:
            data_map = {
                code: _fetch_one(v, code, interval, bars, end) for code in codes
            }
            return align(data_map)
        except Exception as exc:  # noqa: BLE001 - try the next venue
            last_err = exc
    raise ConnectionError(f"no exchange reachable ({venues}): {last_err}")


def _http_json(url: str, timeout: int = 30, headers: Optional[dict] = None) -> dict:
    hdrs = {"User-Agent": "zonexing-backtest/1.0"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _venue_headers(venue: str) -> dict:
    """Credentials for candle endpoints.

    Candles are PUBLIC on both venues — no key is required to fetch them. A
    Binance key raises the per-key rate limit, which matters when pulling tens
    of thousands of bars, so it is sent when present. OKX rate-limits public
    market data per IP regardless of authentication, so a key would buy nothing
    there and is deliberately not sent: no reason to put a secret on the wire
    for an endpoint that ignores it.
    """
    env = load_env()
    if venue == "binance" and env.get("BINANCE_API_KEY"):
        return {"X-MBX-APIKEY": env["BINANCE_API_KEY"]}
    return {}


def _fetch_one(venue, code, interval, bars, end) -> pd.DataFrame:
    headers = _venue_headers(venue)
    step_ms = _INTERVAL_MINUTES[interval] * 60_000
    end_ms = int((end or pd.Timestamp.utcnow()).timestamp() * 1000)
    rows: List[List[float]] = []
    cursor = end_ms

    while len(rows) < bars:
        want = min(300 if venue == "okx" else 1000, bars - len(rows))
        if venue == "okx":
            url = (
                "https://www.okx.com/api/v5/market/history-candles"
                f"?instId={code}&bar={interval}&limit={want}&after={cursor}"
            )
            payload = _http_json(url, headers=headers)
            if payload.get("code") != "0":
                raise RuntimeError(f"okx error: {payload.get('msg')}")
            batch = [
                [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in payload.get("data", [])
            ]
        else:
            sym = code.replace("-", "")
            start = cursor - want * step_ms
            url = (
                "https://api.binance.com/api/v3/klines"
                f"?symbol={sym}&interval={interval}&limit={want}"
                f"&startTime={start}&endTime={cursor}"
            )
            batch = [
                [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in _http_json(url, headers=headers)
            ]
        if not batch:
            break
        rows.extend(batch)
        cursor = min(r[0] for r in batch)
        time.sleep(0.12)  # be polite to the venue

    if not rows:
        raise RuntimeError(f"{venue}: no candles returned for {code}")

    df = pd.DataFrame(rows, columns=["ts", *OHLCV])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    df.index.name = "timestamp"
    return df[~df.index.duplicated(keep="last")].sort_index()


# ── 3. Synthetic candles ─────────────────────────────────────────────────

def synthetic(
    codes: List[str],
    bars: int = 60_000,
    interval: str = "5m",
    seed: int = 7,
    start: str = "2025-06-01",
    beta: float = 0.85,
    regime_bars: int = 2_016,
) -> Dict[str, pd.DataFrame]:
    """Reproducible simulated market with BTC-correlated altcoins.

    BTC follows a regime-switching drift (trend up / trend down / chop) so the
    4h gate actually flips; each altcoin is ``beta`` correlated to BTC plus its
    own idiosyncratic noise. Intrabar high/low are drawn around the close.

    This exercises the full pipeline end to end. It is NOT evidence of edge:
    the price process has no real microstructure, and the pivot/pullback
    patterns the strategy trades are not reproduced faithfully by GBM.
    """
    rng = np.random.default_rng(seed)
    step = _INTERVAL_MINUTES[interval]
    idx = pd.date_range(start, periods=bars, freq=f"{step}min", tz="UTC")

    # BTC: piecewise drift regimes, ~0.35% per-bar vol scaled to 5m
    n_regimes = max(1, bars // regime_bars)
    drifts = rng.choice([2.2e-5, -1.8e-5, 0.0], size=n_regimes + 1, p=[0.4, 0.3, 0.3])
    btc_drift = np.repeat(drifts, regime_bars)[:bars]
    btc_vol = 0.0022
    btc_ret = btc_drift + btc_vol * rng.standard_normal(bars)

    data_map: Dict[str, pd.DataFrame] = {}
    base_px = {
        "BTC-USDT": 62_000.0, "ETH-USDT": 2_900.0, "BNB-USDT": 580.0,
        "SOL-USDT": 145.0, "XRP-USDT": 0.58, "DOGE-USDT": 0.12,
    }

    for code in codes:
        if code == "BTC-USDT":
            ret = btc_ret
            vol = btc_vol
        else:
            vol = btc_vol * rng.uniform(1.1, 1.7)
            idio = np.sqrt(max(vol**2 - (beta * btc_vol) ** 2, (0.3 * vol) ** 2))
            ret = beta * btc_ret + idio * rng.standard_normal(bars)

        close = base_px.get(code, 100.0) * np.exp(np.cumsum(ret))
        open_ = np.concatenate([[close[0]], close[:-1]])
        wick = np.abs(rng.standard_normal(bars)) * vol * close
        high = np.maximum(open_, close) + wick * 0.6
        low = np.minimum(open_, close) - np.abs(rng.standard_normal(bars)) * vol * close * 0.6
        volume = rng.lognormal(mean=10.0, sigma=0.5, size=bars)

        data_map[code] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=idx,
        )
    return data_map


# ── shared ───────────────────────────────────────────────────────────────

def align(data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Restrict every symbol to the timestamps all of them share.

    The BTC gate is read positionally against each altcoin, so the frames must
    line up bar-for-bar or the gate would be misapplied.
    """
    if not data_map:
        return data_map
    common = None
    for df in data_map.values():
        common = df.index if common is None else common.intersection(df.index)
    if common is None or len(common) == 0:
        raise ValueError("symbols share no common timestamps")
    return {code: df.loc[common].copy() for code, df in data_map.items()}


def slice_window(
    data_map: Dict[str, pd.DataFrame], start: Optional[str], end: Optional[str]
) -> Dict[str, pd.DataFrame]:
    out = {}
    for code, df in data_map.items():
        sub = df
        if start:
            sub = sub[sub.index >= pd.Timestamp(start, tz="UTC")]
        if end:
            sub = sub[sub.index <= pd.Timestamp(end, tz="UTC")]
        out[code] = sub
    return out
