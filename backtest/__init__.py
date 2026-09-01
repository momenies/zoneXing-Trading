"""Local backtesting harness for the zoneXing Trading signal engine."""

from .config import BacktestConfig
from .engine import run_backtest, BacktestResult
from .metrics import compute_metrics

__all__ = ["BacktestConfig", "run_backtest", "BacktestResult", "compute_metrics"]
