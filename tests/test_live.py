"""Unit tests for the live layer. Run: python -m tests.test_live (or pytest)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.config import Config
from live.engine import (ACTION_ENTER_LONG, ACTION_ENTER_SHORT, ACTION_EXIT,
                         FLAT, LONG, SHORT, LiveSignalEngine, SymbolState)

TF = 300_000


def bars(closes, highs=None, lows=None, start_ts: int = 1_700_000_000_000):
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes * 1.001
    lows = np.asarray(lows, dtype=float) if lows is not None else closes * 0.999
    idx = np.arange(len(closes), dtype="int64") * TF + start_ts
    return pd.DataFrame({"open": closes, "high": highs, "low": lows,
                         "close": closes, "volume": np.ones(len(closes))}, index=idx)


def test_gate_symbol_cannot_be_traded():
    cfg = Config(symbols=["BTC-USDT", "ETH-USDT"])
    try:
        cfg.validate()
    except ValueError as exc:
        assert "MASTER gate" in str(exc)
        return
    raise AssertionError("config accepted BTC-USDT as a tradable symbol")


def test_live_mode_requires_explicit_confirmation():
    cfg = Config(mode="live", api_key="k", api_secret="s", symbols=["ETH-USDT"])
    try:
        cfg.validate()
    except ValueError as exc:
        assert "I_UNDERSTAND_LIVE_RISK" in str(exc)
        return
    raise AssertionError("live mode armed without the risk interlock")


def test_ccxt_symbol_mapping():
    assert Config(market_type="swap").ccxt_symbol("ETH-USDT") == "ETH/USDT:USDT"
    assert Config(market_type="spot").ccxt_symbol("ETH-USDT") == "ETH/USDT"


def test_gate_thresholds():
    eng = LiveSignalEngine(gate_bars=3, gate_th=0.01, timeframe_ms=TF)
    up = eng.gate_series(np.array([100.0, 100.0, 100.0, 120.0]))
    dn = eng.gate_series(np.array([100.0, 100.0, 100.0, 80.0]))
    flat = eng.gate_series(np.array([100.0, 100.0, 100.0, 100.2]))
    assert up[-1] == 1.0 and dn[-1] == -1.0 and flat[-1] == 0.0
    assert up[0] == 0.0, "gate must be 0 before the window is complete"


def test_min_hold_blocks_immediate_exit():
    eng = LiveSignalEngine(min_hold=3, timeframe_ms=TF)
    df = bars(np.linspace(100, 90, eng.min_bars + 5))     # deep loss, past SL
    st = SymbolState()
    last_ts = int(df.index[-1])
    eng.open_position(st, LONG, 100.0, last_ts - 1 * TF, 1.0)   # 1 bar old
    assert eng.step("X", df, 1.0, st, mutate=False).action != ACTION_EXIT
    eng.open_position(st, LONG, 100.0, last_ts - 4 * TF, 1.0)   # 4 bars old
    assert eng.step("X", df, 1.0, st, mutate=False).action == ACTION_EXIT


def test_gate_flip_exits_regardless_of_min_hold():
    eng = LiveSignalEngine(min_hold=99, timeframe_ms=TF)
    df = bars(np.full(eng.min_bars + 5, 100.0))
    st = SymbolState()
    eng.open_position(st, LONG, 100.0, int(df.index[-1]), 1.0)
    dec = eng.step("X", df, -1.0, st, mutate=False)
    assert dec.action == ACTION_EXIT and dec.reason == "gate-flip"


def test_cooldown_blocks_reentry():
    eng = LiveSignalEngine(cooldown=3, timeframe_ms=TF)
    n = eng.min_bars + 10
    closes = np.concatenate([np.linspace(100, 120, n - 1), [119.0]])
    df = bars(closes)
    st = SymbolState()
    st.last_exit_ts = int(df.index[-1]) - 1 * TF          # exited 1 bar ago
    assert eng.step("X", df, 1.0, st, mutate=False).action != ACTION_ENTER_LONG
    st.last_exit_ts = int(df.index[-1]) - 9 * TF          # cooldown served
    assert eng.step("X", df, 1.0, st, mutate=False).reason != "cooldown"


def test_spot_market_never_shorts():
    n = 80
    closes = np.linspace(120, 100, n)
    highs = closes.copy(); highs[-1] = closes.max() * 1.5   # peak on the last bar
    eng = LiveSignalEngine(timeframe_ms=TF, allow_short=False)
    df = bars(closes, highs=highs)
    dec = eng.step("X", df, -1.0, SymbolState(), mutate=False)
    assert dec.action != ACTION_ENTER_SHORT
    assert "long-only" in dec.reason


def test_swap_market_can_short():
    n = 80
    closes = np.linspace(120, 100, n)
    highs = closes.copy(); highs[-1] = closes.max() * 1.5
    eng = LiveSignalEngine(timeframe_ms=TF, allow_short=True)
    dec = eng.step("X", bars(closes, highs=highs), -1.0, SymbolState(), mutate=False)
    assert dec.action == ACTION_ENTER_SHORT and dec.side == SHORT


def test_protective_levels():
    eng = LiveSignalEngine(sl_pct=0.02, tp_pct=0.04)
    lv = eng.protective_levels(LONG, 100.0)
    assert abs(lv["sl"] - 98.0) < 1e-9 and abs(lv["tp"] - 104.0) < 1e-9
    sv = eng.protective_levels(SHORT, 100.0)
    assert abs(sv["sl"] - 102.0) < 1e-9 and abs(sv["tp"] - 96.0) < 1e-9


def test_state_roundtrip():
    st = SymbolState()
    st.position.side = SHORT
    st.position.entry_price = 3.5
    st.last_exit_ts = 12345
    assert SymbolState.from_dict(st.to_dict()).position.entry_price == 3.5


def test_engine_never_reads_the_forming_bar():
    """A decision must depend only on bars up to and including the last one."""
    eng = LiveSignalEngine(timeframe_ms=TF)
    rng = np.random.default_rng(5)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, eng.min_bars + 50)))
    df = bars(closes)
    a = eng.step("X", df, 1.0, SymbolState(), mutate=False)
    extended = pd.concat([df, bars(closes[:5] * 3.0, start_ts=int(df.index[-1]) + TF)])
    b = eng.step("X", extended.iloc[: len(df)], 1.0, SymbolState(), mutate=False)
    assert (a.action, a.price) == (b.action, b.price)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
