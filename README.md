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

> ### ⚠️ These numbers are not reproducible live
>
> `_fractal_pivots` scores bar `i` with `lo[i - p : i + p + 1]` — a window that
> reaches **8 bars (40 minutes) into the future**. The entries above could not
> have been taken in real time. `python -m live.trader --selftest` quantifies it:
> **255 pivot flags** were unknowable at their own bar, and **16 of every 300**
> real-time signals differ from the hindsight signal on the same data.
>
> Re-run the backtest with the file's own causal `_donchian_pivots` before
> sizing anything. See [DEPLOY.md](DEPLOY.md#7-لماذا-لن-تتكرر-نتائج-الباك-تست).

**Constraint audit (automated, both windows):** BTC signal all-zero (never
traded); **0** gate-direction violations across all altcoins.

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

---

## Live trading

The `live/` package turns the archived strategy into a deployable bot. It keeps
every rule (BTC 4h gate, dual-EMA confirmation, ATR trail, fixed TP/SL,
gate-flip exit, min-hold, cooldown) and replaces the look-ahead pivot detector
with a causal one.

```
live/config.py    env-driven configuration + safety interlocks
live/engine.py    causal streaming signal engine (donchian | fractal_confirmed)
live/broker.py    ccxt market data, PaperBroker (simulated) and LiveBroker (real)
live/trader.py    runner loop, state persistence, reconciliation, risk guards
live/selftest.py  offline causality + constraint audit
live/demo.py      full-stack dry run on synthetic bars
tools/backtest.py backtest the live rule on real history (--compare)
```

```bash
cp .env.example .env          # MODE=paper by default
python -m live.trader --selftest    # causality + constraint audit, no network
python -m tests.test_live           # unit tests
python -m live.demo                 # watch the stack open trades offline
python -m live.trader               # run (paper until you arm live mode)

python -m tools.backtest --exchange okx --days 180 --compare
```

`tools/backtest.py` imports `live.engine` rather than re-implementing the rules,
so its numbers describe the bot that trades. `--compare` runs both causal pivot
modes alongside the archived look-ahead engine, which sizes how much of the
published edge was hindsight. A `test_step_matches_vectorised_path` test pins the
two code paths together.

Real orders need all three of `MODE=live`, API keys, and
`I_UNDERSTAND_LIVE_RISK=yes`; anything less and the bot refuses to start.
Risk guards: daily-loss halt, consecutive-loss halt, max open positions,
exchange-side reduce-only SL/TP, `--flatten` kill switch.

Deployment (Docker, systemd, staged paper → testnet → small-live rollout):
**[DEPLOY.md](DEPLOY.md)**.
