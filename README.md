# zoneXing Trading

**BTC-gated, 5-minute altcoin strategy** — a master-trend (BTC 4H) gate combined with
5-minute pullback entries on altcoins, with disciplined ATR trailing + fixed
TP/SL exits.

> Archived winning strategy. To restore it in the Vibe-Trading backtester, copy
> `zoneXing_Trading_signal_engine.py` into a run dir as `signal_engine.py` and use
> the matching config below.

---

## Hard constraints (binding, never relaxed)

1. **BTC = MASTER / gate only, NEVER traded.** Its direction is read from the
   **4-hour** timeframe (48 bars of 5m) and is **never contradicted**.
2. **Altcoin entries are analysed on the 5-minute timeframe only.**
3. **Gate rule:** BTC 4H **UP** → altcoins may ONLY go **LONG** (never short).
   BTC 4H **DOWN** → altcoins may ONLY go **SHORT** (never long). No position
   ever opposes the gate.

## Edge design

- **Fractal pivot swings** — a bar is a dip/peak only if it is the extreme within
  `p_bars` bars on *each* side → precise, low-noise entry timing.
- **Dual-EMA trend confirmation** on 5m — enter only genuine pullbacks inside an
  established micro-trend aligned with the gate.
- **Disciplined exits** — ATR trailing stop + fixed take-profit/stop-loss +
  gate-flip forced exit + minimum hold + cooldown to avoid whipsaw.

## Parameters (defaults)

| Param | Default | Meaning |
|---|---|---|
| `gate_bars` | 48 | 5m bars making the 4h gate window |
| `gate_th` | 0.002 | buffer around the gate SMA (whipsaw guard) |
| `p_bars` | 8 | fractal pivot half-window (each side) |
| `fast_ema` / `slow_ema` | 8 / 21 | 5m trend confirmation EMAs |
| `atr_period` / `atr_sl_mult` | 14 / 1.5 | ATR trailing stop |
| `sl_pct` / `tp_pct` | 0.02 / 0.04 | fixed stop-loss / take-profit |
| `cooldown` / `min_hold` | 3 / 3 | whipsaw guards |
| `invest_frac` | 1.0 | position weight per trade |

## Backtest results (real Vibe-Trading engine, $1200, leverage 1)

| Window | Return | Max DD | PF | Win% | Trades |
|---|---|---|---|---|---|
| Jan 2026 (choppy) | **+11.55%** | −1.70% | 5.07 | 60.8% | 268 |
| Jun–Dec 2025 (multi-regime) | **+163.14%** | −1.94% | 6.23 | 64.2% | 1823 |

**Constraint audit (automated, both windows):** BTC signal all-zero (never
traded); **0** gate-direction violations across all altcoins.

> ⚠️ **These archived numbers depend on a lookahead bug.** The local harness
> added in `backtest/` shows the fractal pivot rule reads bars that have not
> happened yet. See [Local backtesting](#local-backtesting) below before
> trusting the table above.

---

## Local backtesting

A self-contained harness lives in `backtest/`. No network needed to run it, no
external backtesting service:

```bash
pip install numpy pandas

# real candles you already have (see data/README.md for the CSV format)
python3 run_backtest.py --source csv --csv-dir ./data --compare

# real candles straight from OKX/Binance (needs network access)
python3 run_backtest.py --source exchange --bars 60000 --compare

# reproducible simulated market — exercises the machinery, proves no edge
python3 run_backtest.py --source synthetic --bars 60000 --compare --seed 1 2 3 4
```

| Module | Role |
|---|---|
| `backtest/data.py` | CSV loader, exchange fetcher, synthetic generator |
| `backtest/engine.py` | portfolio simulator — next-bar fills, fees, slippage, shorts |
| `backtest/metrics.py` | return, drawdown, PF, win rate, Sharpe, Calmar |
| `backtest/audit.py` | the hard constraints + a lookahead detector |
| `backtest/causal.py` | non-peeking variants of the pivot rule |
| `tests/test_backtest.py` | 12 tests covering the harness accounting |

The simulator fills a bar-`i` signal at the **open of bar `i+1`** (a signal
derived from a close cannot be filled at that same close), charges
`taker + slippage` on both sides, and uses `position_adjustment="hold"` so a
position is sized once at entry rather than re-sized every bar.

Run the harness tests with `python3 tests/test_backtest.py` (or `pytest tests`).

## Validation findings

### 1. The fractal pivot rule uses future bars

`SignalEngine._fractal_pivots` marks bar `i` as a dip when `lo[i]` is the
minimum of `lo[i-p : i+p+1]` — a window containing `p` bars **after** `i` — and
then enters on bar `i`. With `p_bars=8`, every entry is placed using 40 minutes
of price action that has not happened yet.

`backtest/audit.py:check_causality` proves it by replay: for a causal engine,
`generate(data[:k])` must equal `generate(data)[:k]` — replaying history bar by
bar has to reproduce what the full-history run claimed. The archived engine
fails that check, at a depth bounded by `p_bars`:

```
$ python3 run_backtest.py --source synthetic --bars 60000 --seed 3 --causality-probes 40
  Replay probes           40  (12 disagreed with the full run)
  Bars that changed       62 / 5,840,330 (0.00%)
  Lookahead depth         8 bars of future data
  Result                  LOOKAHEAD — signals change once later bars exist
      BNB-USDT bar 2536 (3 bars before prefix end): live=+0.0 vs hindsight=+1.0
      ETH-USDT bar 5682 (1 bars before prefix end): live=+0.0 vs hindsight=-1.0
```

Few bars change in absolute terms — a probe only diverges where a pivot in the
tail would have opened a position — but those are precisely the entry bars, so
the effect on returns is not small at all. `tests/test_backtest.py` pins the
mechanism deterministically: a V-bottom the full-history run flags is invisible
to a run that stops at that same bar.

### 2. Remove the peek and the edge disappears

`backtest/causal.py` provides two non-peeking pivot rules, changing nothing
else: `shift` acts on the same fractal pivot `p_bars` later (when it is actually
confirmed), and `donchian` uses the engine's own unused trailing-window
`_donchian_pivots`. Across 8 independent simulated markets of 60,000 5m bars:

| pivot rule | median return | worst | best | median PF | profitable seeds |
|---|---|---|---|---|---|
| `fractal` (archived, peeks) | **+1477%** | +990% | +2269% | 5.19 | **8 / 8** |
| `shift` (causal) | −92.7% | −95.1% | −90.4% | 0.59 | 0 / 8 |
| `donchian` (causal) | −51.4% | −61.8% | −46.1% | 0.62 | 0 / 8 |

The archived rule's profit factor of ~5–6 and win rate of ~65% reproduce the
README's headline figures almost exactly — on data with no real edge in it.
That is the signature of a lookahead, not of a strategy.

### 3. Fees are the second problem

At 5m turnover the cost drag is severe on its own. A single causal run
(60,000 bars, `$1200`, `invest_frac 0.2`) paid **$655 in fees — 54.6% of
starting capital** across 1,961 round trips. Buy-and-hold on the same
simulated data returned +237%, so the market was not the difficulty.

### 4. The hard constraints do hold

Every run, every pivot mode, every seed: BTC's signal is all-zero (never
traded), and there are **0** gate-direction violations. The gate logic in the
archived engine is sound — it is the entry timing that is not.

> **Why simulated data?** The sandbox this harness was built in has no route to
> exchange APIs (blocked by network policy). Synthetic candles cannot tell you
> whether a strategy makes money — but they are more than enough to show a
> lookahead, because a strategy that profits on a process with no exploitable
> structure is reading the future. Point the harness at real candles
> (`--source csv` or `--source exchange`) for numbers that speak to live edge.

## Restore config (JSON)

```json
{
  "codes": ["BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","DOGE-USDT"],
  "interval": "5m",
  "engine": "daily",
  "position_adjustment": "hold",
  "initial_cash": 1200,
  "leverage": 1,
  "taker_rate": 0.0005,
  "maker_rate": 0.0002,
  "slippage": 0.0005,
  "invest_frac": 0.2
}
```

## Caveats / honest notes

- Active **short** positions earn funding on the Vibe crypto engine — short-side
  edge may be weaker on real exchanges.
- High turnover on 5m: taker fees + slippage are the main live-cost drag.
- In flat/chop where the gate hovers in the dead-band, the strategy sits out
  (reduces return more than it loses).
- Very high annualised Calmar/Sharpe figures are inflated by the 5m
  annualisation factor; the defensible metrics are return, <2% drawdown, PF 5–6.
