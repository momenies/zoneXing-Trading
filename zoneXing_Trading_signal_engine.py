"""BTC-gated, 5-minute entry strategy for altcoins — Enhanced v2.

Design principles:
  1. BTC is the MASTER / gate only, NEVER traded.  Gate direction from 4h
     (48 bars of 5m).  No position ever opposes the gate.
  2. Altcoin entries analysed on 5m only.
  3. Trend confirmation via dual EMA (fast/slow) on the altcoin — only enter
     when the local EMA alignment agrees with the gate direction.
  4. Fractal pivot swing detection: a bar is a dip/peak only if it is the
     lowest/highest within ``p_bars`` bars on EACH side, reducing noise.
  5. ATR-based trailing stop rides trends; fixed SL/TP as hard limits.
  6. Cooldown + minimum hold to avoid whipsaw re-entries.
  7. BTC signal is always 0 (never traded).

Parameters (all defaulted so SignalEngine() works unchanged):
  gate_bars      – bars of 5m making the 4h gate window (48).
  gate_th        – buffer around gate SMA to avoid whipsaw (0.002).
  p_bars         – half-window for fractal pivot detection (8).
  fast_ema       – fast EMA period for trend confirmation (8).
  slow_ema       – slow EMA period for trend confirmation (21).
  atr_period     – ATR lookback for trailing stop (14).
  atr_sl_mult    – ATR multiplier for trailing stop (1.5).
  sl_pct         – fixed stop-loss as fraction of entry (0.02 = 2.0%).
  tp_pct         – fixed take-profit as fraction of entry (0.04 = 4.0%).
  cooldown       – bars to wait after exit before re-entering (3).
  min_hold       – minimum bars to hold a position (3).
  invest_frac    – position weight per trade (1.0).

Pure numpy/pandas; no network; no forbidden imports.
"""
from typing import Dict

import numpy as np
import pandas as pd


class SignalEngine:
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
        invest_frac: float = 1.0,
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
        self.invest_frac = float(invest_frac)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _ema(arr: np.ndarray, span: int) -> np.ndarray:
        return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy(dtype=float)

    def _gate(self, btc_close: np.ndarray) -> np.ndarray:
        """BTC 4h gate: +1 (up), -1 (down), 0 (flat)."""
        sma = pd.Series(btc_close).rolling(
            self.gate_bars, min_periods=self.gate_bars
        ).mean().to_numpy(dtype=float)
        gate = np.zeros(len(btc_close), dtype=float)
        up = btc_close > (1.0 + self.gate_th) * sma
        dn = btc_close < (1.0 - self.gate_th) * sma
        gate[up & np.isfinite(sma)] = 1.0
        gate[dn & np.isfinite(sma)] = -1.0
        return gate

    @staticmethod
    def _atr(hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, period: int) -> np.ndarray:
        """Average True Range."""
        prev_cl = np.roll(cl, 1); prev_cl[0] = cl[0]
        tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev_cl), np.abs(lo - prev_cl)))
        return pd.Series(tr).rolling(period, min_periods=period).mean().to_numpy(dtype=float)

    def _fractal_pivots(self, hi: np.ndarray, lo: np.ndarray):
        """Fractal pivot detection: dip where low is min over [i-p, i+p];
        peak where high is max over [i-p, i+p]."""
        n = len(hi)
        p = self.p_bars
        dip = np.zeros(n, dtype=bool)
        peak = np.zeros(n, dtype=bool)
        for i in range(p, n - p):
            window_lo = lo[i - p: i + p + 1]
            window_hi = hi[i - p: i + p + 1]
            dip[i] = lo[i] == window_lo.min()
            peak[i] = hi[i] == window_hi.max()
        return dip, peak

    def _donchian_pivots(self, hi: np.ndarray, lo: np.ndarray):
        """Donchian-style pivot: dip when low <= rolling min over 2*p+1 bars;
        peak when high >= rolling max over 2*p+1 bars."""
        w = 2 * self.p_bars + 1
        roll_lo = pd.Series(lo).rolling(w, min_periods=w).min().to_numpy(dtype=float)
        roll_hi = pd.Series(hi).rolling(w, min_periods=w).max().to_numpy(dtype=float)
        dip = lo <= roll_lo
        peak = hi >= roll_hi
        return dip, peak

    # ── main signal generator ────────────────────────────────────────────

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        # ---- BTC gate ----
        btc = data_map.get("BTC-USDT")
        if btc is not None:
            gate = self._gate(btc["close"].to_numpy(dtype=float))
        else:
            n0 = len(next(iter(data_map.values())))
            gate = np.zeros(n0, dtype=float)

        result: Dict[str, pd.Series] = {}
        for code, df in data_map.items():
            c = df["close"].to_numpy(dtype=float)
            hi = df["high"].to_numpy(dtype=float)
            lo = df["low"].to_numpy(dtype=float)
            n = len(df)

            # BTC itself: never traded — signal always 0
            if code == "BTC-USDT":
                result[code] = pd.Series(np.zeros(n, dtype=float), index=df.index)
                continue

            # Altcoin indicators
            fast = self._ema(c, self.fast_ema)
            slow = self._ema(c, self.slow_ema)
            atr = self._atr(hi, lo, c, self.atr_period)

            # Use fractal pivots for precise entry timing
            dip, peak = self._fractal_pivots(hi, lo)

            signals = np.zeros(n, dtype=float)
            size = self.invest_frac
            state = 0.0          # current position: +1 long, -1 short, 0 flat
            entry_price = 0.0    # price at entry
            entry_bar = 0        # bar index at entry
            best_price = 0.0     # highest close since entry (for trailing)
            worst_price = 0.0    # lowest close since entry (for trailing)
            last_exit_bar = -999 # bar index of last exit (for cooldown)

            for i in range(1, n):
                g = gate[i] if i < len(gate) else 0.0
                bars_since_entry = i - entry_bar if state != 0 else 0
                in_cooldown = (i - last_exit_bar) < self.cooldown

                # ---- EXIT logic (checked first) ----
                if state == 1.0:  # long position
                    pnl_pct = (c[i] - entry_price) / entry_price
                    best_price = max(best_price, hi[i])
                    # Trailing stop: price fell atr_sl_mult * ATR from best
                    trail_stop = (best_price - c[i]) > self.atr_sl_mult * atr[i] if np.isfinite(atr[i]) else False
                    # Fixed TP: price hit target
                    tp_hit = pnl_pct >= self.tp_pct
                    # Fixed SL: price hit stop
                    sl_hit = pnl_pct <= -self.sl_pct
                    # Min hold check
                    held_enough = bars_since_entry >= self.min_hold
                    if held_enough and (trail_stop or tp_hit or sl_hit):
                        state = 0.0
                        last_exit_bar = i
                elif state == -1.0:  # short position
                    pnl_pct = (entry_price - c[i]) / entry_price
                    worst_price = min(worst_price, lo[i])
                    # Trailing stop: price rose atr_sl_mult * ATR from worst
                    trail_stop = (c[i] - worst_price) > self.atr_sl_mult * atr[i] if np.isfinite(atr[i]) else False
                    tp_hit = pnl_pct >= self.tp_pct
                    sl_hit = pnl_pct <= -self.sl_pct
                    held_enough = bars_since_entry >= self.min_hold
                    if held_enough and (trail_stop or tp_hit or sl_hit):
                        state = 0.0
                        last_exit_bar = i

                # ---- GATE-FLIP EXIT: close if gate opposes position ----
                if state == 1.0 and g == -1.0:
                    state = 0.0
                    last_exit_bar = i
                elif state == -1.0 and g == 1.0:
                    state = 0.0
                    last_exit_bar = i

                # ---- ENTRY logic ----
                if state == 0.0 and not in_cooldown:
                    if g == 1.0 and dip[i] and fast[i] > slow[i]:
                        # Long: BTC up + local dip + uptrend confirmed
                        state = 1.0
                        entry_price = c[i]
                        entry_bar = i
                        best_price = c[i]
                    elif g == -1.0 and peak[i] and fast[i] < slow[i]:
                        # Short: BTC down + local peak + downtrend confirmed
                        state = -1.0
                        entry_price = c[i]
                        entry_bar = i
                        worst_price = c[i]

                signals[i] = state * size

            result[code] = pd.Series(signals, index=df.index)
        return result
