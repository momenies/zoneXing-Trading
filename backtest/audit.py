"""Constraint and validity audits.

Two very different kinds of check live here:

* ``audit_constraints`` — the README's binding rules: BTC is never traded, and
  no altcoin position ever opposes the BTC 4h gate.
* ``check_causality``  — does the engine peek at the future? A causal engine
  must satisfy ``generate(data[:k])[-1] == generate(data)[k-1]``: replaying
  history bar by bar has to reproduce what the full-history run claimed. Any
  mismatch means the backtest is trading on information it would not have had.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd


def audit_constraints(
    data_map: Dict[str, pd.DataFrame],
    signals: Dict[str, pd.Series],
    gate: np.ndarray,
    btc_code: str = "BTC-USDT",
) -> dict:
    """Verify the strategy's hard constraints. Returns a report dict."""
    report: dict = {"btc_traded": False, "btc_max_abs_signal": 0.0,
                    "gate_violations": {}, "total_gate_violations": 0}

    if btc_code in signals:
        btc_sig = signals[btc_code].to_numpy(dtype=float)
        report["btc_max_abs_signal"] = float(np.abs(btc_sig).max())
        report["btc_traded"] = bool(report["btc_max_abs_signal"] > 0)

    total = 0
    for code, s in signals.items():
        if code == btc_code:
            continue
        sv = s.to_numpy(dtype=float)
        g = gate[: len(sv)]
        # a violation is holding long while the gate is DOWN, or vice versa.
        # gate == 0 (dead band) is "no opinion", not opposition.
        bad = ((sv > 0) & (g == -1.0)) | ((sv < 0) & (g == 1.0))
        count = int(bad.sum())
        report["gate_violations"][code] = count
        total += count
    report["total_gate_violations"] = total
    report["passed"] = (not report["btc_traded"]) and total == 0
    return report


def check_causality(
    engine, data_map: Dict[str, pd.DataFrame], probes: int = 8,
    warmup: int = 600, seed: int = 3, codes: List[str] | None = None,
) -> dict:
    """Replay the engine on truncated history and diff it against the full run.

    For a causal engine ``generate(data[:k])`` must equal ``generate(data)[:k]``
    exactly — running today with only today's history has to reproduce what the
    full-history backtest claimed for those same bars. Any bar that changes once
    later data exists is a bar the backtest traded with future information.

    ``lookahead_bars`` is how far back from the end of a prefix the earliest
    changed bar sits, i.e. how many bars of future the engine consumes.
    """
    rng = np.random.default_rng(seed)
    full = engine.generate(data_map)
    n = len(next(iter(data_map.values())))
    if n <= warmup + 5:
        raise ValueError("not enough bars for a causality probe")

    codes = codes or [c for c in data_map if c != "BTC-USDT"]
    points = sorted(rng.choice(np.arange(warmup, n), size=min(probes, n - warmup),
                               replace=False).tolist())

    changed_bars = 0
    compared_bars = 0
    bad_probes = 0
    lookahead = 0
    examples: List[dict] = []

    for k in points:
        prefix = {c: df.iloc[:k].copy() for c, df in data_map.items()}
        partial = engine.generate(prefix)
        probe_bad = False
        for c in codes:
            live = partial[c].to_numpy(dtype=float)
            hindsight = full[c].to_numpy(dtype=float)[:k]
            diff = np.flatnonzero(np.abs(live - hindsight) > 1e-9)
            compared_bars += k
            if diff.size:
                probe_bad = True
                changed_bars += int(diff.size)
                lookahead = max(lookahead, int(k - 1 - diff[0]) + 1)
                if len(examples) < 5:
                    i = int(diff[0])
                    examples.append({
                        "code": c, "prefix_len": k, "bar": i,
                        "bars_before_prefix_end": int(k - 1 - i),
                        "live_signal": float(live[i]),
                        "hindsight_signal": float(hindsight[i]),
                    })
        bad_probes += int(probe_bad)

    return {
        "probe_points": len(points),
        "probes_with_changes": bad_probes,
        "compared_bars": compared_bars,
        "changed_bars": changed_bars,
        "change_rate": changed_bars / compared_bars if compared_bars else float("nan"),
        "lookahead_bars": lookahead,
        "examples": examples,
        "causal": changed_bars == 0,
    }
