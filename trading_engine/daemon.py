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
from .notifications import WebhookNotifier

log = logging.getLogger("trading_engine.daemon")


class Daemon:
    def __init__(self, ledger: Ledger, broker: Broker, feed: Feed, strategy: TrendPullbackStrategy,
                 risk: RiskManager, agent: AgentTrigger, poll_interval: int = config.POLL_INTERVAL_SECONDS,
                 notifier: Optional[WebhookNotifier] = None):
        self.ledger = ledger
        self.broker = broker
        self.notifier = notifier or WebhookNotifier(url="", ledger=ledger)
        self.feed = feed
        self.strategy = strategy
        self.risk = risk
        self.agent = agent
        self.poll_interval = poll_interval
        self.symbols: List[str] = list(feed.symbols)
        # A dead feed at startup must not be fatal: start with empty history and let ticks fail into safe mode.
        self.history: Dict[str, List[Bar]] = {}
        for s in self.symbols:
            try:
                self.history[s] = feed.history(s)
            except Exception as e:  # noqa: BLE001
                self.history[s] = []
                log.warning("history unavailable for %s at startup: %s", s, e)
                ledger.log_event("WARN", "feed_error", f"startup history unavailable for {s}: {e}"[:300])
        self.cycle = int(ledger.get_state("cycle", "0") or 0)
        self.bar_index = int(ledger.get_state("bar_index", "0") or 0)   # increments only on a NEW bar
        self.now: float = 0.0
        self.consecutive_errors = 0
        self.safe_mode = False
        self.halt_new_entries = False
        self.last_weekly_review_ts: float = float(ledger.get_state("last_weekly_review_ts", "0") or 0)
        self.last_regime_ts: float = float(ledger.get_state("last_regime_ts", "0") or 0)
        self._stop = False
        self.fills: List[Fill] = []
        self.rejections: List[str] = []
        self._live_feed = type(feed).__name__ != "SyntheticFeed"
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
        new_bar = False
        for s, b in bars.items():
            hist = self.history.setdefault(s, [])
            if not hist or b.ts > hist[-1].ts:
                hist.append(b)
                new_bar = True
        if new_bar:
            self.bar_index += 1
        # live feeds: wall clock drives the trading day; synthetic: the bar timestamp does
        self.now = time.time() if self._live_feed else max((b.ts for b in bars.values()), default=time.time())

        quotes = self.broker.get_quotes(self.symbols)
        positions = self.broker.get_positions()
        equity = self._equity(quotes, positions)
        # Day-start equity is anchored to the PRIOR close so an opening gap counts toward the 3% limit.
        if self.risk.roll_day(self.now, self.last_equity if self.last_equity is not None else equity):
            self.risk.trades_today = self.ledger.trades_today(self.risk.day_start_ts)

        if self.risk.check_daily_drawdown(equity):
            if positions:
                msg = f"daily drawdown {self.risk.drawdown_pct(equity):.2%} breached; flattening {len(positions)} position(s)"
                self.ledger.log_event("WARN", "drawdown_halt", msg)
                self._flatten(positions, quotes, equity, "daily drawdown halt")
                self.notifier.notify_alert("daily drawdown halt", msg, {"equity": cents(equity), "cycle": self.cycle})
        else:
            self._trade(quotes, positions, equity)

        positions = self.broker.get_positions()
        equity = self._equity(quotes, positions)
        equity = self.reconcile_90_10_split(equity)
        self.ledger.record_equity(ts=self.now, cycle=self.cycle, cash=self.broker.get_cash(),
                                  positions_value=equity - self.broker.get_cash(), equity=equity,
                                  swept_total=self.ledger.swept_total(), day_start_equity=self.risk.day_start_equity,
                                  drawdown_pct=self.risk.drawdown_pct(equity), open_positions=len(positions))
        self.last_equity = equity
        self._scheduled_triggers(equity, positions)
        self.ledger.set_state("cycle", self.cycle)
        self.ledger.set_state("bar_index", self.bar_index)

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
        decision = self.risk.evaluate(intent, quote, equity, cash, positions, cost_bps, self.bar_index,
                                      self.broker.supports_fractional, min_qty=self.broker.min_qty(intent.symbol))
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
        self.risk.note_fill(fill.symbol, fill.side, self.bar_index)
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

    def reconcile_90_10_split(self, equity: float) -> float:
        """Sweep realized profit above principal: 10% operational reserve, 90% owner disbursement."""
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
        self.notifier.notify_sweep(realized_profit=self.ledger.realized_pnl_total(), distributable=split.distributable,
                                   owner=split.owner_disbursement, reserve=split.operational_reserve,
                                   owner_total=self.ledger.owner_total(), reserve_total=self.ledger.reserve_total(),
                                   equity_after=equity_after, credit_remaining=self.ledger.credit_remaining_cad(),
                                   withdrawn_at_broker=withdrawn, cycle=self.cycle)
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
        msg = f"entering safe mode (no new entries); bridge invoked={result.invoked}"
        self.ledger.log_event("WARN", "safe_mode", msg)
        self.notifier.notify_alert("safe mode", msg, {"consecutive_errors": self.consecutive_errors, "cycle": self.cycle})


# ---------------------------------------------------------------------- wiring
def build(broker_kind: str = config.BROKER, feed_kind: str = config.DATA_FEED, ledger_path: str = config.LEDGER_PATH,
          seed: int = config.SYNTHETIC_SEED, bridge_enabled: bool = config.CLAUDE_BRIDGE_ENABLED,
          paper_live_feed: bool = config.PAPER_LIVE_FEED, live_enabled: bool = config.LIVE_TRADING_ENABLED) -> Daemon:
    """Wire the daemon. Modes:
      * synthetic + mock                  -> offline dry run
      * kraken_live/yahoo + PAPER_LIVE_FEED -> paper soak: real prices/spreads, MockBroker, PAPER_FEE_BPS fees
      * kraken_live + BROKER=kraken + LIVE_TRADING_ENABLED -> real orders (requires PAPER_LIVE_FEED=false)
    """
    ledger = Ledger(ledger_path)
    if feed_kind == "synthetic":
        feed = make_feed(feed_kind, seed=seed)
    elif feed_kind == "kraken_live":
        def _on_source(source, reason):
            ledger.log_event("WARN" if source != "kraken" else "INFO", "feed_source", f"market data source -> {source}: {reason}")
            log.warning("market data source -> %s (%s)", source, reason)
        feed = make_feed(feed_kind, on_source_change=_on_source)
    else:
        feed = make_feed(feed_kind)
    mode = "dry_run"
    if broker_kind == "kraken" and paper_live_feed:
        ledger.log_event("WARN", "paper_override", "PAPER_LIVE_FEED=true: BROKER=kraken ignored, routing to MockBroker")
        broker_kind = "mock"
    if broker_kind == "mock":
        schedule = config.FEE_TABLES["kraken_paper"] if config.ASSET_UNIVERSE == "crypto" else None
        broker: Broker = MockBroker(feed, starting_cash=ledger.principal, seed=seed, fee_schedule=schedule)
        if feed_kind != "synthetic":
            mode = "paper_soak"
    elif broker_kind == "kraken":
        from .broker.live_broker import KrakenBroker
        broker = KrakenBroker(enabled=live_enabled, cost_basis_store=ledger)
        broker.load_pair_rules()
        mode = "live"
    else:
        raise ValueError(f"unknown BROKER {broker_kind}")
    strategy = TrendPullbackStrategy()
    risk = RiskManager(principal=ledger.principal)
    agent = AgentTrigger(ledger, enabled=bridge_enabled)
    notifier = WebhookNotifier(ledger=ledger)
    d = Daemon(ledger, broker, feed, strategy, risk, agent, notifier=notifier)
    d.mode = mode
    ledger.log_event("INFO", "build", f"mode={mode} broker={broker.name} feed={type(feed).__name__} "
                                     f"universe={config.ASSET_UNIVERSE} fees={broker.fees.broker} webhook={notifier.kind if notifier.enabled else 'off'}")
    log.info("mode=%s broker=%s feed=%s universe=%s fee_table=%s", mode, broker.name, type(feed).__name__,
             config.ASSET_UNIVERSE, broker.fees.broker)
    return d


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
    problems = config.validate()
    if problems:
        for pr in problems:
            log.error("config: %s", pr)
        return 2
    d = build(args.broker, args.feed, args.ledger)
    sleep = 0 if args.fast else args.interval
    d.run(max_cycles=args.cycles, sleep_seconds=sleep)
    print(d.ledger.summary(d.broker.equity()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
