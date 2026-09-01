#!/usr/bin/env python3
"""Run a local backtest of the zoneXing Trading signal engine.

Examples
--------
    # real candles you already have on disk (one CSV per symbol)
    python3 run_backtest.py --source csv --csv-dir ./data

    # pull real candles (needs network access to OKX / Binance)
    python3 run_backtest.py --source exchange --bars 60000

    # reproducible simulated market — mechanics only, not evidence of edge
    python3 run_backtest.py --source synthetic --bars 60000

    # archived pivot rule vs. the two non-peeking variants
    python3 run_backtest.py --source synthetic --compare
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest import data as data_mod                      # noqa: E402
from backtest.audit import audit_constraints, check_causality  # noqa: E402
from backtest.causal import PIVOT_MODES, build_engine      # noqa: E402
from backtest.config import DEFAULT_CODES, BacktestConfig  # noqa: E402
from backtest.engine import run_backtest                   # noqa: E402
from backtest.env import describe, load_env, redact        # noqa: E402
from backtest.metrics import compute_metrics, format_report  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("data")
    src.add_argument("--source", choices=["csv", "exchange", "synthetic"],
                     default="synthetic")
    src.add_argument("--csv-dir", default="./data", help="dir of <CODE>.csv files")
    src.add_argument("--codes", nargs="+", default=DEFAULT_CODES)
    src.add_argument("--bars", type=int, default=60_000,
                     help="bars to fetch/generate (ignored for --source csv)")
    src.add_argument("--interval", default="5m")
    src.add_argument("--start", default=None, help="window start, e.g. 2025-06-01")
    src.add_argument("--end", default=None)
    src.add_argument("--seed", type=int, nargs="+", default=[7],
                     help="synthetic RNG seed(s); several seeds run a robustness sweep")
    src.add_argument("--synthetic-start", default="2025-06-01",
                     help="first timestamp for --source synthetic")
    src.add_argument("--save-csv", default=None, metavar="DIR",
                     help="write the loaded candles to DIR as <CODE>.csv so a "
                          "fetch can be cached and re-run offline")

    strat = p.add_argument_group("strategy")
    strat.add_argument("--pivot", choices=list(PIVOT_MODES), default="fractal",
                       help="fractal = archived rule (peeks ahead); "
                            "shift/donchian = non-peeking")
    strat.add_argument("--compare", action="store_true",
                       help="run every pivot mode and print them side by side")
    strat.add_argument("--gate-bars", type=int, default=48)
    strat.add_argument("--gate-th", type=float, default=0.002)
    strat.add_argument("--p-bars", type=int, default=8)
    strat.add_argument("--fast-ema", type=int, default=8)
    strat.add_argument("--slow-ema", type=int, default=21)
    strat.add_argument("--atr-period", type=int, default=14)
    strat.add_argument("--atr-sl-mult", type=float, default=1.5)
    strat.add_argument("--sl-pct", type=float, default=0.02)
    strat.add_argument("--tp-pct", type=float, default=0.04)
    strat.add_argument("--cooldown", type=int, default=3)
    strat.add_argument("--min-hold", type=int, default=3)

    ex = p.add_argument_group("execution")
    ex.add_argument("--initial-cash", type=float, default=1200.0)
    ex.add_argument("--leverage", type=float, default=1.0)
    ex.add_argument("--taker", type=float, default=0.0005)
    ex.add_argument("--maker", type=float, default=0.0002)
    ex.add_argument("--slippage", type=float, default=0.0005)
    ex.add_argument("--invest-frac", type=float, default=0.2)
    ex.add_argument("--funding-8h", type=float, default=0.0,
                    help="funding per 8h on open notional (>0 = you pay)")
    ex.add_argument("--position-adjustment", choices=["hold", "rebalance"],
                    default="hold")

    out = p.add_argument_group("output")
    out.add_argument("--no-audit", action="store_true")
    out.add_argument("--causality-probes", type=int, default=8,
                     help="0 disables the lookahead check")
    out.add_argument("--json", default=None, help="write results to this JSON file")
    out.add_argument("--trades-csv", default=None)
    return p.parse_args(argv)


def load_data(args, seed=None):
    seed = args.seed[0] if seed is None else seed
    if args.source == "csv":
        dm = data_mod.load_csv_dir(args.csv_dir, args.codes)
        note = f"real candles from {args.csv_dir}"
    elif args.source == "exchange":
        dm = data_mod.fetch_exchange(args.codes, args.interval, args.bars)
        note = "real candles from exchange API"
    else:
        dm = data_mod.synthetic(args.codes, bars=args.bars,
                                interval=args.interval, seed=seed,
                                start=args.synthetic_start)
        note = (f"SYNTHETIC candles (seed={seed}) — mechanics only, "
                f"NOT evidence of real edge")
    if args.start or args.end:
        dm = data_mod.slice_window(dm, args.start, args.end)
        dm = data_mod.align(dm)
    if args.save_csv:
        os.makedirs(args.save_csv, exist_ok=True)
        for code, df in dm.items():
            df.to_csv(os.path.join(args.save_csv, f"{code}.csv"),
                      index_label="timestamp")
        print(f"Candles cached to {args.save_csv}/")
    return dm, note


def strategy_params(args):
    return dict(
        gate_bars=args.gate_bars, gate_th=args.gate_th, p_bars=args.p_bars,
        fast_ema=args.fast_ema, slow_ema=args.slow_ema,
        atr_period=args.atr_period, atr_sl_mult=args.atr_sl_mult,
        sl_pct=args.sl_pct, tp_pct=args.tp_pct,
        cooldown=args.cooldown, min_hold=args.min_hold,
        invest_frac=1.0,  # portfolio weighting is applied by BacktestConfig
    )


def run_one(pivot_mode, data_map, args, cfg):
    engine = build_engine(pivot_mode, **strategy_params(args))
    signals = engine.generate(data_map)
    result = run_backtest(data_map, signals, cfg,
                          position_adjustment=args.position_adjustment)
    metrics = compute_metrics(result, cfg)

    audit = None
    if not args.no_audit:
        gate = engine._gate(data_map["BTC-USDT"]["close"].to_numpy(dtype=float)) \
            if "BTC-USDT" in data_map else np.zeros(len(next(iter(data_map.values()))))
        audit = audit_constraints(data_map, signals, gate)
    return engine, result, metrics, audit


def print_audit(audit):
    print("\n── Constraint audit " + "─" * 42)
    status = "PASS" if audit["passed"] else "FAIL"
    print(f"  BTC never traded        {'yes' if not audit['btc_traded'] else 'NO'}"
          f"   (max |signal| = {audit['btc_max_abs_signal']:.3f})")
    print(f"  Gate-direction breaches {audit['total_gate_violations']}")
    for code, count in audit["gate_violations"].items():
        if count:
            print(f"      {code}: {count}")
    print(f"  Result                  {status}")


def print_causality(rep):
    print("\n── Lookahead check " + "─" * 43)
    print(f"  Replay probes           {rep['probe_points']}  "
          f"({rep['probes_with_changes']} disagreed with the full run)")
    print(f"  Bars that changed       {rep['changed_bars']:,} / {rep['compared_bars']:,} "
          f"({rep['change_rate'] * 100:.2f}%)")
    if rep["causal"]:
        print("  Result                  CAUSAL — no future information used")
    else:
        print(f"  Lookahead depth         {rep['lookahead_bars']} bars of future data")
        print("  Result                  LOOKAHEAD — signals change once later bars exist")
        for ex in rep["examples"][:3]:
            print(f"      {ex['code']} bar {ex['bar']} "
                  f"({ex['bars_before_prefix_end']} bars before prefix end): "
                  f"live={ex['live_signal']:+.1f} vs hindsight={ex['hindsight_signal']:+.1f}")


def sweep(args, cfg, modes):
    """Run every mode across several synthetic seeds and summarise the spread."""
    import statistics

    per_mode = {m: [] for m in modes}
    for seed in args.seed:
        data_map, _ = load_data(args, seed=seed)
        for mode in modes:
            _, _, metrics, audit = run_one(mode, data_map, args, cfg)
            per_mode[mode].append({"seed": seed, "metrics": metrics, "audit": audit})

    print(f"── Robustness sweep over {len(args.seed)} synthetic seeds " + "─" * 22)
    print(f"  {'pivot mode':<12}{'median ret':>13}{'worst':>11}{'best':>11}"
          f"{'median PF':>12}{'seeds +':>9}")
    for mode in modes:
        rets = [r["metrics"]["total_return"] for r in per_mode[mode]]
        pfs = [r["metrics"]["profit_factor"] for r in per_mode[mode]]
        pfs = [x for x in pfs if x == x and x != float("inf")] or [float("nan")]
        wins = sum(1 for r in rets if r > 0)
        print(f"  {mode:<12}{statistics.median(rets) * 100:>12.2f}%"
              f"{min(rets) * 100:>10.2f}%{max(rets) * 100:>10.2f}%"
              f"{statistics.median(pfs):>12.2f}{wins:>6}/{len(rets)}")

    breaches = sum(r["audit"]["total_gate_violations"]
                   for m in modes for r in per_mode[m] if r["audit"])
    btc = any(r["audit"]["btc_traded"]
              for m in modes for r in per_mode[m] if r["audit"])
    print(f"\n  Constraint audit across all runs: "
          f"{'BTC TRADED' if btc else 'BTC never traded'}, "
          f"{breaches} gate breaches")
    return {m: per_mode[m] for m in modes}


def main(argv=None):
    args = parse_args(argv)
    cfg = BacktestConfig(
        codes=args.codes, interval=args.interval,
        initial_cash=args.initial_cash, leverage=args.leverage,
        taker_rate=args.taker, maker_rate=args.maker, slippage=args.slippage,
        invest_frac=args.invest_frac, funding_rate_8h=args.funding_8h,
    )
    modes = list(PIVOT_MODES) if args.compare else [args.pivot]
    if args.source == "exchange":
        print(f"Credentials: {describe(load_env())}")

    print(f"Costs: taker {cfg.taker_rate:.4%} + slippage {cfg.slippage:.4%} "
          f"per side, leverage {cfg.leverage:g}, invest_frac {cfg.invest_frac:g}, "
          f"sizing={args.position_adjustment}\n")

    if len(args.seed) > 1:
        if args.source != "synthetic":
            raise SystemExit("multiple --seed values only apply to --source synthetic")
        payload = {"sweep": sweep(args, cfg, modes)}
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(payload, fh, indent=2, default=str)
            print(f"\nJSON written to {args.json}")
        return 0

    try:
        data_map, note = load_data(args)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"{exc}\nSee data/README.md for the expected CSV layout, or run with "
            f"--source synthetic to exercise the harness without real candles."
        )
    except ConnectionError as exc:
        raise SystemExit(
            redact(str(exc), load_env())
            + "\nNo exchange reachable. Note that candle endpoints are public: an "
            "API key does not help if the connection itself is blocked.\n"
            "Use --source csv with your own candles (see data/README.md), or "
            "--source synthetic."
        )
    n = len(next(iter(data_map.values())))
    print(f"Data: {note}")
    print(f"      {len(data_map)} symbols x {n:,} bars @ {args.interval}\n")

    payload = {"data_note": note, "bars": n, "runs": {}}

    for mode in modes:
        engine, result, metrics, audit = run_one(mode, data_map, args, cfg)
        label = "fractal (ARCHIVED — peeks ahead)" if mode == "fractal" else f"{mode} (causal)"
        print(format_report(metrics, f"pivot = {label}"))
        if audit:
            print_audit(audit)

        causality = None
        if args.causality_probes > 0:
            causality = check_causality(engine, data_map, probes=args.causality_probes)
            print_causality(causality)
        print()

        payload["runs"][mode] = {"metrics": metrics, "audit": audit,
                                 "causality": causality}

        if args.trades_csv and mode == modes[-1]:
            import pandas as pd
            pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(
                args.trades_csv, index=False)
            print(f"Trades written to {args.trades_csv}")

    if args.compare:
        print("── Side by side " + "─" * 46)
        print(f"  {'pivot mode':<12}{'return':>12}{'max DD':>10}{'PF':>8}"
              f"{'win%':>8}{'trades':>9}")
        for mode in modes:
            m = payload["runs"][mode]["metrics"]
            pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
            wr = "n/a" if m["win_rate"] != m["win_rate"] else f"{m['win_rate'] * 100:.1f}"
            print(f"  {mode:<12}{m['total_return'] * 100:>11.2f}%"
                  f"{m['max_drawdown'] * 100:>9.2f}%{pf:>8}{wr:>8}{m['trades']:>9,}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
