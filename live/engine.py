"""Causal (look-ahead-free) live signal engine for zoneXing Trading.

The archived backtest engine (`zoneXing_Trading_signal_engine.py`) detects swing
pivots with ``lo[i - p : i + p + 1]`` — that window reaches ``p_bars`` bars into
the FUTURE, so its entries cannot be reproduced in real time.  This module keeps
every other rule identical (gate, EMA confirmation, ATR trail, fixed TP/SL,
gate-flip exit, min-hold, cooldown) but decides using closed bars only:

  * ``donchian``          – dip when the low is the lowest of the trailing
                            ``2*p_bars+1`` bars (the strategy file's own
                            ``_donchian_pivots``, causal, zero delay).
  * ``fractal_confirmed`` – the original fractal, but acted on ``p_bars`` bars
                            later, once the window is complete (faithful shape,
                            delayed fill).

The engine is a per-symbol state machine driven by *closed* bars.  It only
emits a decision for the newest closed bar; the trader turns that into orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

import numpy as np
import pandas as pd

FLAT, LONG, SHORT = 0.0, 1.0, -1.0

ACTION_HOLD = "hold"
ACTION_ENTER_LONG = "enter_long"
ACTION_ENTER_SHORT = "enter_short"
ACTION_EXIT = "exit"


@dataclass
class Position:
    side: float = FLAT             # +1 long, -1 short, 0 flat
    entry_price: float = 0.0
    entry_ts: int = 0              # ms timestamp of the entry bar
    best_price: float = 0.0        # highest high since entry (long trail)
    worst_price: float = 0.0       # lowest low since entry (short trail)
    qty: float = 0.0               # base-asset amount actually filled


@dataclass
class SymbolState:
    position: Position = field(default_factory=Position)
    last_exit_ts: int = 0          # ms timestamp of the last exit bar
    last_bar_ts: int = 0           # newest bar already processed

    def to_dict(self) -> dict:
        return {"position": asdict(self.position),
                "last_exit_ts": self.last_exit_ts,
                "last_bar_ts": self.last_bar_ts}

    @classmethod
    def from_dict(cls, d: dict) -> "SymbolState":
        return cls(position=Position(**d.get("position", {})),
                   last_exit_ts=int(d.get("last_exit_ts", 0)),
                   last_bar_ts=int(d.get("last_bar_ts", 0)))


@dataclass
class Indicators:
    """Vectorised inputs for the decision rule, computed once per series."""
    ts: np.ndarray
    hi: np.ndarray
    lo: np.ndarray
    cl: np.ndarray
    fast: np.ndarray
    slow: np.ndarray
    atr: np.ndarray
    dip: np.ndarray
    peak: np.ndarray

    def __len__(self) -> int:
        return len(self.cl)


@dataclass
class Decision:
    symbol: str
    action: str = ACTION_HOLD
    reason: str = ""
    price: float = 0.0             # close of the decision bar
    bar_ts: int = 0
    gate: float = 0.0
    side: float = FLAT             # side to open (entries only)


class LiveSignalEngine:
    """Streaming, causal version of the archived SignalEngine."""

    def __init__(
        self,
        gate_bars: int = 48,
        gate_th: float = 0.002,
        p_bars: int = 8,
        fast_ema: int = 8,
        slow_ema: int = 21,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        sl_pct: float = 0.02,
        tp_pct: float = 0.04,
        cooldown: int = 3,
        min_hold: int = 3,
        pivot_mode: str = "donchian",
        timeframe_ms: int = 300_000,
        allow_short: bool = True,
    ):
        self.gate_bars = int(gate_bars)
        self.gate_th = float(gate_th)
        self.p_bars = int(p_bars)
        self.fast_ema = int(fast_ema)
        self.slow_ema = int(slow_ema)
        self.atr_period = int(atr_period)
        self.atr_sl_mult = float(atr_sl_mult)
        self.sl_pct = float(sl_pct)
        self.tp_pct = float(tp_pct)
        self.cooldown = int(cooldown)
        self.min_hold = int(min_hold)
        self.pivot_mode = pivot_mode
        self.timeframe_ms = int(timeframe_ms)
        self.allow_short = bool(allow_short)

    # ── indicators (identical maths to the backtest engine) ─────────────

    @staticmethod
    def _ema(arr: np.ndarray, span: int) -> np.ndarray:
        return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy(dtype=float)

    @staticmethod
    def _atr(hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, period: int) -> np.ndarray:
        prev_cl = np.roll(cl, 1)
        prev_cl[0] = cl[0]
        tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev_cl), np.abs(lo - prev_cl)))
        return pd.Series(tr).rolling(period, min_periods=period).mean().to_numpy(dtype=float)

    def gate_series(self, btc_close: np.ndarray) -> np.ndarray:
        """BTC 4h gate from 5m closes: +1 up, -1 down, 0 flat/dead-band."""
        sma = (pd.Series(btc_close)
               .rolling(self.gate_bars, min_periods=self.gate_bars)
               .mean().to_numpy(dtype=float))
        gate = np.zeros(len(btc_close), dtype=float)
        with np.errstate(invalid="ignore"):
            up = btc_close > (1.0 + self.gate_th) * sma
            dn = btc_close < (1.0 - self.gate_th) * sma
        gate[up & np.isfinite(sma)] = 1.0
        gate[dn & np.isfinite(sma)] = -1.0
        return gate

    def _pivots(self, hi: np.ndarray, lo: np.ndarray):
        """Causal dip/peak flags — never reads a bar after the decision bar."""
        p = self.p_bars
        if self.pivot_mode == "fractal_confirmed":
            # The fractal centred on bar i-p is only knowable at bar i.
            n = len(hi)
            w = 2 * p + 1
            dip = np.zeros(n, dtype=bool)
            peak = np.zeros(n, dtype=bool)
            for i in range(w - 1, n):
                c = i - p                       # centre of the completed window
                dip[i] = lo[c] == lo[i - w + 1: i + 1].min()
                peak[i] = hi[c] == hi[i - w + 1: i + 1].max()
            return dip, peak
        # donchian: trailing extreme, decided on the bar itself
        w = 2 * p + 1
        roll_lo = pd.Series(lo).rolling(w, min_periods=w).min().to_numpy(dtype=float)
        roll_hi = pd.Series(hi).rolling(w, min_periods=w).max().to_numpy(dtype=float)
        dip = lo <= roll_lo
        peak = hi >= roll_hi
        return dip, peak

    @property
    def min_bars(self) -> int:
        return max(self.gate_bars, self.slow_ema, self.atr_period, 2 * self.p_bars + 1) + 5

    # ── per-bar decision ────────────────────────────────────────────────

    def indicators(self, df: pd.DataFrame) -> Indicators:
        """Compute every decision input for a whole series in one pass.

        Both callers use this: ``step`` (live, newest bar) and the backtester
        (every bar), so a backtest exercises the same rule that trades.
        """
        hi = df["high"].to_numpy(dtype=float)
        lo = df["low"].to_numpy(dtype=float)
        cl = df["close"].to_numpy(dtype=float)
        dip, peak = self._pivots(hi, lo)
        return Indicators(
            ts=df.index.to_numpy(dtype="int64"), hi=hi, lo=lo, cl=cl,
            fast=self._ema(cl, self.fast_ema), slow=self._ema(cl, self.slow_ema),
            atr=self._atr(hi, lo, cl, self.atr_period), dip=dip, peak=peak,
        )

    def decide_at(
        self,
        symbol: str,
        ind: Indicators,
        i: int,
        gate: float,
        state: SymbolState,
        mutate: bool = True,
    ) -> Decision:
        """The decision rule for bar ``i``. Reads no index above ``i``.

        Exit checks run first (mirroring the archived backtest loop), then the
        gate-flip exit, then entry.
        """
        d = Decision(symbol=symbol, gate=gate)
        bar_ts = int(ind.ts[i])
        d.price = float(ind.cl[i])
        d.bar_ts = bar_ts

        hi, lo, cl = ind.hi, ind.lo, ind.cl
        fast, slow, atr = ind.fast, ind.slow, ind.atr
        dip, peak = ind.dip, ind.peak

        pos = state.position
        tf = self.timeframe_ms
        bars_since_entry = (bar_ts - pos.entry_ts) // tf if pos.side != FLAT else 0
        bars_since_exit = (bar_ts - state.last_exit_ts) // tf if state.last_exit_ts else 10**9
        in_cooldown = bars_since_exit < self.cooldown

        # ---- EXIT (trail / TP / SL, gated by min_hold) ----
        if pos.side == LONG:
            best = max(pos.best_price, float(hi[i]))
            if mutate:
                pos.best_price = best
            pnl_pct = (cl[i] - pos.entry_price) / pos.entry_price
            trail = (best - cl[i]) > self.atr_sl_mult * atr[i] if np.isfinite(atr[i]) else False
            if bars_since_entry >= self.min_hold and (trail or pnl_pct >= self.tp_pct
                                                      or pnl_pct <= -self.sl_pct):
                d.action, d.reason = ACTION_EXIT, (
                    "take-profit" if pnl_pct >= self.tp_pct else
                    "stop-loss" if pnl_pct <= -self.sl_pct else "atr-trail")
                if mutate:
                    state.last_exit_ts = bar_ts
                    state.last_bar_ts = bar_ts
                return d
        elif pos.side == SHORT:
            worst = min(pos.worst_price, float(lo[i]))
            if mutate:
                pos.worst_price = worst
            pnl_pct = (pos.entry_price - cl[i]) / pos.entry_price
            trail = (cl[i] - worst) > self.atr_sl_mult * atr[i] if np.isfinite(atr[i]) else False
            if bars_since_entry >= self.min_hold and (trail or pnl_pct >= self.tp_pct
                                                      or pnl_pct <= -self.sl_pct):
                d.action, d.reason = ACTION_EXIT, (
                    "take-profit" if pnl_pct >= self.tp_pct else
                    "stop-loss" if pnl_pct <= -self.sl_pct else "atr-trail")
                if mutate:
                    state.last_exit_ts = bar_ts
                    state.last_bar_ts = bar_ts
                return d

        # ---- GATE-FLIP EXIT (hard constraint: never oppose the gate) ----
        if (pos.side == LONG and gate == -1.0) or (pos.side == SHORT and gate == 1.0):
            d.action, d.reason = ACTION_EXIT, "gate-flip"
            if mutate:
                state.last_exit_ts = bar_ts
                state.last_bar_ts = bar_ts
            return d

        # ---- ENTRY ----
        if pos.side == FLAT and not in_cooldown:
            if gate == 1.0 and dip[i] and fast[i] > slow[i]:
                d.action, d.side, d.reason = ACTION_ENTER_LONG, LONG, "gate-up + dip + ema-up"
            elif gate == -1.0 and peak[i] and fast[i] < slow[i]:
                if self.allow_short:
                    d.action, d.side, d.reason = ACTION_ENTER_SHORT, SHORT, "gate-down + peak + ema-down"
                else:
                    d.reason = "short signal skipped (spot market is long-only)"
            elif gate == 0.0:
                d.reason = "gate flat (dead-band) — standing aside"
            else:
                d.reason = "no entry trigger"
        elif in_cooldown:
            d.reason = f"cooldown ({bars_since_exit}/{self.cooldown} bars)"
        else:
            d.reason = f"holding {'long' if pos.side == LONG else 'short'} " \
                       f"({bars_since_entry} bars)"

        if mutate:
            state.last_bar_ts = bar_ts
        return d

    def step(
        self,
        symbol: str,
        df: pd.DataFrame,
        gate: float,
        state: SymbolState,
        mutate: bool = True,
    ) -> Decision:
        """Evaluate the newest CLOSED bar of ``df``.

        ``df`` must hold closed bars only, indexed by open-time in ms, with
        columns open/high/low/close.
        """
        n = len(df)
        if n < self.min_bars:
            return Decision(symbol=symbol, gate=gate,
                            reason=f"warming up ({n}/{self.min_bars} bars)")
        return self.decide_at(symbol, self.indicators(df), n - 1, gate, state, mutate)

    # ── helpers used by the trader ──────────────────────────────────────

    def open_position(self, state: SymbolState, side: float, price: float,
                      bar_ts: int, qty: float) -> None:
        state.position = Position(side=side, entry_price=price, entry_ts=bar_ts,
                                  best_price=price, worst_price=price, qty=qty)

    def close_position(self, state: SymbolState, bar_ts: int) -> None:
        state.position = Position()
        state.last_exit_ts = bar_ts

    def protective_levels(self, side: float, entry: float) -> Dict[str, float]:
        """Exchange-side reduce-only SL/TP prices for a fresh position."""
        if side == LONG:
            return {"sl": entry * (1 - self.sl_pct), "tp": entry * (1 + self.tp_pct)}
        return {"sl": entry * (1 + self.sl_pct), "tp": entry * (1 - self.tp_pct)}
