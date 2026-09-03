"""Dry-run the complete trader stack offline (no network, no keys).

Feeds synthetic 5m bars through the real ``Trader`` + ``PaperBroker`` so you can
watch entries, exits, protective levels, the trade log and the state file behave
exactly as they will on your server.  Use it to smoke-test a deployment before
pointing it at a live exchange:

    python -m live.demo
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Config
from .selftest import synth
from .trader import Trader, setup_logging

log = logging.getLogger("zonexing.demo")


class ReplayFeed:
    """MarketData stand-in that reveals synthetic history one bar at a time."""

    def __init__(self, cfg: Config, bars: int = 900):
        self.cfg = cfg
        self.ex = None
        seeds = {cfg.gate_symbol: (3, 60000.0)}
        for k, code in enumerate(cfg.symbols):
            seeds[code] = (11 + k, 3000.0 / (k + 1))
        self.frames = {code: synth(n=bars, seed=s, start=p)
                       for code, (s, p) in seeds.items()}
        self.cursor = 0

    def advance(self, to: int) -> None:
        self.cursor = to

    def fetch(self, code: str, limit: Optional[int] = None) -> pd.DataFrame:
        df = self.frames[code].iloc[: self.cursor]
        limit = limit or self.cfg.history_bars
        return df.iloc[-limit:]

    def last_price(self, code: str) -> float:
        return float(self.frames[code]["close"].iloc[self.cursor - 1])


def main(argv: Optional[list] = None) -> int:
    from .broker import PaperBroker

    cfg = Config(mode="paper", market_type="swap",
                 symbols=["ETH-USDT", "SOL-USDT"], invest_frac=0.05,
                 paper_equity=1200.0, leverage=1.0,
                 state_dir=Path(__file__).resolve().parent.parent / "state" / "demo")
    cfg.validate()
    setup_logging(cfg)
    for noisy in ("zonexing.trader",):
        logging.getLogger(noisy).setLevel(logging.INFO)

    trader = Trader(cfg)
    feed = ReplayFeed(cfg)
    trader.data = feed
    trader.broker = PaperBroker(cfg, feed)

    total = len(next(iter(feed.frames.values())))
    print(f"replaying {total} synthetic 5m bars through the live trader "
          f"({cfg.symbols}, paper equity {cfg.paper_equity:.0f})\n")

    first_trade_bar = None
    for i in range(trader.engine.min_bars, total):
        feed.advance(i)
        trader.cycle()
        if first_trade_bar is None and any(
                s.position.side != 0 for s in trader.states.values()):
            first_trade_bar = i
            print(f"\n>>> FIRST TRADE OPENED at replay bar {i}\n")

    print("\n" + trader.status())
    log_path = cfg.state_dir / "trades.csv"
    if log_path.is_file():
        rows = log_path.read_text(encoding="utf-8").strip().splitlines()
        print(f"\ntrades logged: {len(rows) - 1}  →  {log_path}")
        for line in rows[:6]:
            print("  " + line)
    print(f"\npaper equity: {trader.broker.equity():.2f} "
          f"(realised {trader.broker.realised:+.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
