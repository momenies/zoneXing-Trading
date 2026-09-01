"""Performance metrics.

Trade-level stats (PF, win rate) come from realised round trips NET of fees,
which is the honest version — a gross-PF number flatters a 5m strategy badly.
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .engine import BacktestResult, Trade


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _trade_stats(trades: List[Trade]) -> dict:
    if not trades:
        return {
            "trades": 0, "win_rate": float("nan"), "profit_factor": float("nan"),
            "avg_win": float("nan"), "avg_loss": float("nan"),
            "avg_bars_held": float("nan"), "long_trades": 0, "short_trades": 0,
        }
    pnl = np.array([t.net_pnl for t in trades], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_loss = float(-losses.sum())
    return {
        "trades": len(trades),
        "win_rate": float(len(wins) / len(pnl)),
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
        "long_trades": sum(1 for t in trades if t.side == "long"),
        "short_trades": sum(1 for t in trades if t.side == "short"),
    }


def compute_metrics(result: BacktestResult, config: BacktestConfig | None = None) -> dict:
    cfg = config or result.config
    eq = result.equity
    rets = eq.pct_change().fillna(0.0)
    n = len(eq)
    years = n / cfg.bars_per_year if cfg.bars_per_year else float("nan")

    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    max_dd = _max_drawdown(eq)
    vol = float(rets.std() * np.sqrt(cfg.bars_per_year))
    sharpe = float(rets.mean() / rets.std() * np.sqrt(cfg.bars_per_year)) if rets.std() > 0 else float("nan")
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0) if years > 0 else float("nan")

    exposure = float((result.positions.abs().sum(axis=1) > 0).mean())

    metrics = {
        "start": str(eq.index[0]), "end": str(eq.index[-1]),
        "bars": n, "years": years,
        "initial_equity": float(eq.iloc[0]), "final_equity": float(eq.iloc[-1]),
        "total_return": total_return, "cagr": cagr,
        "max_drawdown": max_dd,
        "calmar": float(cagr / abs(max_dd)) if max_dd < 0 else float("nan"),
        "ann_vol": vol, "sharpe": sharpe,
        "total_fees": float(result.total_fees),
        "fees_pct_of_initial": float(result.total_fees / eq.iloc[0]),
        "time_in_market": exposure,
        **_trade_stats(result.trades),
    }
    result.metrics = metrics
    return metrics


def format_report(metrics: dict, title: str = "Backtest") -> str:
    def pct(x):
        return "n/a" if x != x else f"{x * 100:+.2f}%"

    def num(x, d=2):
        if x != x:
            return "n/a"
        return "inf" if x == float("inf") else f"{x:.{d}f}"

    lines = [
        f"── {title} " + "─" * max(0, 58 - len(title)),
        f"  Period          {metrics['start'][:19]} → {metrics['end'][:19]}",
        f"  Bars            {metrics['bars']:,}  ({metrics['years']:.2f} years)",
        f"  Equity          {metrics['initial_equity']:,.2f} → {metrics['final_equity']:,.2f}",
        f"  Total return    {pct(metrics['total_return'])}",
        f"  CAGR            {pct(metrics['cagr'])}",
        f"  Max drawdown    {pct(metrics['max_drawdown'])}",
        f"  Calmar          {num(metrics['calmar'])}",
        f"  Ann. vol        {pct(metrics['ann_vol'])}",
        f"  Sharpe          {num(metrics['sharpe'])}",
        f"  Time in market  {pct(metrics['time_in_market'])}",
        "",
        f"  Trades          {metrics['trades']:,}  ({metrics['long_trades']} long / {metrics['short_trades']} short)",
        f"  Win rate        {pct(metrics['win_rate'])}",
        f"  Profit factor   {num(metrics['profit_factor'])}   (net of fees)",
        f"  Avg win/loss    {num(metrics['avg_win'])} / {num(metrics['avg_loss'])}",
        f"  Avg bars held   {num(metrics['avg_bars_held'], 1)}",
        f"  Fees paid       {metrics['total_fees']:,.2f}  ({pct(metrics['fees_pct_of_initial'])} of initial)",
    ]
    return "\n".join(lines)
