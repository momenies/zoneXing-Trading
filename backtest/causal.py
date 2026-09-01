"""Causal (non-peeking) variants of the archived signal engine.

The archived ``SignalEngine._fractal_pivots`` marks bar ``i`` as a dip when
``lo[i]`` is the minimum of ``lo[i-p : i+p+1]`` — a window that includes
``p`` bars AFTER ``i``. It then enters on bar ``i``. Live, that pivot is not
knowable until bar ``i+p``, so the archived engine trades on information from
the future.

These subclasses keep every other rule identical and only change how a pivot
becomes known:

``shift``     the same fractal pivot, but only acted on ``p_bars`` later —
              i.e. at the moment it is actually confirmed.
``donchian``  the engine's own (unused) ``_donchian_pivots``: a trailing-window
              extreme, which needs no future bars at all.
"""
from __future__ import annotations

import numpy as np

from zoneXing_Trading_signal_engine import SignalEngine

PIVOT_MODES = ("fractal", "shift", "donchian")


class CausalSignalEngine(SignalEngine):
    """``SignalEngine`` with a selectable, non-peeking pivot rule."""

    def __init__(self, *args, pivot_mode: str = "shift", **kwargs):
        if pivot_mode not in PIVOT_MODES:
            raise ValueError(f"pivot_mode must be one of {PIVOT_MODES}")
        super().__init__(*args, **kwargs)
        self.pivot_mode = pivot_mode

    def _fractal_pivots(self, hi: np.ndarray, lo: np.ndarray):
        if self.pivot_mode == "fractal":
            return super()._fractal_pivots(hi, lo)
        if self.pivot_mode == "donchian":
            return self._donchian_pivots(hi, lo)

        # "shift": confirm the fractal pivot p bars after the fact
        dip, peak = super()._fractal_pivots(hi, lo)
        p = self.p_bars
        dip_c = np.zeros_like(dip)
        peak_c = np.zeros_like(peak)
        if p < len(dip):
            dip_c[p:] = dip[:-p] if p else dip
            peak_c[p:] = peak[:-p] if p else peak
        return dip_c, peak_c


def build_engine(pivot_mode: str = "fractal", **params):
    """Return the archived engine for ``fractal``, else a causal variant."""
    if pivot_mode == "fractal":
        return SignalEngine(**params)
    return CausalSignalEngine(pivot_mode=pivot_mode, **params)
