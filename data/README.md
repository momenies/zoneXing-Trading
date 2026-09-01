# Real candle data

Drop one CSV per symbol here, named after the code, then run:

```bash
python3 run_backtest.py --source csv --csv-dir ./data --compare
```

Expected files (default codes):

```
data/BTC-USDT.csv   data/ETH-USDT.csv   data/BNB-USDT.csv
data/SOL-USDT.csv   data/XRP-USDT.csv   data/DOGE-USDT.csv
```

`BTC-USDT.csv` is required — it drives the 4h gate.

## Format

Column names are matched case-insensitively. A timestamp column is required and
may be called `timestamp`, `time`, `date`, `datetime` or `open_time`; it may be
epoch seconds, epoch milliseconds, or an ISO-8601 string. `volume` is optional.

```csv
timestamp,open,high,low,close,volume
1717200000000,67450.1,67512.0,67401.3,67489.7,142.55
1717200300000,67489.7,67530.2,67455.0,67470.1,98.21
```

Bars must be 5-minute and the symbols must overlap in time — the loader
intersects timestamps across all symbols so the gate lines up bar-for-bar.

## Getting the data

This directory is empty because the sandbox that generated the harness has no
route to exchange APIs. With network access:

```bash
python3 run_backtest.py --source exchange --bars 60000 --compare
```

pulls candles straight from OKX (falling back to Binance). Otherwise export
them from your own venue, or from `ccxt`, into the format above.
