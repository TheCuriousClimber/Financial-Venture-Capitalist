"""Zero-cost 24/7 local execution engine.

One ``tick`` per polling interval:
  1. pull the next bar / latest quotes from the feed (local or free HTTP, never an LLM)
  2. roll the trading day, enforce the 3% daily drawdown halt (flatten + no new entries)
  3. run the rule-based strategy -> risk gate -> broker
  4. sweep realized profit above principal into the 90/10 split
  5. snapshot equity to the ledger
  6. evaluate cost-gated bridge triggers (weekly review / regime shift) - usually a $0 no-op

Exceptions in a tick never kill the loop; after N consecutive failures the self-heal trigger fires
(cost-gated) and the daemon enters safe mode (no new entries) until ticks succeed again.

Run:  python -m trading_engine.daemon [--cycles N] [--interval SECONDS] [--fast]
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import traceback
from typing import Dict, List, Optional

from . import config
from .bridge.agent_trigger import AgentResult, AgentTrigger
from .broker.base import Broker, BrokerError, Fill, Order, Position, Quote
from .broker.mock_broker import MockBroker
from .core.risk_manager import OrderIntent, RiskManager
from .core.strategy import Signal, TrendPullbackStrategy
from .data.feed import Bar, Feed, make_feed
from .ledger import Ledger, cents, compute_split

log = logging.getLogger("trading_engine.daemon")


class Daemon:
    def __init__(self, ledger: Ledger, broker: Broker, feed: Feed, strategy: TrendPullbackStrategy,
                 risk: RiskManager, agent: AgentTrigger, poll_interval: int = config.POLL_INTERVAL_SECONDS):
        self.ledger = ledger
        self.broker = broker
        self.feed = feed
        self.strategy = strategy
        self.risk = risk
        self.agent = agent
        self.poll_interval = poll_interval
        self.symbols: List[str] = list(feed.symbols)
        self.history: Dict[str, List[Bar]] = {s: feed.history(s) for s in self.symbols}
        self.cycle = int(ledger.get_state("cycle", "0") or 0)
        self.now: float = 0.0
        self.consecutive_errors = 0
        self.safe_mode = False
        self.halt_new_entries = False
        self.last_weekly_review_ts: float = float(ledger.get_state("last_weekly_review_ts", "0") or 0)
        self.last_regime_ts: float = float(ledger.get_state("last_regime_ts", "0") or 0)
        self._stop = False
        self.fills: List[Fill] = []
        self.rejections: List[str] = []
        self.last_equity: Optional[float] = None
        last = ledger.equity_curve(limit=1)
        if last:
            self.last_equity = float(last[-1]["equity"])
        saved = ledger.get_state_json("strategy_params")
        if saved:
            strategy.update_params(saved)

    # ------------------------------------------------------------------ loop
    def run(self, max_cycles: Optional[int] = None, sleep_seconds: Optional[float] = None) -> None:
        sleep_seconds = self.poll_interval if sleep_seconds is None else sleep_seconds
        self._install_signals()
        self.ledger.log_event("INFO", "daemon_start", f"broker={self.broker.name} feed={type(self.feed).__name__}")
        done = 0
        while not self._stop and (max_cycles is None or done < max_cycles):
            self.safe_tick()
            done += 1
            if sleep_seconds and not self._stop and (max_cycles is None or done < max_cycles):
                time.sleep(sleep_seconds)
        self.ledger.log_event("INFO", "daemon_stop", f"cycles={done}")

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            log.info("signal %s received; stopping after current tick", signum)
            self._stop = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass   # not in main thread (tests)

    def safe_tick(self) -> None:
        try:
            self.tick()
            self.consecutive_errors = 0
            if self.safe_mode:
                self.safe_mode = False
                self.ledger.log_event("INFO", "safe_mode_exit", "tick succeeded; leaving safe mode")
        except Exception as e:  # noqa: BLE001
            self.consecutive_errors += 1
            tb = traceback.format_exc()
            log.error("tick failed (%d consecutive): %s", self.consecutive_errors, e)
            self.ledger.log_event("ERROR", "tick_error", str(e), {"traceback": tb[-2000:]})
            if self.consecutive_errors >= config.SELF_HEAL_CONSECUTIVE_ERRORS:
                self.safe_mode = True
                self._self_heal(tb)

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        self.cycle += 1
        bars = self.feed.next_bars()
        for s, b in bars.items():
            hist = self.history.setdefault(s, [])
            if not hist or b.ts > hist[-1].ts:
                hist.append(b)
        self.now = max((b.ts for b in bars.values()), default=time.time())

        quotes = self.broker.get_quotes(self.symbols)
        positions = self.broker.get_positions()
        equity = self._equity(quotes, positions)
        # Day-start equity is anchored to the PRIOR close so an opening gap counts toward the 3% limit.
        if self.risk.roll_day(self.now, self.last_equity if self.last_equity is not None else equity):
            self.risk.trades_today = self.ledger.trades_today(self.risk.day_start_ts)

        if self.risk.check_daily_drawdown(equity):
            if positions:
                self.ledger.log_event("WARN", "drawdown_halt",
                                      f"daily drawdown {self.risk.drawdown_pct(equity):.2%} breached; flattening")
                self._flatten(positions, quotes, equity, "daily drawdown halt")
        else:
            self._trade(quotes, positions, equity)

        positions = self.broker.get_positions()
        equity = self._equity(quotes, positions)
        equity = self._sweep_profits(equity)
        self.ledger.record_equity(ts=self.now, cycle=self.cycle, cash=self.broker.get_cash(),
                                  positions_value=equity - self.broker.get_cash(), equity=equity,
                                  swept_total=self.ledger.swept_total(), day_start_equity=self.risk.day_start_equity,
                                  drawdown_pct=self.risk.drawdown_pct(equity), open_positions=len(positions))
        self.last_equity = equity
        self._scheduled_triggers(equity, positions)
        self.ledger.set_state("cycle", self.cycle)

    # --------------------------------------------------------------- helpers
    def _equity(self, quotes: Dict[str, Quote], positions: Dict[str, Position]) -> float:
        mv = sum(p.qty * quotes[s].mid for s, p in positions.items() if s in quotes)
        return self.broker.get_cash() + mv

    def _trade(self, quotes: Dict[str, Quote], positions: Dict[str, Position], equity: float) -> None:
        cap = self.risk.max_position_notional(equity)
        signals = self.strategy.generate(self.history, positions, equity, cap)
        # intraday stop check on live quotes (bars may be stale between closes)
        for s, p in positions.items():
            stop = self.strategy.trailing_stops.get(s)
            if stop is not None and quotes[s].bid <= stop and not any(x.symbol == s for x in signals):
                signals.append(Signal(s, "EXIT", 0.0, 0.0, stop, "intraday stop on quote"))
        exits = [x for x in signals if x.action == "EXIT"]
        entries = [x for x in signals if x.action == "ENTER_LONG"]
        for sig in exits:
            self._execute(OrderIntent(sig.symbol, "SELL", 0.0, 0.0, sig.reason, exit_all=True), quotes, sig)
        if self.halt_new_entries or self.safe_mode:
            return
        for sig in entries:
            self._execute(OrderIntent(sig.symbol, "BUY", sig.target_notional, sig.expected_edge_bps, sig.reason), quotes, sig)

    def _execute(self, intent: OrderIntent, quotes: Dict[str, Quote], sig: Optional[Signal] = None) -> Optional[Fill]:
        quote = quotes[intent.symbol]
        positions = self.broker.get_positions()
        cash = self.broker.get_cash()
        equity = self._equity(quotes, positions)
        cost_bps = self.broker.estimate_round_trip_cost_bps(intent.symbol, max(intent.target_notional, 1.0), quote)
        decision = self.risk.evaluate(intent, quote, equity, cash, positions, cost_bps, self.cycle,
                                      self.broker.supports_fractional)
        if not decision.approved:
            self.rejections.append(f"{intent.symbol} {intent.side}: {decision.reason}")
            self.ledger.log_event("INFO", "order_rejected", f"{intent.symbol} {intent.side}: {decision.reason}",
                                  {"cycle": self.cycle, "checks": decision.checks})
            return None
        order = Order(intent.symbol, intent.side, decision.qty, reason=intent.reason,
                      expected_edge_bps=intent.expected_edge_bps)
        try:
            fill = self.broker.submit_order(order, quote)
        except BrokerError as e:
            self.rejections.append(f"{intent.symbol} {intent.side}: broker error {e}")
            self.ledger.log_event("WARN", "broker_error", f"{intent.symbol} {intent.side}: {e}", {"cycle": self.cycle})
            return None
        realized = fill.realized_pnl
        if intent.side == "SELL" and self.broker.name != "mock":
            held = positions.get(intent.symbol)
            realized = (fill.price - held.avg_cost) * fill.qty if held else 0.0
        self.ledger.record_trade(ts=fill.ts, cycle=self.cycle, symbol=fill.symbol, side=fill.side, qty=fill.qty,
                                 price=fill.price, commission=fill.commission, fees=fill.fees,
                                 slippage_bps=fill.slippage_bps, realized_pnl=realized, equity_at_order=equity,
                                 reason=intent.reason, broker=self.broker.name)
        self.risk.note_fill(fill.symbol, fill.side, self.cycle)
        if intent.side == "BUY":
            self.strategy.on_entry(fill.symbol, sig.stop_price if sig else None)
        else:
            self.strategy.on_exit(fill.symbol)
        self.fills.append(fill)
        log.info("cycle %d %s %s qty=%.4f @ %.4f notional=%.2f cost=%.4f pnl=%.4f (%s)", self.cycle, fill.side,
                 fill.symbol, fill.qty, fill.price, fill.notional, fill.total_cost, realized, intent.reason)
        return fill

    def _flatten(self, positions: Dict[str, Position], quotes: Dict[str, Quote], equity: float, reason: str) -> None:
        for s in list(positions):
            self._execute(OrderIntent(s, "SELL", 0.0, 0.0, reason, exit_all=True), quotes)

    def _sweep_profits(self, equity: float) -> float:
        d = self.ledger.pending_distribution(equity)
        if d < config.MIN_SWEEP_CAD:
            return equity
        split = compute_split(d)
        try:
            withdrawn = self.broker.withdraw(split.distributable, memo="profit sweep 90/10")
        except BrokerError as e:
            self.ledger.log_event("WARN", "sweep_failed", str(e))
            return equity
        equity_after = equity - split.distributable if withdrawn else equity
        self.ledger.record_split(ts=self.now, cycle=self.cycle, split=split, equity_after=equity_after)
        self.ledger.log_event("INFO", "profit_split",
                              f"swept {split.distributable:.2f}: owner {split.owner_disbursement:.2f} / "
                              f"reserve {split.operational_reserve:.2f}" + ("" if withdrawn else " (pending manual transfer)"),
                              {"cycle": self.cycle, "withdrawn_at_broker": withdrawn})
        log.info("profit split: %.2f -> owner %.2f, reserve %.2f", split.distributable, split.owner_disbursement,
                 split.operational_reserve)
        return equity_after

    # --------------------------------------------------------- bridge triggers
    def _context(self, equity: float, positions: Dict[str, Position]) -> dict:
        curve = [row["equity"] for row in self.ledger.equity_curve(limit=60)]
        recent = [dict(r) for r in self.ledger.trades(limit=15)]
        return {
            "cycle": self.cycle, "sim_ts": self.now, "equity": cents(equity),
            "positions": {s: {"qty": p.qty, "avg_cost": p.avg_cost} for s, p in positions.items()},
            "ledger": self.ledger.summary(equity), "equity_curve_60": [round(e, 2) for e in curve],
            "recent_trades": recent, "strategy_params": self.strategy.p,
            "rejections_recent": self.rejections[-10:], "risk_limits": vars(self.risk.limits),
        }

    def _apply_agent_result(self, result: AgentResult) -> None:
        if not result.invoked:
            return
        if result.param_overrides:
            applied = self.strategy.update_params(result.param_overrides)
            self.ledger.set_state_json("strategy_params", self.strategy.p)
            self.ledger.log_event("INFO", "params_updated", "agent overrides applied (clamped)", applied)
        if result.halt_new_entries != self.halt_new_entries:
            self.halt_new_entries = result.halt_new_entries
            self.ledger.log_event("WARN", "halt_toggle", f"halt_new_entries={self.halt_new_entries}")

    def _scheduled_triggers(self, equity: float, positions: Dict[str, Position]) -> None:
        regime = self.strategy.regime(self.history)
        if regime.shifted and self.now - self.last_regime_ts >= config.REGIME_TRIGGER_MIN_INTERVAL_SECONDS:
            self.last_regime_ts = self.now
            self.ledger.set_state("last_regime_ts", self.now)
            self.ledger.log_event("WARN", "regime_shift", "vol anomaly detected", regime.anomalies)
            ctx = self._context(equity, positions)
            ctx["anomalies"] = regime.anomalies
            self._apply_agent_result(self.agent.maybe_invoke("regime_shift", ctx))
        if self.now - self.last_weekly_review_ts >= config.WEEKLY_REVIEW_INTERVAL_SECONDS:
            self.last_weekly_review_ts = self.now
            self.ledger.set_state("last_weekly_review_ts", self.now)
            self._apply_agent_result(self.agent.maybe_invoke("weekly_review", self._context(equity, positions)))

    def _self_heal(self, tb: str) -> None:
        ctx = {"cycle": self.cycle, "consecutive_errors": self.consecutive_errors, "traceback": tb[-4000:],
               "ledger": self.ledger.summary()}
        result = self.agent.maybe_invoke("self_heal", ctx)
        self.ledger.log_event("WARN", "safe_mode", f"entering safe mode (no new entries); bridge invoked={result.invoked}")


# ---------------------------------------------------------------------- wiring
def build(broker_kind: str = config.BROKER, feed_kind: str = config.DATA_FEED, ledger_path: str = config.LEDGER_PATH,
          seed: int = config.SYNTHETIC_SEED, bridge_enabled: bool = config.CLAUDE_BRIDGE_ENABLED) -> Daemon:
    ledger = Ledger(ledger_path)
    feed = make_feed(feed_kind, seed=seed) if feed_kind == "synthetic" else make_feed(feed_kind)
    if broker_kind == "mock":
        broker: Broker = MockBroker(feed, starting_cash=ledger.principal, seed=seed)
    elif broker_kind == "questrade":
        from .broker.live_broker import LiveBroker
        broker = LiveBroker()
    else:
        raise ValueError(f"unknown BROKER {broker_kind}")
    strategy = TrendPullbackStrategy()
    risk = RiskManager(principal=ledger.principal)
    agent = AgentTrigger(ledger, enabled=bridge_enabled)
    return Daemon(ledger, broker, feed, strategy, risk, agent)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="zero-token local trading daemon")
    parser.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    parser.add_argument("--interval", type=float, default=None, help="seconds between cycles")
    parser.add_argument("--fast", action="store_true", help="no sleep between cycles (simulation)")
    parser.add_argument("--broker", default=config.BROKER)
    parser.add_argument("--feed", default=config.DATA_FEED)
    parser.add_argument("--ledger", default=config.LEDGER_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    d = build(args.broker, args.feed, args.ledger)
    sleep = 0 if args.fast else args.interval
    d.run(max_cycles=args.cycles, sleep_seconds=sleep)
    print(d.ledger.summary(d.broker.equity()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
