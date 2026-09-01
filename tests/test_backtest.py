"""Tests for the backtest harness itself.

The point is to make the reported numbers trustworthy: if the accounting is
wrong, every metric downstream is wrong too. Run with ``pytest tests`` or
``python3 tests/test_backtest.py``.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.audit import audit_constraints, check_causality  # noqa: E402
from backtest.causal import build_engine                       # noqa: E402
from backtest.config import BacktestConfig                     # noqa: E402
from backtest.data import synthetic                            # noqa: E402
from backtest.engine import run_backtest                       # noqa: E402
from backtest.metrics import compute_metrics                   # noqa: E402


def _ramp(n=50, start=100.0, step=1.0):
    """Deterministic single-symbol frame with no intrabar noise."""
    close = start + step * np.arange(n, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range("2025-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": np.maximum(open_, close),
         "low": np.minimum(open_, close), "close": close, "volume": 1.0},
        index=idx,
    )


def _cfg(**kw):
    base = dict(initial_cash=1000.0, leverage=1.0, taker_rate=0.0,
                maker_rate=0.0, slippage=0.0, invest_frac=1.0)
    base.update(kw)
    return BacktestConfig(**base)


def test_long_matches_price_return():
    """Fully invested long, zero cost -> equity tracks the price exactly."""
    df = _ramp()
    dm = {"ETH-USDT": df}
    sig = {"ETH-USDT": pd.Series(np.ones(len(df)), index=df.index)}
    res = run_backtest(dm, sig, _cfg())
    # filled at bar 1 open, force-closed at the last close
    expected = df["close"].iloc[-1] / df["open"].iloc[1] - 1.0
    got = res.equity.iloc[-1] / res.equity.iloc[0] - 1.0
    assert abs(got - expected) < 1e-9, (got, expected)


def test_short_is_mirror_of_long():
    df = _ramp()
    dm = {"ETH-USDT": df}
    sig = {"ETH-USDT": pd.Series(-np.ones(len(df)), index=df.index)}
    res = run_backtest(dm, sig, _cfg())
    expected = -(df["close"].iloc[-1] / df["open"].iloc[1] - 1.0)
    got = res.equity.iloc[-1] / res.equity.iloc[0] - 1.0
    assert abs(got - expected) < 1e-9, (got, expected)


def test_flat_signal_never_moves_equity():
    df = _ramp()
    dm = {"ETH-USDT": df}
    sig = {"ETH-USDT": pd.Series(np.zeros(len(df)), index=df.index)}
    res = run_backtest(dm, sig, _cfg())
    assert res.equity.nunique() == 1
    assert res.trades == []


def test_signal_is_filled_on_the_next_bar():
    """A signal on bar i must not capture bar i's own move."""
    df = _ramp(n=6)
    dm = {"ETH-USDT": df}
    s = np.zeros(6)
    s[2] = 1.0  # decided at close of bar 2 -> filled at open of bar 3
    sig = {"ETH-USDT": pd.Series(s, index=df.index)}
    res = run_backtest(dm, sig, _cfg())
    assert res.equity.iloc[2] == res.equity.iloc[0]      # nothing before the fill
    trade = res.trades[0]
    assert trade.entry_price == df["open"].iloc[3]


def test_costs_are_charged_on_both_sides():
    df = _ramp(n=10)
    dm = {"ETH-USDT": df}
    s = np.zeros(10); s[1:5] = 1.0
    sig = {"ETH-USDT": pd.Series(s, index=df.index)}
    free = run_backtest(dm, sig, _cfg())
    costed = run_backtest(dm, sig, _cfg(taker_rate=0.001, slippage=0.0))
    assert costed.total_fees > 0
    assert costed.equity.iloc[-1] < free.equity.iloc[-1]
    # one entry + one exit, each at 0.1% of traded notional
    assert len(costed.trades) == 1
    assert costed.trades[0].fees > 0


def test_round_trip_pnl_matches_equity_change():
    df = _ramp(n=20)
    dm = {"ETH-USDT": df}
    s = np.zeros(20); s[3:12] = 1.0
    sig = {"ETH-USDT": pd.Series(s, index=df.index)}
    res = run_backtest(dm, sig, _cfg(taker_rate=0.0005, slippage=0.0005))
    assert len(res.trades) == 1
    net = sum(t.net_pnl for t in res.trades)
    delta = res.equity.iloc[-1] - res.equity.iloc[0]
    assert abs(net - delta) < 1e-6, (net, delta)


def test_hold_sizing_does_not_churn():
    """'hold' must not re-trade as symbol weights drift apart; 'rebalance' does.

    Two symbols on diverging paths: under 'rebalance' the winner has to be
    trimmed into the loser every bar, which costs fees 'hold' never pays.
    """
    dm = synthetic(["ETH-USDT", "SOL-USDT"], bars=800, seed=4)
    sig = {c: pd.Series(np.ones(len(df)), index=df.index) for c, df in dm.items()}
    cfg = _cfg(taker_rate=0.001, invest_frac=0.5)
    held = run_backtest(dm, sig, cfg, position_adjustment="hold")
    rebal = run_backtest(dm, sig, cfg, position_adjustment="rebalance")
    assert held.total_fees < rebal.total_fees, (held.total_fees, rebal.total_fees)


def test_btc_is_never_traded_and_gate_is_respected():
    dm = synthetic(["BTC-USDT", "ETH-USDT", "SOL-USDT"], bars=1500, seed=11)
    engine = build_engine("fractal")
    sig = engine.generate(dm)
    gate = engine._gate(dm["BTC-USDT"]["close"].to_numpy(dtype=float))
    report = audit_constraints(dm, sig, gate)
    assert not report["btc_traded"]
    assert report["total_gate_violations"] == 0
    assert report["passed"]


def test_fractal_pivot_needs_bars_that_have_not_happened_yet():
    """The defect, isolated: a pivot is only marked once future bars exist.

    A V-bottom at index 5 is flagged by the full-history run, but a run that
    stops at index 5 -- all a live system ever has -- cannot see it.
    """
    engine = build_engine("fractal", p_bars=3)
    lo = np.array([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10], dtype=float)
    hi = lo + 1.0
    dip_full, _ = engine._fractal_pivots(hi, lo)
    assert dip_full[5], "full-history run should mark the V-bottom"
    dip_live, _ = engine._fractal_pivots(hi[:6], lo[:6])
    assert not dip_live[5], "live run cannot know bar 5 is a pivot yet"


def test_archived_engine_signals_change_when_future_arrives():
    dm = synthetic(["BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT"],
                   bars=3000, seed=5)
    rep = check_causality(build_engine("fractal"), dm, probes=12, warmup=800)
    assert not rep["causal"], "archived engine should be caught peeking"
    assert rep["lookahead_bars"] > 0


def test_causal_variants_do_not_peek():
    dm = synthetic(["BTC-USDT", "ETH-USDT"], bars=2500, seed=5)
    for mode in ("shift", "donchian"):
        rep = check_causality(build_engine(mode), dm, probes=6, warmup=800)
        assert rep["causal"], f"{mode} leaked {rep['changed_bars']} bars"


def test_metrics_are_self_consistent():
    dm = synthetic(["BTC-USDT", "ETH-USDT"], bars=2000, seed=2)
    sig = build_engine("fractal").generate(dm)
    res = run_backtest(dm, sig, BacktestConfig())
    m = compute_metrics(res, res.config)
    assert m["max_drawdown"] <= 0.0
    assert 0.0 <= m["time_in_market"] <= 1.0
    assert m["trades"] == m["long_trades"] + m["short_trades"]
    if m["trades"]:
        assert 0.0 <= m["win_rate"] <= 1.0


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
