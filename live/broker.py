"""Market data + order execution for the zoneXing live trader.

Two brokers share one interface:

  * ``PaperBroker`` – real market data, simulated fills (taker fee + slippage).
    Used by ``MODE=paper``; this is what you run first on a new server.
  * ``LiveBroker``  – real orders through ccxt (``MODE=live``).

Market data always comes from the public ccxt client, so paper mode needs no
API keys at all.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import pandas as pd

try:  # ccxt is only needed at runtime, not for the offline self-test
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None

from .config import Config

log = logging.getLogger("zonexing.broker")


class BrokerError(RuntimeError):
    pass


# ── market data ─────────────────────────────────────────────────────────

class MarketData:
    """Public OHLCV feed with retry/backoff. Returns CLOSED bars only."""

    def __init__(self, cfg: Config):
        if ccxt is None:
            raise BrokerError("ccxt is not installed — run: pip install -r requirements.txt")
        klass = getattr(ccxt, cfg.exchange_id, None)
        if klass is None:
            raise BrokerError(f"unknown exchange id {cfg.exchange_id!r}")
        self.cfg = cfg
        self.ex = klass({
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": cfg.market_type},
        })
        if cfg.testnet and self.ex.has.get("sandbox", True):
            try:
                self.ex.set_sandbox_mode(True)
            except Exception as exc:  # pragma: no cover
                log.warning("sandbox mode unavailable for data feed: %s", exc)
        self.ex.load_markets()

    def fetch(self, code: str, limit: Optional[int] = None,
              retries: int = 4) -> pd.DataFrame:
        symbol = self.cfg.ccxt_symbol(code)
        limit = limit or self.cfg.history_bars
        delay = 2.0
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                rows = self.ex.fetch_ohlcv(symbol, self.cfg.timeframe, limit=limit)
                break
            except Exception as exc:  # network / rate-limit
                last_exc = exc
                log.warning("fetch_ohlcv %s failed (%d/%d): %s",
                            symbol, attempt + 1, retries, exc)
                time.sleep(delay)
                delay *= 2
        else:
            raise BrokerError(f"could not fetch {symbol}: {last_exc}")

        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.astype({c: float for c in ("open", "high", "low", "close", "volume")})
        df["ts"] = df["ts"].astype("int64")
        df = df.set_index("ts").sort_index()
        # Drop the still-forming bar: a bar is closed once ts + timeframe <= now.
        now_ms = int(time.time() * 1000)
        return df[df.index + self.cfg.timeframe_ms <= now_ms]

    def last_price(self, code: str) -> float:
        return float(self.ex.fetch_ticker(self.cfg.ccxt_symbol(code))["last"])


# ── brokers ─────────────────────────────────────────────────────────────

class BaseBroker:
    kind = "base"

    def equity(self) -> float:
        raise NotImplementedError

    def amount_for(self, code: str, notional: float, price: float) -> float:
        raise NotImplementedError

    def market_open(self, code: str, side: float, amount: float, price: float) -> dict:
        raise NotImplementedError

    def market_close(self, code: str, side: float, amount: float, price: float) -> dict:
        raise NotImplementedError

    def place_protective(self, code: str, side: float, amount: float,
                         sl: float, tp: float) -> None:
        return None

    def cancel_protective(self, code: str) -> None:
        return None

    def exchange_positions(self) -> Dict[str, float]:
        """symbol code → signed base amount (0 when flat)."""
        return {}


class PaperBroker(BaseBroker):
    """Simulated fills against real prices — no keys, no exchange risk."""

    kind = "paper"

    def __init__(self, cfg: Config, data: MarketData):
        self.cfg = cfg
        self.data = data
        self.cash = float(cfg.paper_equity)
        self.realised = 0.0

    def equity(self) -> float:
        return self.cash

    def _fill_price(self, side: float, price: float, closing: bool) -> float:
        # Buying (open long / close short) slips up; selling slips down.
        buying = (side > 0) != closing
        return price * (1 + self.cfg.slippage) if buying else price * (1 - self.cfg.slippage)

    def amount_for(self, code: str, notional: float, price: float) -> float:
        try:
            amt = float(self.data.ex.amount_to_precision(
                self.cfg.ccxt_symbol(code), notional / price))
        except Exception:
            amt = notional / price
        return amt

    def market_open(self, code: str, side: float, amount: float, price: float) -> dict:
        fill = self._fill_price(side, price, closing=False)
        fee = fill * amount * self.cfg.taker_rate
        self.cash -= fee
        return {"id": f"paper-{int(time.time()*1000)}", "price": fill,
                "amount": amount, "fee": fee, "paper": True}

    def market_close(self, code: str, side: float, amount: float, price: float) -> dict:
        fill = self._fill_price(side, price, closing=True)
        fee = fill * amount * self.cfg.taker_rate
        self.cash -= fee
        return {"id": f"paper-{int(time.time()*1000)}", "price": fill,
                "amount": amount, "fee": fee, "paper": True}

    def settle(self, pnl: float) -> None:
        self.cash += pnl
        self.realised += pnl


class LiveBroker(BaseBroker):
    """Real orders via ccxt. Requires API keys and I_UNDERSTAND_LIVE_RISK=yes."""

    kind = "live"

    def __init__(self, cfg: Config, data: MarketData):
        if ccxt is None:
            raise BrokerError("ccxt is not installed")
        self.cfg = cfg
        self.data = data
        klass = getattr(ccxt, cfg.exchange_id)
        self.ex = klass({
            "apiKey": cfg.api_key,
            "secret": cfg.api_secret,
            "password": cfg.api_password or None,
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": cfg.market_type},
        })
        if cfg.testnet:
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()
        if cfg.market_type == "swap" and cfg.leverage != 1:
            for code in cfg.symbols:
                try:
                    self.ex.set_leverage(cfg.leverage, cfg.ccxt_symbol(code))
                except Exception as exc:
                    log.warning("set_leverage %s failed: %s", code, exc)

    def equity(self) -> float:
        bal = self.ex.fetch_balance()
        total = bal.get("total", {})
        for quote in ("USDT", "USD", "USDC"):
            if quote in total and total[quote]:
                return float(total[quote])
        raise BrokerError("could not read a USDT/USD balance from the exchange")

    def amount_for(self, code: str, notional: float, price: float) -> float:
        symbol = self.cfg.ccxt_symbol(code)
        market = self.ex.market(symbol)
        raw = notional / price
        if market.get("contractSize"):
            raw = raw / float(market["contractSize"])
        amt = float(self.ex.amount_to_precision(symbol, raw))
        limits = market.get("limits", {})
        min_amt = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if min_amt and amt < float(min_amt):
            raise BrokerError(
                f"{code}: size {amt} below exchange minimum {min_amt} — "
                "raise INVEST_FRAC/LEVERAGE or fund the account")
        if min_cost and amt * price < float(min_cost):
            raise BrokerError(
                f"{code}: notional {amt * price:.2f} below minimum {min_cost}")
        return amt

    def _order(self, code: str, ccxt_side: str, amount: float, params: dict) -> dict:
        symbol = self.cfg.ccxt_symbol(code)
        order = self.ex.create_order(symbol, "market", ccxt_side, amount, None, params)
        price = order.get("average") or order.get("price") or self.data.last_price(code)
        return {"id": order.get("id"), "price": float(price),
                "amount": float(order.get("filled") or amount), "raw": order}

    def market_open(self, code: str, side: float, amount: float, price: float) -> dict:
        params = {}
        if self.cfg.market_type == "swap":
            params["reduceOnly"] = False
        return self._order(code, "buy" if side > 0 else "sell", amount, params)

    def market_close(self, code: str, side: float, amount: float, price: float) -> dict:
        params = {"reduceOnly": True} if self.cfg.market_type == "swap" else {}
        return self._order(code, "sell" if side > 0 else "buy", amount, params)

    def place_protective(self, code: str, side: float, amount: float,
                         sl: float, tp: float) -> None:
        """Reduce-only stop-loss + take-profit so an outage cannot run away."""
        if not self.cfg.protective_orders or self.cfg.market_type != "swap":
            return
        symbol = self.cfg.ccxt_symbol(code)
        close_side = "sell" if side > 0 else "buy"
        for kind, trigger in (("stop", sl), ("take_profit", tp)):
            try:
                self.ex.create_order(
                    symbol, "market", close_side, amount, None,
                    {"reduceOnly": True,
                     "stopLossPrice" if kind == "stop" else "takeProfitPrice":
                         float(self.ex.price_to_precision(symbol, trigger))},
                )
            except Exception as exc:
                log.warning("protective %s order for %s rejected: %s", kind, code, exc)

    def cancel_protective(self, code: str) -> None:
        try:
            self.ex.cancel_all_orders(self.cfg.ccxt_symbol(code))
        except Exception as exc:
            log.warning("cancel_all_orders %s failed: %s", code, exc)

    def exchange_positions(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if self.cfg.market_type != "swap":
            return out
        try:
            positions = self.ex.fetch_positions([self.cfg.ccxt_symbol(c)
                                                 for c in self.cfg.symbols])
        except Exception as exc:
            log.warning("fetch_positions failed: %s", exc)
            return out
        by_symbol = {self.cfg.ccxt_symbol(c): c for c in self.cfg.symbols}
        for p in positions:
            code = by_symbol.get(p.get("symbol"))
            if not code:
                continue
            contracts = float(p.get("contracts") or 0)
            if contracts:
                out[code] = contracts if p.get("side") == "long" else -contracts
        return out


def make_broker(cfg: Config, data: MarketData) -> BaseBroker:
    return LiveBroker(cfg, data) if cfg.is_live else PaperBroker(cfg, data)
