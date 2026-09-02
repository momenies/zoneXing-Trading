"""Backtest the zoneXing strategy with the SAME engine that trades live.

The point of this tool is that it imports ``live.engine`` rather than
re-implementing the rules, so a result here is a claim about the deployed bot,
not about a separate research script.

    # fetch real 5m history from the exchange and backtest the live rule
    python -m tools.backtest --exchange mexc --days 180

    # compare the causal rules against the archived look-ahead engine
    python -m tools.backtest --exchange mexc --days 180 --compare

    # offline, from cached/exported CSVs (ts,open,high,low,close,volume)
    python -m tools.backtest --csv-dir data --compare

Downloaded candles are cached under ``data/`` so repeated runs are free.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live.engine import (ACTION_EXIT, FLAT, LONG, SHORT, LiveSignalEngine,  # noqa: E402
                         SymbolState)

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


# ── data ────────────────────────────────────────────────────────────────

def cache_path(exchange: str, code: str, tf: str) -> Path:
    return ROOT / "data" / f"{exchange}_{code.replace('/', '-')}_{tf}.csv"


def fetch_history(exchange_id: str, code: str, timeframe: str, days: int,
                  market_type: str = "swap") -> pd.DataFrame:
    """Paginated OHLCV download via ccxt, cached to data/*.csv."""
    import ccxt

    path = cache_path(exchange_id, code, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    tf_ms = TF_MS[timeframe]
    since = int((time.time() - days * 86400) * 1000)

    cached = pd.DataFrame()
    if path.is_file():
        cached = pd.read_csv(path, index_col=0)
        cached.index = cached.index.astype("int64")
        if len(cached) and cached.index[-1] >= since:
            since = int(cached.index[-1]) + tf_ms

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 30_000,
                                     "options": {"defaultType": market_type}})
    ex.load_markets()
    base, _, quote = code.partition("-")
    symbol = f"{base}/{quote}:{quote}" if market_type == "swap" else f"{base}/{quote}"

    rows: List[list] = []
    now_ms = int(time.time() * 1000)
    while since < now_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + tf_ms
        print(f"\r  {code}: {len(rows)} bars…", end="", flush=True)
        if len(batch) < 2:
            break
    print()

    fresh = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if len(fresh):
        fresh["ts"] = fresh["ts"].astype("int64")
        fresh = fresh.set_index("ts")
    df = pd.concat([cached, fresh])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(path)
    return df


def load_csv_dir(directory: Path, codes: List[str]) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for code in codes:
        matches = sorted(directory.glob(f"*{code.replace('/', '-')}*.csv"))
        if not matches:
            raise SystemExit(f"no CSV for {code} in {directory}")
        df = pd.read_csv(matches[0], index_col=0)
        df.index = df.index.astype("int64")
        out[code] = df.sort_index()
    return out


def align(data: Dict[str, pd.DataFrame], gate_code: str) -> Dict[str, pd.DataFrame]:
    """Keep only timestamps present in every series, so the gate always applies."""
    common = None
    for df in data.values():
        common = df.index if common is None else common.intersection(df.index)
    if common is None or not len(common):
        raise SystemExit("series do not overlap in time")
    return {c: df.loc[common] for c, df in data.items()}


# ── simulation ──────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    side: float
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    qty: float
    pnl: float
    reason: str

    @property
    def bars(self) -> int:
        return max(1, (self.exit_ts - self.entry_ts) // TF_MS["5m"])


@dataclass
class Result:
    label: str
    equity: np.ndarray = field(default_factory=lambda: np.array([]))
    trades: List[Trade] = field(default_factory=list)
    fees: float = 0.0
    gate_violations: int = 0
    initial: float = 1200.0

    def summary(self) -> dict:
        eq = self.equity
        final = float(eq[-1]) if len(eq) else self.initial
        ret = (final / self.initial - 1) * 100
        peak = np.maximum.accumulate(eq) if len(eq) else np.array([self.initial])
        dd = float(((eq - peak) / peak).min() * 100) if len(eq) else 0.0
        pnls = np.array([t.pnl for t in self.trades], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else float("inf")
        return {
            "label": self.label,
            "return_pct": ret,
            "max_dd_pct": dd,
            "profit_factor": pf,
            "win_pct": (len(wins) / len(pnls) * 100) if len(pnls) else 0.0,
            "trades": len(self.trades),
            "avg_bars": float(np.mean([t.bars for t in self.trades])) if self.trades else 0.0,
            "fees": self.fees,
            "fees_pct_of_capital": self.fees / self.initial * 100,
            "gate_violations": self.gate_violations,
        }


def simulate(engine: LiveSignalEngine, data: Dict[str, pd.DataFrame], gate_code: str,
             label: str, initial: float = 1200.0, invest_frac: float = 0.2,
             leverage: float = 1.0, taker: float = 0.0005,
             slippage: float = 0.0005) -> Result:
    """Walk every bar through ``engine.decide_at`` — the live decision rule."""
    codes = [c for c in data if c != gate_code]
    gate = engine.gate_series(data[gate_code]["close"].to_numpy(dtype=float))
    ind = {c: engine.indicators(data[c]) for c in codes}
    states = {c: SymbolState() for c in codes}
    open_at: Dict[str, dict] = {}

    n = len(data[gate_code])
    res = Result(label=label, initial=initial)
    equity = initial
    curve = np.empty(n, dtype=float)

    for i in range(n):
        g = float(gate[i])
        unrealised = 0.0
        for code in codes:
            st = states[code]
            k = ind[code]
            if i < engine.min_bars:
                continue
            dec = engine.decide_at(code, k, i, g, st)

            if dec.action == ACTION_EXIT and st.position.side != FLAT:
                pos = st.position
                fill = dec.price * (1 - slippage) if pos.side > 0 else dec.price * (1 + slippage)
                fee = fill * pos.qty * taker
                pnl = (fill - pos.entry_price) * pos.qty * pos.side - fee
                equity += pnl
                res.fees += fee
                res.trades.append(Trade(code, pos.side, pos.entry_ts, dec.bar_ts,
                                        pos.entry_price, fill, pos.qty, pnl, dec.reason))
                engine.close_position(st, dec.bar_ts)
                open_at.pop(code, None)

            elif dec.side != FLAT and st.position.side == FLAT:
                fill = dec.price * (1 + slippage) if dec.side > 0 else dec.price * (1 - slippage)
                qty = (equity * invest_frac * leverage) / fill
                fee = fill * qty * taker
                equity -= fee
                res.fees += fee
                engine.open_position(st, dec.side, fill, dec.bar_ts, qty)
                open_at[code] = {"entry": fill}

            pos = st.position
            if pos.side != FLAT:
                unrealised += (k.cl[i] - pos.entry_price) * pos.qty * pos.side
                if (pos.side == LONG and g == -1.0) or (pos.side == SHORT and g == 1.0):
                    res.gate_violations += 1
        curve[i] = equity + unrealised

    res.equity = curve[engine.min_bars:] if n > engine.min_bars else curve
    return res


def simulate_archived(data: Dict[str, pd.DataFrame], gate_code: str, initial: float = 1200.0,
                      invest_frac: float = 0.2, leverage: float = 1.0,
                      taker: float = 0.0005, slippage: float = 0.0005) -> Result:
    """Reference run of the archived look-ahead engine, for comparison only.

    Its entries use bars that had not printed yet, so this row is an upper
    bound on a fantasy, never a live expectation.
    """
    from zoneXing_Trading_signal_engine import SignalEngine

    sig = SignalEngine(invest_frac=1.0).generate(dict(data))
    res = Result(label="archived fractal (LOOK-AHEAD — not achievable)", initial=initial)
    equity = initial
    n = len(data[gate_code])
    curve = np.empty(n, dtype=float)
    open_pos: Dict[str, dict] = {}

    for i in range(n):
        unrealised = 0.0
        for code, series in sig.items():
            if code == gate_code:
                continue
            s = float(series.to_numpy()[i])
            px = float(data[code]["close"].to_numpy()[i])
            ts = int(data[code].index[i])
            held = open_pos.get(code)
            want = np.sign(s)

            if held and want != held["side"]:
                fill = px * (1 - slippage) if held["side"] > 0 else px * (1 + slippage)
                fee = fill * held["qty"] * taker
                pnl = (fill - held["entry"]) * held["qty"] * held["side"] - fee
                equity += pnl
                res.fees += fee
                res.trades.append(Trade(code, held["side"], held["ts"], ts, held["entry"],
                                        fill, held["qty"], pnl, "signal-flat"))
                open_pos.pop(code)
                held = None
            if not held and want != 0:
                fill = px * (1 + slippage) if want > 0 else px * (1 - slippage)
                qty = (equity * invest_frac * leverage) / fill
                fee = fill * qty * taker
                equity -= fee
                res.fees += fee
                open_pos[code] = {"side": want, "entry": fill, "qty": qty, "ts": ts}
            if code in open_pos:
                p = open_pos[code]
                unrealised += (px - p["entry"]) * p["qty"] * p["side"]
        curve[i] = equity + unrealised
    res.equity = curve
    return res


# ── reporting ───────────────────────────────────────────────────────────

def report(results: List[Result]) -> None:
    rows = [r.summary() for r in results]
    head = f"{'strategy':<44} {'return':>9} {'maxDD':>8} {'PF':>7} {'win%':>7} {'trades':>7} {'fees':>9}"
    print("\n" + head)
    print("-" * len(head))
    for s in rows:
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        print(f"{s['label']:<44} {s['return_pct']:>8.2f}% {s['max_dd_pct']:>7.2f}% "
              f"{pf:>7} {s['win_pct']:>6.1f}% {s['trades']:>7} {s['fees']:>8.2f}")
    print()
    for s in rows:
        if s["gate_violations"]:
            print(f"  ⚠ {s['label']}: {s['gate_violations']} gate violations")
        if s["trades"]:
            print(f"  {s['label']}: avg hold {s['avg_bars']:.1f} bars, "
                  f"fees = {s['fees_pct_of_capital']:.2f}% of starting capital")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.backtest")
    ap.add_argument("--exchange", default="okx")
    ap.add_argument("--market-type", default="swap", choices=["swap", "spot"])
    ap.add_argument("--gate-symbol", default="BTC-USDT")
    ap.add_argument("--symbols", default="ETH-USDT,BNB-USDT,SOL-USDT,XRP-USDT,DOGE-USDT")
    ap.add_argument("--timeframe", default="5m", choices=sorted(TF_MS))
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--csv-dir", help="load from CSVs instead of the network")
    ap.add_argument("--mode", default="donchian",
                    choices=["donchian", "fractal_confirmed"])
    ap.add_argument("--compare", action="store_true",
                    help="run both causal modes plus the archived look-ahead engine")
    ap.add_argument("--initial", type=float, default=1200.0)
    ap.add_argument("--invest-frac", type=float, default=0.2)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--taker", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.0005)
    args = ap.parse_args(argv)

    codes = [args.gate_symbol] + [c.strip().upper() for c in args.symbols.split(",") if c.strip()]
    codes = list(dict.fromkeys(codes))

    if args.csv_dir:
        data = load_csv_dir(Path(args.csv_dir), codes)
    else:
        print(f"fetching {args.days}d of {args.timeframe} from {args.exchange}…")
        data = {c: fetch_history(args.exchange, c, args.timeframe, args.days,
                                 args.market_type) for c in codes}
    data = align(data, args.gate_symbol)
    n = len(data[args.gate_symbol])
    span_days = n * TF_MS[args.timeframe] / 86_400_000
    print(f"{n} aligned bars ≈ {span_days:.0f} days, {len(codes) - 1} tradable symbols")

    sim_kw = dict(initial=args.initial, invest_frac=args.invest_frac,
                  leverage=args.leverage, taker=args.taker, slippage=args.slippage)
    modes = ["donchian", "fractal_confirmed"] if args.compare else [args.mode]
    results = []
    for mode in modes:
        eng = LiveSignalEngine(pivot_mode=mode, timeframe_ms=TF_MS[args.timeframe],
                               allow_short=args.market_type != "spot")
        results.append(simulate(eng, data, args.gate_symbol, f"live engine [{mode}]", **sim_kw))
    if args.compare:
        results.append(simulate_archived(data, args.gate_symbol, **sim_kw))

    report(results)
    if args.compare:
        print("\nThe archived row is shown only to size the look-ahead. Plan against the "
              "causal rows — those are what the bot can actually trade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
