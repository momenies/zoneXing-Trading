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
