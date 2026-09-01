"""Backtest configuration — mirrors the Vibe-Trading restore config in README.md."""
from dataclasses import dataclass, field
from typing import List


DEFAULT_CODES = [
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT",
]


@dataclass
class BacktestConfig:
    """Execution / cost assumptions for a run.

    Defaults reproduce the ``Restore config (JSON)`` block in README.md.
    """

    codes: List[str] = field(default_factory=lambda: list(DEFAULT_CODES))
    interval: str = "5m"
    initial_cash: float = 1200.0
    leverage: float = 1.0
    taker_rate: float = 0.0005
    maker_rate: float = 0.0002
    slippage: float = 0.0005
    invest_frac: float = 0.2
    # Funding paid (>0) or earned (<0) per 8h on open notional. The README notes
    # the Vibe engine credits funding to shorts; live venues usually charge it.
    funding_rate_8h: float = 0.0

    # bars per year, used only for annualised figures (5m -> 105_120)
    bars_per_year: int = 105_120

    @property
    def round_trip_cost(self) -> float:
        """Cost of one side of a trade as a fraction of traded notional."""
        return self.taker_rate + self.slippage
