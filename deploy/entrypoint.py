#!/usr/bin/env python3
"""Cloud Run Job entrypoint: fetch real candles, backtest, publish results.

Everything is configured through environment variables so the same image can
be re-run with different windows without a rebuild:

  CODES           space-separated symbols (default: the six from the README)
  BARS            5m bars per symbol (52992 ~= 6 months)
  INTERVAL        candle interval (default 5m)
  INITIAL_CASH    starting equity (default 1200)
  INVEST_FRAC     weight per position (default 0.2)
  LEVERAGE        default 1
  TAKER / SLIPPAGE   per-side costs (defaults 0.0005 / 0.0005)
  PIVOT_MODES     which entry rules to run (default: all three)
  GCS_BUCKET      results destination; omit to write locally only
  OUTPUT_PREFIX   object prefix inside the bucket (default: runs)
  CACHE_CANDLES   "1" to also upload the fetched candles for offline re-runs
  DATA_SOURCE     exchange | synthetic  (synthetic is for smoke-testing)

Exit code is non-zero if the fetch fails, so a failed run is visible in
Cloud Run's job history instead of silently publishing nothing.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from backtest import data as data_mod          # noqa: E402
from backtest.audit import audit_constraints, check_causality  # noqa: E402
from backtest.causal import build_engine       # noqa: E402
from backtest.config import DEFAULT_CODES, BacktestConfig      # noqa: E402
from backtest.engine import run_backtest       # noqa: E402
from backtest.metrics import compute_metrics, format_report    # noqa: E402


def env_str(key, default):
    return os.environ.get(key, default)


def env_float(key, default):
    return float(os.environ.get(key, default))


def env_int(key, default):
    return int(os.environ.get(key, default))


def log(msg):
    """Unbuffered stdout so lines reach Cloud Logging as they happen."""
    print(msg, flush=True)


def upload(bucket_name, blob_path, content, content_type="text/plain"):
    from google.cloud import storage

    client = storage.Client()
    blob = client.bucket(bucket_name).blob(blob_path)
    blob.upload_from_string(content, content_type=content_type)
    return f"gs://{bucket_name}/{blob_path}"


def main() -> int:
    codes = env_str("CODES", " ".join(DEFAULT_CODES)).split()
    bars = env_int("BARS", 52_992)              # ~6 months of 5m
    interval = env_str("INTERVAL", "5m")
    source = env_str("DATA_SOURCE", "exchange")
    modes = env_str("PIVOT_MODES", "fractal shift donchian").split()
    bucket = env_str("GCS_BUCKET", "")
    prefix = env_str("OUTPUT_PREFIX", "runs")

    cfg = BacktestConfig(
        codes=codes, interval=interval,
        initial_cash=env_float("INITIAL_CASH", 1200),
        leverage=env_float("LEVERAGE", 1),
        taker_rate=env_float("TAKER", 0.0005),
        slippage=env_float("SLIPPAGE", 0.0005),
        invest_frac=env_float("INVEST_FRAC", 0.2),
    )

    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    log(f"zoneXing backtest job — started {started.isoformat()}")
    log(f"  source={source} codes={len(codes)} bars={bars} interval={interval}")
    log(f"  cash={cfg.initial_cash} invest_frac={cfg.invest_frac} "
        f"cost/side={cfg.round_trip_cost:.4%}")

    # ── data ──────────────────────────────────────────────────────────
    if source == "synthetic":
        data_map = data_mod.synthetic(codes, bars=bars, interval=interval)
        note = "SYNTHETIC — smoke test only, not real market data"
    else:
        log("Fetching candles from exchange...")
        data_map = data_mod.fetch_exchange(codes, interval, bars)
        note = "real candles from exchange API"

    index = next(iter(data_map.values())).index
    n = len(index)
    log(f"  {note}: {len(data_map)} symbols x {n:,} bars "
        f"({index[0]} -> {index[-1]})")

    if bucket and env_str("CACHE_CANDLES", "") == "1":
        for code, df in data_map.items():
            buf = io.StringIO()
            df.to_csv(buf, index_label="timestamp")
            upload(bucket, f"candles/{interval}/{code}.csv", buf.getvalue(), "text/csv")
        log(f"  candles cached to gs://{bucket}/candles/{interval}/")

    # ── run every requested pivot rule ────────────────────────────────
    payload = {
        "run_id": stamp,
        "started_utc": started.isoformat(),
        "data_note": note,
        "window": {"start": str(index[0]), "end": str(index[-1]), "bars": n},
        "config": {
            "codes": codes, "interval": interval,
            "initial_cash": cfg.initial_cash, "leverage": cfg.leverage,
            "invest_frac": cfg.invest_frac, "taker_rate": cfg.taker_rate,
            "slippage": cfg.slippage,
        },
        "runs": {},
    }
    report_lines = [
        f"zoneXing Trading backtest — {stamp}",
        f"{note}",
        f"{len(data_map)} symbols x {n:,} {interval} bars: {index[0]} -> {index[-1]}",
        f"${cfg.initial_cash:,.0f} start, leverage {cfg.leverage:g}, "
        f"invest_frac {cfg.invest_frac:g}, {cfg.round_trip_cost:.2%} cost per side",
        "",
    ]

    for mode in modes:
        log(f"Running pivot={mode} ...")
        engine = build_engine(mode, invest_frac=1.0)
        signals = engine.generate(data_map)
        result = run_backtest(data_map, signals, cfg, position_adjustment="hold")
        metrics = compute_metrics(result, cfg)

        gate = engine._gate(data_map["BTC-USDT"]["close"].to_numpy(dtype=float))
        audit = audit_constraints(data_map, signals, gate)
        causality = check_causality(engine, data_map, probes=6)

        label = f"{mode} (ARCHIVED — peeks ahead)" if mode == "fractal" else f"{mode} (causal)"
        text = format_report(metrics, f"pivot = {label}")
        log(text)
        log(f"  constraints: {'PASS' if audit['passed'] else 'FAIL'} "
            f"({audit['total_gate_violations']} gate breaches); "
            f"causality: {'CAUSAL' if causality['causal'] else 'LOOKAHEAD'}")

        report_lines += [
            text,
            f"  Constraints     {'PASS' if audit['passed'] else 'FAIL'} — "
            f"BTC traded: {audit['btc_traded']}, "
            f"gate breaches: {audit['total_gate_violations']}",
            f"  Causality       {'CAUSAL' if causality['causal'] else 'LOOKAHEAD'}"
            + ("" if causality["causal"]
               else f" — {causality['lookahead_bars']} bars of future data"),
            "",
        ]
        payload["runs"][mode] = {"metrics": metrics, "audit": audit,
                                 "causality": causality}

        if bucket:
            import pandas as pd
            buf = io.StringIO()
            pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(buf, index=False)
            upload(bucket, f"{prefix}/{stamp}/trades_{mode}.csv", buf.getvalue(), "text/csv")
            eq = io.StringIO()
            result.equity.to_csv(eq, index_label="timestamp")
            upload(bucket, f"{prefix}/{stamp}/equity_{mode}.csv", eq.getvalue(), "text/csv")

    # ── side-by-side summary ──────────────────────────────────────────
    summary = ["Side by side", f"  {'pivot':<12}{'final $':>12}{'return':>11}"
                               f"{'max DD':>10}{'PF':>8}{'win%':>8}{'trades':>9}"]
    for mode in modes:
        m = payload["runs"][mode]["metrics"]
        pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        wr = "n/a" if m["win_rate"] != m["win_rate"] else f"{m['win_rate'] * 100:.1f}"
        summary.append(
            f"  {mode:<12}{m['final_equity']:>12,.0f}{m['total_return'] * 100:>10.2f}%"
            f"{m['max_drawdown'] * 100:>9.2f}%{pf:>8}{wr:>8}{m['trades']:>9,}")
    report = "\n".join(report_lines + summary)
    log("\n" + "\n".join(summary))

    if bucket:
        j = upload(bucket, f"{prefix}/{stamp}/results.json",
                   json.dumps(payload, indent=2, default=str), "application/json")
        r = upload(bucket, f"{prefix}/{stamp}/report.txt", report)
        upload(bucket, f"{prefix}/latest/results.json",
               json.dumps(payload, indent=2, default=str), "application/json")
        upload(bucket, f"{prefix}/latest/report.txt", report)
        log(f"\nResults: {j}\n         {r}")
        log(f"Latest:  gs://{bucket}/{prefix}/latest/report.txt")
    else:
        out = env_str("LOCAL_OUT", "/tmp/zonexing")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "results.json"), "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        with open(os.path.join(out, "report.txt"), "w") as fh:
            fh.write(report)
        log(f"\nNo GCS_BUCKET set — results written to {out}/")

    log("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
