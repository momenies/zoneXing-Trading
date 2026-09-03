"""Configuration for the zoneXing live trader.

Everything is read from environment variables (or a local ``.env`` file), so the
same image/checkout can be deployed to any server without editing code.
See ``.env.example`` for the full list with comments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency, does not overwrite real env)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _s(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _f(key: str, default: float) -> float:
    try:
        return float(_s(key) or default)
    except ValueError:
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(float(_s(key) or default))
    except ValueError:
        return default


def _b(key: str, default: bool = False) -> bool:
    val = _s(key).lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "y", "on"}


def _list(key: str, default: str) -> List[str]:
    return [x.strip().upper() for x in (_s(key) or default).split(",") if x.strip()]


@dataclass
class Config:
    # ── mode / venue ────────────────────────────────────────────────────
    mode: str = "paper"              # paper | live
    exchange_id: str = "okx"         # any ccxt id: okx, binance, bybit, kucoinfutures…
    market_type: str = "swap"        # swap (perp, allows shorts) | spot (long only)
    testnet: bool = False

    api_key: str = ""
    api_secret: str = ""
    api_password: str = ""           # OKX/KuCoin passphrase

    # ── universe ────────────────────────────────────────────────────────
    gate_symbol: str = "BTC-USDT"    # master gate, NEVER traded
    symbols: List[str] = field(default_factory=lambda: ["ETH-USDT"])
    timeframe: str = "5m"
    history_bars: int = 300          # bars fetched per cycle (warm-up window)

    # ── strategy params (must mirror the backtest config) ───────────────
    gate_bars: int = 48
    gate_th: float = 0.002
    p_bars: int = 8
    fast_ema: int = 8
    slow_ema: int = 21
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    sl_pct: float = 0.02
    tp_pct: float = 0.04
    cooldown: int = 3
    min_hold: int = 3
    invest_frac: float = 0.05
    pivot_mode: str = "donchian"     # donchian (causal) | fractal_confirmed

    # ── execution / risk ────────────────────────────────────────────────
    leverage: float = 1.0
    paper_equity: float = 1200.0
    taker_rate: float = 0.0005
    slippage: float = 0.0005
    max_open_positions: int = 5
    max_daily_loss_pct: float = 5.0   # halt NEW entries for the day past this
    max_consecutive_losses: int = 6   # halt NEW entries until restart
    protective_orders: bool = True    # exchange-side reduce-only SL/TP (swap only)
    close_on_exit: bool = False       # flatten everything on SIGTERM
    confirm_live: str = ""            # must equal "yes" to arm real orders

    # ── ops ─────────────────────────────────────────────────────────────
    poll_buffer_sec: int = 8         # wait after bar close before fetching
    state_dir: Path = ROOT / "state"
    log_level: str = "INFO"
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # ── derived ─────────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def allow_short(self) -> bool:
        return self.market_type != "spot"

    @property
    def timeframe_ms(self) -> int:
        unit = self.timeframe[-1]
        n = int(self.timeframe[:-1])
        return n * {"m": 60, "h": 3600, "d": 86400}[unit] * 1000

    def ccxt_symbol(self, code: str) -> str:
        """`ETH-USDT` → `ETH/USDT` (spot) or `ETH/USDT:USDT` (perp)."""
        base, _, quote = code.partition("-")
        pair = f"{base}/{quote}"
        return f"{pair}:{quote}" if self.market_type == "swap" else pair

    def validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError(f"MODE must be paper|live, got {self.mode!r}")
        if self.market_type not in {"spot", "swap"}:
            raise ValueError(f"MARKET_TYPE must be spot|swap, got {self.market_type!r}")
        if self.pivot_mode not in {"donchian", "fractal_confirmed"}:
            raise ValueError(f"PIVOT_MODE must be donchian|fractal_confirmed")
        if self.gate_symbol in self.symbols:
            raise ValueError(
                f"{self.gate_symbol} is the MASTER gate and must never be traded — "
                "remove it from SYMBOLS."
            )
        if not self.symbols:
            raise ValueError("SYMBOLS is empty")
        if not 0 < self.invest_frac <= 1:
            raise ValueError("INVEST_FRAC must be in (0, 1]")
        if self.is_live:
            if not (self.api_key and self.api_secret):
                raise ValueError("MODE=live requires API_KEY and API_SECRET")
            if self.confirm_live.lower() != "yes":
                raise ValueError(
                    "MODE=live refused: set I_UNDERSTAND_LIVE_RISK=yes to arm real orders"
                )


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    _load_dotenv(Path(env_file) if env_file else ROOT / ".env")
    cfg = Config(
        mode=_s("MODE", "paper").lower(),
        exchange_id=_s("EXCHANGE", "okx").lower(),
        market_type=_s("MARKET_TYPE", "swap").lower(),
        testnet=_b("TESTNET", False),
        api_key=_s("API_KEY"),
        api_secret=_s("API_SECRET"),
        api_password=_s("API_PASSWORD"),
        gate_symbol=_s("GATE_SYMBOL", "BTC-USDT").upper(),
        symbols=_list("SYMBOLS", "ETH-USDT,BNB-USDT,SOL-USDT,XRP-USDT,DOGE-USDT"),
        timeframe=_s("TIMEFRAME", "5m"),
        history_bars=_i("HISTORY_BARS", 300),
        gate_bars=_i("GATE_BARS", 48),
        gate_th=_f("GATE_TH", 0.002),
        p_bars=_i("P_BARS", 8),
        fast_ema=_i("FAST_EMA", 8),
        slow_ema=_i("SLOW_EMA", 21),
        atr_period=_i("ATR_PERIOD", 14),
        atr_sl_mult=_f("ATR_SL_MULT", 1.5),
        sl_pct=_f("SL_PCT", 0.02),
        tp_pct=_f("TP_PCT", 0.04),
        cooldown=_i("COOLDOWN", 3),
        min_hold=_i("MIN_HOLD", 3),
        invest_frac=_f("INVEST_FRAC", 0.05),
        pivot_mode=_s("PIVOT_MODE", "donchian").lower(),
        leverage=_f("LEVERAGE", 1.0),
        paper_equity=_f("PAPER_EQUITY", 1200.0),
        taker_rate=_f("TAKER_RATE", 0.0005),
        slippage=_f("SLIPPAGE", 0.0005),
        max_open_positions=_i("MAX_OPEN_POSITIONS", 5),
        max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 5.0),
        max_consecutive_losses=_i("MAX_CONSECUTIVE_LOSSES", 6),
        protective_orders=_b("PROTECTIVE_ORDERS", True),
        close_on_exit=_b("CLOSE_ON_EXIT", False),
        confirm_live=_s("I_UNDERSTAND_LIVE_RISK"),
        poll_buffer_sec=_i("POLL_BUFFER_SEC", 8),
        state_dir=Path(_s("STATE_DIR") or (ROOT / "state")),
        log_level=_s("LOG_LEVEL", "INFO").upper(),
        telegram_token=_s("TELEGRAM_TOKEN"),
        telegram_chat_id=_s("TELEGRAM_CHAT_ID"),
    )
    cfg.validate()
    return cfg
