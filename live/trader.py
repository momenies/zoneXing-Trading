"""zoneXing Trading — live/paper runner.

    python -m live.trader              # run the loop (mode from .env)
    python -m live.trader --once       # evaluate one bar and exit
    python -m live.trader --status     # print state and exit
    python -m live.trader --flatten    # close every open position and exit
    python -m live.trader --selftest   # offline constraint audit (no network)

Hard constraints enforced at runtime, not just in the strategy:
  * the gate symbol (BTC) is never traded — it is refused in config validation
    and skipped again in the loop;
  * no position may oppose the BTC 4h gate — checked before every entry and on
    every bar (gate-flip exit).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .config import Config, load_config
from .engine import (ACTION_ENTER_LONG, ACTION_ENTER_SHORT, ACTION_EXIT, FLAT,
                     LONG, SHORT, Decision, LiveSignalEngine, SymbolState)
from .notify import Notifier

log = logging.getLogger("zonexing.trader")

STATE_FILE = "state.json"
TRADE_LOG = "trades.csv"
TRADE_FIELDS = ["opened_at", "closed_at", "symbol", "side", "qty", "entry",
                "exit", "pnl", "pnl_pct", "reason", "mode"]


def setup_logging(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format=fmt,
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cfg.state_dir / "zonexing.log", encoding="utf-8")],
    )


class Trader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = LiveSignalEngine(
            gate_bars=cfg.gate_bars, gate_th=cfg.gate_th, p_bars=cfg.p_bars,
            fast_ema=cfg.fast_ema, slow_ema=cfg.slow_ema,
            atr_period=cfg.atr_period, atr_sl_mult=cfg.atr_sl_mult,
            sl_pct=cfg.sl_pct, tp_pct=cfg.tp_pct, cooldown=cfg.cooldown,
            min_hold=cfg.min_hold, pivot_mode=cfg.pivot_mode,
            timeframe_ms=cfg.timeframe_ms, allow_short=cfg.allow_short,
        )
        self.notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
        self.states: Dict[str, SymbolState] = {c: SymbolState() for c in cfg.symbols}
        self.open_meta: Dict[str, dict] = {}
        self.consecutive_losses = 0
        self.day_key = ""
        self.day_start_equity = 0.0
        self.halted_reason = ""
        self.heartbeat: dict = {}      # evidence the loop is actually deciding
        self.cycle_errors = 0
        self._stop = False
        # lazy so --selftest works without ccxt/network
        self.data = None
        self.broker = None

    # ── wiring ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        from .broker import MarketData, make_broker, preflight
        self.data = MarketData(self.cfg)
        self.broker = make_broker(self.cfg, self.data)
        log.info("connected to %s (%s, %s mode%s)", self.cfg.exchange_id,
                 self.cfg.market_type, self.broker.kind,
                 ", TESTNET" if self.cfg.testnet else "")

        for problem in preflight(self.cfg, self.data, self.broker):
            log.warning("PREFLIGHT: %s", problem)
            self.notifier.send(f"⚠️ zoneXing preflight: {problem}")

    # ── persistence ─────────────────────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self.cfg.state_dir / STATE_FILE

    def load_state(self) -> None:
        if not self.state_path.is_file():
            log.info("no saved state — cold start, all symbols flat")
            return
        try:
            blob = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("state file unreadable (%s) — cold start", exc)
            return
        for code, d in (blob.get("symbols") or {}).items():
            if code in self.states:
                self.states[code] = SymbolState.from_dict(d)
        self.open_meta = blob.get("open_meta", {})
        self.consecutive_losses = int(blob.get("consecutive_losses", 0))
        self.day_key = blob.get("day_key", "")
        self.day_start_equity = float(blob.get("day_start_equity", 0.0))
        self.heartbeat = blob.get("heartbeat") or {}
        self.cycle_errors = int(blob.get("cycle_errors", 0))
        held = [c for c, s in self.states.items() if s.position.side != FLAT]
        log.info("state restored — open positions: %s", held or "none")

    def save_state(self) -> None:
        blob = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.cfg.mode,
            "symbols": {c: s.to_dict() for c, s in self.states.items()},
            "open_meta": self.open_meta,
            "consecutive_losses": self.consecutive_losses,
            "day_key": self.day_key,
            "day_start_equity": self.day_start_equity,
            "heartbeat": self.heartbeat,
            "cycle_errors": self.cycle_errors,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def log_trade(self, row: dict) -> None:
        path = self.cfg.state_dir / TRADE_LOG
        new = not path.is_file()
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})

    # ── reconciliation ──────────────────────────────────────────────────

    def reconcile(self) -> None:
        """Make saved state agree with what the exchange actually holds."""
        if not self.cfg.is_live:
            return
        actual = self.broker.exchange_positions()
        for code, state in self.states.items():
            on_book = actual.get(code, 0.0)
            if state.position.side != FLAT and on_book == 0.0:
                log.warning("%s: state says %s but exchange is flat — clearing state",
                            code, "long" if state.position.side > 0 else "short")
                self.engine.close_position(state, int(time.time() * 1000))
            elif state.position.side == FLAT and on_book != 0.0:
                price = self.data.last_price(code)
                side = LONG if on_book > 0 else SHORT
                log.warning("%s: untracked exchange position (%s %s) — adopting it "
                            "so exits are managed", code,
                            "long" if side > 0 else "short", abs(on_book))
                self.engine.open_position(state, side, price,
                                          int(time.time() * 1000), abs(on_book))
        self.save_state()

    # ── risk guards ─────────────────────────────────────────────────────

    def refresh_day(self, equity: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day_key:
            self.day_key = today
            self.day_start_equity = equity
            if self.halted_reason.startswith("daily loss"):
                self.halted_reason = ""
            log.info("new UTC day %s — equity baseline %.2f", today, equity)

    def entries_allowed(self, equity: float) -> bool:
        if self.halted_reason:
            return False
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halted_reason = (f"{self.consecutive_losses} consecutive losses "
                                  "— new entries halted until restart")
            log.error(self.halted_reason)
            self.notifier.send(f"⛔️ zoneXing halted: {self.halted_reason}")
            return False
        if self.day_start_equity > 0:
            dd = (self.day_start_equity - equity) / self.day_start_equity * 100
            if dd >= self.cfg.max_daily_loss_pct:
                self.halted_reason = (f"daily loss {dd:.2f}% ≥ "
                                      f"{self.cfg.max_daily_loss_pct}% — entries halted today")
                log.error(self.halted_reason)
                self.notifier.send(f"⛔️ zoneXing halted: {self.halted_reason}")
                return False
        open_count = sum(1 for s in self.states.values() if s.position.side != FLAT)
        if open_count >= self.cfg.max_open_positions:
            return False
        return True

    # ── order execution ─────────────────────────────────────────────────

    def do_entry(self, code: str, dec: Decision, equity: float) -> None:
        cfg = self.cfg
        state = self.states[code]
        notional = equity * cfg.invest_frac * cfg.leverage
        try:
            amount = self.broker.amount_for(code, notional, dec.price)
            if amount <= 0:
                log.warning("%s: computed size is zero — skipping", code)
                return
            order = self.broker.market_open(code, dec.side, amount, dec.price)
        except Exception as exc:
            log.error("%s: entry order failed: %s", code, exc)
            self.notifier.send(f"⚠️ zoneXing entry failed {code}: {exc}")
            return

        fill = float(order["price"])
        qty = float(order["amount"])
        self.engine.open_position(state, dec.side, fill, dec.bar_ts, qty)
        levels = self.engine.protective_levels(dec.side, fill)
        self.broker.place_protective(code, dec.side, qty, levels["sl"], levels["tp"])
        self.open_meta[code] = {
            "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": dec.reason,
        }
        side_txt = "LONG" if dec.side > 0 else "SHORT"
        msg = (f"🟢 OPEN {side_txt} {code} @ {fill:.6g} qty={qty:g} "
               f"({dec.reason}) | SL {levels['sl']:.6g} TP {levels['tp']:.6g} "
               f"[{self.broker.kind}]")
        log.info(msg)
        self.notifier.send(msg)
        self.save_state()

    def do_exit(self, code: str, dec: Decision) -> None:
        state = self.states[code]
        pos = state.position
        if pos.side == FLAT:
            return
        try:
            self.broker.cancel_protective(code)
            order = self.broker.market_close(code, pos.side, pos.qty, dec.price)
        except Exception as exc:
            log.error("%s: EXIT ORDER FAILED (%s) — position still open, retrying "
                      "next bar", code, exc)
            self.notifier.send(f"🚨 zoneXing exit failed {code}: {exc}")
            return

        fill = float(order["price"])
        gross = (fill - pos.entry_price) * pos.qty * pos.side
        fee = float(order.get("fee", 0.0) or 0.0)
        pnl = gross - fee if self.cfg.is_live else gross
        pnl_pct = (fill - pos.entry_price) / pos.entry_price * pos.side * 100
        if hasattr(self.broker, "settle"):
            self.broker.settle(gross)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0

        self.log_trade({
            "opened_at": self.open_meta.get(code, {}).get("opened_at", ""),
            "closed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": code, "side": "long" if pos.side > 0 else "short",
            "qty": f"{pos.qty:g}", "entry": f"{pos.entry_price:.8g}",
            "exit": f"{fill:.8g}", "pnl": f"{pnl:.4f}", "pnl_pct": f"{pnl_pct:.3f}",
            "reason": dec.reason, "mode": self.broker.kind,
        })
        self.open_meta.pop(code, None)
        self.engine.close_position(state, dec.bar_ts)
        emoji = "✅" if pnl >= 0 else "🔻"
        msg = (f"{emoji} CLOSE {code} @ {fill:.6g} ({dec.reason}) "
               f"pnl {pnl:+.2f} ({pnl_pct:+.2f}%) [{self.broker.kind}]")
        log.info(msg)
        self.notifier.send(msg)
        self.save_state()

    def flatten(self, reason: str = "manual flatten") -> None:
        for code, state in self.states.items():
            if state.position.side == FLAT:
                continue
            price = self.data.last_price(code)
            self.do_exit(code, Decision(symbol=code, action=ACTION_EXIT, reason=reason,
                                        price=price, bar_ts=int(time.time() * 1000)))

    # ── one cycle ───────────────────────────────────────────────────────

    def cycle(self) -> None:
        cfg = self.cfg
        gate_df = self.data.fetch(cfg.gate_symbol)
        if len(gate_df) < cfg.gate_bars:
            log.warning("gate history too short (%d/%d bars) — standing aside",
                        len(gate_df), cfg.gate_bars)
            return
        gate_arr = self.engine.gate_series(gate_df["close"].to_numpy(dtype=float))
        gate = float(gate_arr[-1])
        gate_txt = {1.0: "UP → long only", -1.0: "DOWN → short only",
                    0.0: "FLAT → no new entries"}[gate]
        log.info("BTC 4h gate: %s (close %.2f)", gate_txt, gate_df["close"].iloc[-1])

        equity = self.broker.equity()
        self.refresh_day(equity)
        may_enter = self.entries_allowed(equity)

        for code in cfg.symbols:
            if code == cfg.gate_symbol:      # belt and braces: never trade the gate
                continue
            state = self.states[code]
            try:
                df = self.data.fetch(code)
            except Exception as exc:
                log.error("%s: data fetch failed (%s) — skipping this bar", code, exc)
                continue
            bar_ts = int(df.index[-1]) if len(df) else 0
            if bar_ts and bar_ts == state.last_bar_ts:
                log.debug("%s: bar %s already processed", code, bar_ts)
                continue

            dec = self.engine.step(code, df, gate, state)
            log.info("%s: %s — %s (close %.6g)", code, dec.action, dec.reason, dec.price)

            if dec.action == ACTION_EXIT:
                self.do_exit(code, dec)
            elif dec.action in (ACTION_ENTER_LONG, ACTION_ENTER_SHORT):
                # final hard-constraint check before any money moves
                if (dec.side == LONG and gate != 1.0) or (dec.side == SHORT and gate != -1.0):
                    log.error("%s: entry blocked — would oppose the gate", code)
                    continue
                if not may_enter:
                    log.info("%s: entry suppressed (%s)", code,
                             self.halted_reason or "position/risk limit reached")
                    continue
                self.do_entry(code, dec, equity)
                may_enter = self.entries_allowed(self.broker.equity())
            self.heartbeat.setdefault("decisions", {})[code] = {
                "action": dec.action, "reason": dec.reason,
                "price": dec.price, "bar_ts": dec.bar_ts,
            }

        self.heartbeat.update({
            "cycle_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gate": gate,
            "gate_bar_ts": int(gate_df.index[-1]),
            "equity": equity,
        })
        self.save_state()

    # ── main loop ───────────────────────────────────────────────────────

    def sleep_to_next_bar(self) -> None:
        tf = self.cfg.timeframe_ms / 1000
        now = time.time()
        nxt = (now // tf + 1) * tf + self.cfg.poll_buffer_sec
        wait = max(1.0, nxt - now)
        log.debug("sleeping %.0fs to next bar close", wait)
        deadline = time.time() + wait
        while time.time() < deadline and not self._stop:
            time.sleep(min(1.0, deadline - time.time()))

    def run(self) -> None:
        self.connect()
        self.load_state()
        self.reconcile()

        banner = (f"zoneXing Trading started — {self.broker.kind.upper()} mode on "
                  f"{self.cfg.exchange_id}/{self.cfg.market_type}, "
                  f"gate {self.cfg.gate_symbol}, symbols {','.join(self.cfg.symbols)}, "
                  f"pivots={self.cfg.pivot_mode}, invest_frac={self.cfg.invest_frac}, "
                  f"lev={self.cfg.leverage}")
        log.info(banner)
        self.notifier.send("🚀 " + banner)

        def _sig(signum, _frame):
            log.info("signal %s received — shutting down after this cycle", signum)
            self._stop = True

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        while not self._stop:
            try:
                self.cycle()
            except Exception as exc:
                self.cycle_errors += 1
                log.exception("cycle failed: %s", exc)
                self.notifier.send(f"⚠️ zoneXing cycle error: {exc}")
            if self._stop:
                break
            self.sleep_to_next_bar()

        if self.cfg.close_on_exit:
            log.info("CLOSE_ON_EXIT=true — flattening")
            self.flatten("shutdown")
        self.save_state()
        log.info("stopped cleanly")
        self.notifier.send("🛑 zoneXing stopped")

    def health(self) -> tuple:
        """Answer one question: is this bot still deciding on fresh bars?

        A live process is not proof of life — a bot whose data fetch fails every
        cycle stays "running" forever. This checks the evidence the loop leaves
        behind. Returns (ok, lines) so it can drive a non-zero exit code for
        cron/monitoring.
        """
        cfg = self.cfg
        lines: List[str] = []
        problems: List[str] = []
        now = datetime.now(timezone.utc)
        tf_sec = cfg.timeframe_ms / 1000

        if not self.state_path.is_file():
            return False, [f"no state file at {self.state_path} — "
                           "the bot has never completed a cycle here"]
        blob = json.loads(self.state_path.read_text(encoding="utf-8"))
        hb = blob.get("heartbeat") or {}

        saved_at = blob.get("saved_at")
        age = None
        if saved_at:
            age = (now - datetime.fromisoformat(saved_at)).total_seconds()
            allowed = tf_sec * 2 + cfg.poll_buffer_sec + 60
            mark = "OK" if age <= allowed else "STALE"
            lines.append(f"[{mark}] last state write: {age / 60:.1f} min ago "
                         f"(expected every {tf_sec / 60:.0f} min)")
            if age > allowed:
                problems.append("the loop has not completed a cycle recently — "
                                "check the service is running and the logs")

        cycle_at = hb.get("cycle_at")
        if not cycle_at:
            problems.append("no cycle heartbeat recorded — the bot may be failing "
                            "before it reaches a decision (data fetch? credentials?)")
        else:
            bar_ts = hb.get("gate_bar_ts")
            if bar_ts:
                bar_age = (now.timestamp() * 1000 - bar_ts) / 1000
                mark = "OK" if bar_age <= tf_sec * 3 else "STALE"
                lines.append(f"[{mark}] newest {cfg.timeframe} bar seen: "
                             f"{bar_age / 60:.1f} min old")
                if bar_age > tf_sec * 3:
                    problems.append("market data is behind — the exchange feed may be "
                                    "blocked or the server clock is wrong")
            gate = hb.get("gate")
            gate_txt = {1.0: "UP (long only)", -1.0: "DOWN (short only)",
                        0.0: "FLAT (no new entries)"}.get(gate, str(gate))
            lines.append(f"[INFO] gate: {gate_txt} | equity: {hb.get('equity', 0):.2f}")

        decisions = (hb.get("decisions") or {})
        if decisions:
            lines.append("[INFO] last decision per symbol:")
            for code, d in decisions.items():
                lines.append(f"         {code}: {d.get('action')} — {d.get('reason')}")
        elif cycle_at:
            problems.append("a cycle ran but no symbol was evaluated — check SYMBOLS")

        errors = int(blob.get("cycle_errors", 0))
        lines.append(f"[{'OK' if not errors else 'WARN'}] cycle errors since start: {errors}")

        halted = blob.get("halted_reason") or self.halted_reason
        if halted:
            problems.append(f"entries halted: {halted}")

        open_pos = [c for c, d in (blob.get("symbols") or {}).items()
                    if (d.get("position") or {}).get("side")]
        lines.append(f"[INFO] open positions: {', '.join(open_pos) if open_pos else 'none'}")

        trades = self.cfg.state_dir / TRADE_LOG
        if trades.is_file():
            rows = trades.read_text(encoding="utf-8").strip().splitlines()
            lines.append(f"[INFO] closed trades logged: {max(0, len(rows) - 1)}")
        else:
            lines.append("[INFO] closed trades logged: 0 (none closed yet)")

        for pr in problems:
            lines.append(f"[PROBLEM] {pr}")
        return (not problems), lines

    def status(self) -> str:
        lines = [f"mode={self.cfg.mode} exchange={self.cfg.exchange_id}/{self.cfg.market_type}",
                 f"gate={self.cfg.gate_symbol} (never traded)",
                 f"halted: {self.halted_reason or 'no'}",
                 f"consecutive losses: {self.consecutive_losses}"]
        for code, s in self.states.items():
            p = s.position
            if p.side == FLAT:
                lines.append(f"  {code}: flat")
            else:
                lines.append(f"  {code}: {'LONG' if p.side > 0 else 'SHORT'} "
                             f"qty={p.qty:g} entry={p.entry_price:.6g}")
        return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="live.trader", description="zoneXing live trader")
    ap.add_argument("--env", help="path to a .env file")
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    ap.add_argument("--status", action="store_true", help="print saved state and exit")
    ap.add_argument("--health", action="store_true",
                    help="check the bot is alive and deciding; exit 1 if not")
    ap.add_argument("--flatten", action="store_true", help="close all positions and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="offline constraint audit — no network, no keys")
    args = ap.parse_args(argv)

    if args.selftest:
        from .selftest import run_selftest
        return run_selftest()

    try:
        cfg = load_config(args.env)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg)
    trader = Trader(cfg)

    if args.health:
        trader.load_state()
        ok, lines = trader.health()
        print("\n".join(lines))
        print("\nHEALTHY" if ok else "\nUNHEALTHY — see [PROBLEM] lines above")
        return 0 if ok else 1

    if args.status:
        trader.load_state()
        print(trader.status())
        return 0

    try:
        trader.connect()
    except Exception as exc:
        log.error("could not reach %s: %s", cfg.exchange_id, exc)
        log.error("check outbound network access, EXCHANGE/MARKET_TYPE and API keys")
        return 3
    trader.load_state()
    trader.reconcile()

    if args.flatten:
        trader.flatten()
        trader.save_state()
        return 0
    if args.once:
        trader.cycle()
        print(trader.status())
        return 0

    trader.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
