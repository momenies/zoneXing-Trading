"""Portfolio simulator.

Fill model
----------
A signal produced on bar ``i`` is derived from that bar's CLOSE, so it is
filled at the OPEN of bar ``i+1``. Nothing is ever filled on the bar that
produced it — that would be a one-bar lookahead the live system cannot have.

Position sizing
---------------
``position_adjustment="hold"`` (the README's config) sizes a position once,
when the target weight changes, and then holds the quantity until the target
changes again. ``"rebalance"`` re-sizes every bar to ``weight * equity``,
which on 5m data burns a lot of fees.

Accounting is plain signed-quantity cash accounting, so shorts work without a
special case; ``leverage`` simply permits gross notional above equity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import BacktestConfig

DUST = 1e-12


@dataclass
class Trade:
    code: str
    side: str            # "long" | "short"
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    fees: float
    bars_held: int

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: List[Trade]
    positions: pd.DataFrame          # signed notional per symbol, per bar
    signals: Dict[str, pd.Series]
    config: BacktestConfig
    total_fees: float = 0.0
    metrics: dict = field(default_factory=dict)


class _Book:
    """Per-symbol position state and round-trip bookkeeping."""

    def __init__(self, code: str):
        self.code = code
        self.qty = 0.0
        self.avg_price = 0.0
        self.entry_time: Optional[pd.Timestamp] = None
        self.entry_bar = 0
        self.open_fees = 0.0
        self.gross_pnl = 0.0

    def fill(self, dq: float, price: float, fee: float, ts, bar: int,
             trades: List[Trade]) -> None:
        if abs(dq) < DUST:
            return
        if self.qty == 0.0:
            self.qty, self.avg_price = dq, price
            self.entry_time, self.entry_bar = ts, bar
            self.open_fees, self.gross_pnl = fee, 0.0
            return

        if np.sign(dq) == np.sign(self.qty):          # add to position
            notional = self.avg_price * abs(self.qty) + price * abs(dq)
            self.qty += dq
            self.avg_price = notional / abs(self.qty)
            self.open_fees += fee
            return

        # reducing / closing / flipping
        closed = min(abs(dq), abs(self.qty))
        self.gross_pnl += (price - self.avg_price) * closed * np.sign(self.qty)
        self.open_fees += fee
        remainder = abs(dq) - closed
        side = "long" if self.qty > 0 else "short"
        self.qty += dq

        if abs(self.qty) < DUST or remainder > DUST:
            trades.append(Trade(
                code=self.code, side=side,
                entry_time=self.entry_time, exit_time=ts,
                entry_price=self.avg_price, exit_price=price,
                qty=closed, gross_pnl=self.gross_pnl, fees=self.open_fees,
                bars_held=bar - self.entry_bar,
            ))
            if remainder > DUST:                      # flipped straight through
                self.qty = np.sign(dq) * remainder
                self.avg_price = price
                self.entry_time, self.entry_bar = ts, bar
                self.open_fees, self.gross_pnl = 0.0, 0.0
            else:
                self.qty = 0.0
                self.avg_price = 0.0
                self.entry_time = None
                self.open_fees, self.gross_pnl = 0.0, 0.0


def run_backtest(
    data_map: Dict[str, pd.DataFrame],
    signals: Dict[str, pd.Series],
    config: BacktestConfig,
    position_adjustment: str = "hold",
) -> BacktestResult:
    codes = [c for c in data_map if c in signals]
    index = next(iter(data_map.values())).index
    n = len(index)
    if n < 2:
        raise ValueError("need at least 2 bars to backtest")

    opens = {c: data_map[c]["open"].to_numpy(dtype=float) for c in codes}
    closes = {c: data_map[c]["close"].to_numpy(dtype=float) for c in codes}
    sig = {c: signals[c].to_numpy(dtype=float) for c in codes}

    books = {c: _Book(c) for c in codes}
    trades: List[Trade] = []
    cash = float(config.initial_cash)
    equity = np.empty(n, dtype=float)
    equity[0] = cash
    total_fees = 0.0
    notional_hist = np.zeros((n, len(codes)), dtype=float)

    cost_rate = config.round_trip_cost
    # 5m bars: funding accrues 96 bars per 8h window
    bars_per_funding = 96 if config.interval == "5m" else 1
    funding_per_bar = config.funding_rate_8h / bars_per_funding

    last_target = {c: 0.0 for c in codes}

    for t in range(1, n):
        ts = index[t]
        # equity marked at this bar's open, before any fills
        equity_pre = cash + sum(books[c].qty * opens[c][t] for c in codes)

        for j, c in enumerate(codes):
            target_w = float(sig[c][t - 1]) * config.invest_frac
            book = books[c]
            px = opens[c][t]

            if position_adjustment == "hold":
                changed = abs(target_w - last_target[c]) > 1e-12
                if not changed:
                    notional_hist[t, j] = book.qty * closes[c][t]
                    continue
            last_target[c] = target_w

            target_qty = (target_w * equity_pre * config.leverage) / px
            dq = target_qty - book.qty
            if abs(dq * px) > DUST:
                fee = abs(dq * px) * cost_rate
                cash -= dq * px + fee
                total_fees += fee
                book.fill(dq, px, fee, ts, t, trades)

            notional_hist[t, j] = book.qty * closes[c][t]

        if funding_per_bar:
            gross = sum(abs(books[c].qty) * closes[c][t] for c in codes)
            cash -= gross * funding_per_bar

        equity[t] = cash + sum(books[c].qty * closes[c][t] for c in codes)

    # force-close anything still open on the final bar
    last_ts = index[-1]
    for c in codes:
        book = books[c]
        if abs(book.qty) > DUST:
            px = closes[c][-1]
            fee = abs(book.qty * px) * cost_rate
            cash += book.qty * px - fee
            total_fees += fee
            book.fill(-book.qty, px, fee, last_ts, n - 1, trades)
    equity[-1] = cash

    eq = pd.Series(equity, index=index, name="equity")
    pos = pd.DataFrame(notional_hist, index=index, columns=codes)
    return BacktestResult(
        equity=eq, trades=trades, positions=pos, signals=signals,
        config=config, total_fees=total_fees,
    )
