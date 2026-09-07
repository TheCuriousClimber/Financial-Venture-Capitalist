"""Unit tests: risk gates, ledger split math, broker fee model, strategy bounds, bridge gating, daemon safety."""
from __future__ import annotations

import _env  # noqa: F401  pins config before trading_engine is imported

import time
import unittest
from types import SimpleNamespace

from trading_engine import config
from trading_engine.bridge.agent_trigger import AgentTrigger, estimate_cost_cad
from trading_engine.broker.base import BrokerError, Order, Position, Quote
from trading_engine.broker.mock_broker import MockBroker
from trading_engine.core import indicators as ind
from trading_engine.core.risk_manager import OrderIntent, RiskManager
from trading_engine.core.strategy import TrendPullbackStrategy
from trading_engine.daemon import Daemon
from trading_engine.data.feed import SyntheticFeed
from trading_engine.dry_run import run_dry
from trading_engine.ledger import Ledger, compute_split


def quote(sym="XIU.TO", mid=40.0, spread_bps=5.0):
    half = mid * spread_bps / 1e4 / 2
    return Quote(sym, mid - half, mid + half, mid, ts=time.time())


class TestLedgerSplit(unittest.TestCase):
    def test_split_legs_sum_exactly(self):
        for d in (0.01, 0.05, 0.47, 1.23, 12.34, 999.99):
            s = compute_split(d)
            self.assertAlmostEqual(s.owner_disbursement + s.operational_reserve, s.distributable, places=9)
            self.assertAlmostEqual(s.operational_reserve, round(d * 0.10 + 1e-12, 2), places=9)

    def test_negative_never_distributes(self):
        self.assertEqual(compute_split(-5).distributable, 0.0)

    def test_pending_distribution_capped_by_principal_headroom(self):
        l = Ledger(":memory:")
        l.record_trade(ts=1, cycle=1, symbol="XIU.TO", side="SELL", qty=1, price=10, commission=0, fees=0,
                       slippage_bps=0, realized_pnl=2.00, equity_at_order=100, reason="t", broker="mock")
        self.assertEqual(l.pending_distribution(equity=101.0), 1.0)    # headroom binds
        self.assertEqual(l.pending_distribution(equity=105.0), 2.0)    # realized binds
        self.assertEqual(l.pending_distribution(equity=99.0), 0.0)     # below principal: nothing

    def test_reserve_extends_credit_pool(self):
        l = Ledger(":memory:")
        l.record_split(ts=1, cycle=1, split=compute_split(1.00), equity_after=100)
        self.assertAlmostEqual(l.credit_remaining_cad(), 100.10)
        l.record_tokens(purpose="weekly_review", model="claude-fable-5-1", input_tokens=1000, output_tokens=100,
                        cost_usd=0.1, cost_cad=0.137)
        self.assertAlmostEqual(l.credit_remaining_cad(), 100.10 - 0.137)


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()
        self.rm.roll_day(time.time(), 100.0)

    def eval(self, intent, equity=100.0, cash=100.0, positions=None, cost_bps=8.0, q=None):
        return self.rm.evaluate(intent, q or quote(), equity, cash, positions or {}, cost_bps, 10, True)

    CAP = config.RISK.max_position_pct * 100.0   # $10 at a $100 principal

    def test_caps_at_max_position_pct(self):
        d = self.eval(OrderIntent("XIU.TO", "BUY", 50.0, 100.0, "t"))
        self.assertTrue(d.approved)
        self.assertLessEqual(d.notional, self.CAP)
        self.assertGreater(d.notional, self.CAP * 0.98)

    def test_cap_does_not_grow_above_principal(self):
        d = self.eval(OrderIntent("XIU.TO", "BUY", 50.0, 100.0, "t"), equity=140.0, cash=140.0)
        self.assertLessEqual(d.notional, self.CAP)

    def test_existing_position_reduces_room(self):
        pos = {"XIU.TO": Position("XIU.TO", 0.2, 40.0)}   # $8 held at a $40 mid
        d = self.eval(OrderIntent("XIU.TO", "BUY", 50.0, 100.0, "t"), positions=pos)
        self.assertLessEqual(d.notional, self.CAP - 8.0 + 0.05)

    def test_no_short(self):
        d = self.eval(OrderIntent("XIU.TO", "SELL", 5.0, 0.0, "t"))
        self.assertFalse(d.approved)

    def test_no_leverage(self):
        d = self.eval(OrderIntent("XIU.TO", "BUY", 5.0, 100.0, "t"), cash=2.0)
        self.assertTrue(d.approved)
        self.assertLess(d.notional, 2.0)

    def test_fee_gate_rejects_questrade_style_commissions(self):
        d = self.eval(OrderIntent("XIU.TO", "BUY", 5.0, 100.0, "t"), cost_bps=9900.0)
        self.assertFalse(d.approved)
        self.assertIn("cost", d.reason)

    def test_fee_to_edge_ratio(self):
        d = self.eval(OrderIntent("XIU.TO", "BUY", 5.0, 10.0, "t"), cost_bps=8.0)   # 80% of edge
        self.assertFalse(d.approved)

    def test_whitelist(self):
        self.assertFalse(self.eval(OrderIntent("BTC-CAD", "BUY", 5.0, 500.0, "t")).approved)

    def test_daily_drawdown_halts_and_blocks_entries(self):
        self.assertFalse(self.rm.check_daily_drawdown(98.0))
        self.assertTrue(self.rm.check_daily_drawdown(96.9))
        self.assertTrue(self.rm.halted_for_day)
        self.assertFalse(self.eval(OrderIntent("XIU.TO", "BUY", 5.0, 100.0, "t"), equity=96.9).approved)
        # exits still allowed while halted
        d = self.eval(OrderIntent("XIU.TO", "SELL", 0, 0, "t", exit_all=True),
                      positions={"XIU.TO": Position("XIU.TO", 0.1, 40)})
        self.assertTrue(d.approved)
        # new day resets
        self.rm.roll_day(time.time() + 86400, 96.9)
        self.assertFalse(self.rm.halted_for_day)

    def test_max_open_positions(self):
        pos = {s: Position(s, 0.01, 40) for s in ("XIC.TO", "ZSP.TO", "ZAG.TO")}
        self.assertFalse(self.eval(OrderIntent("XIU.TO", "BUY", 5.0, 100.0, "t"), positions=pos).approved)

    def test_cooldown(self):
        self.rm.note_fill("XIU.TO", "SELL", cycle=9)
        self.assertFalse(self.eval(OrderIntent("XIU.TO", "BUY", 5.0, 100.0, "t")).approved)


class TestMockBroker(unittest.TestCase):
    def test_round_trip_costs_and_accounting(self):
        feed = SyntheticFeed(seed=5, history_bars=10)
        b = MockBroker(feed, seed=1)
        q = b.get_quote("XIU.TO")
        f = b.submit_order(Order("XIU.TO", "BUY", 0.1), q)
        self.assertGreaterEqual(f.price, q.ask)          # paid the ask plus slippage
        self.assertEqual(f.commission, 0.0)              # wealthsimple: commission-free
        s = b.submit_order(Order("XIU.TO", "SELL", 0.1), q)
        self.assertLessEqual(s.price, q.bid)
        self.assertLess(s.realized_pnl, 0)               # spread + slippage is a real cost
        self.assertAlmostEqual(b.cash, 100 + s.realized_pnl, places=9)
        self.assertEqual(b.get_positions(), {})

    def test_rejects_leverage_and_shorts(self):
        b = MockBroker(SyntheticFeed(seed=5, history_bars=10), starting_cash=1.0)
        with self.assertRaises(BrokerError):
            b.submit_order(Order("XIU.TO", "BUY", 1.0))
        with self.assertRaises(BrokerError):
            b.submit_order(Order("XIU.TO", "SELL", 1.0))

    def test_questrade_fee_table_is_unviable_for_micro_lots(self):
        b = MockBroker(SyntheticFeed(seed=5, history_bars=10), fee_schedule=config.FEE_TABLES["questrade"])
        self.assertGreater(b.estimate_round_trip_cost_bps("XIU.TO", 5.0), config.RISK.max_round_trip_cost_bps)
        w = MockBroker(SyntheticFeed(seed=5, history_bars=10))
        self.assertLess(w.estimate_round_trip_cost_bps("XIU.TO", 5.0), config.RISK.max_round_trip_cost_bps)


class TestStrategy(unittest.TestCase):
    def test_param_overrides_clamped_and_risk_keys_ignored(self):
        s = TrendPullbackStrategy()
        applied = s.update_params({"atr_stop_mult": 99, "max_position_pct": 0.9, "slow_sma": 10, "bogus": 1})
        self.assertEqual(applied["atr_stop_mult"], 4.0)
        self.assertNotIn("max_position_pct", applied)
        self.assertEqual(s.p["slow_sma"], 30)
        self.assertLess(s.p["fast_sma"], s.p["slow_sma"])

    def test_indicators_basic(self):
        closes = [float(i) for i in range(1, 60)]
        self.assertAlmostEqual(ind.sma(closes, 5), 57.0)
        self.assertEqual(ind.rsi(closes, 14), 100.0)
        self.assertAlmostEqual(ind.momentum(closes, 10), 59 / 49 - 1)
        self.assertLess(ind.max_drawdown([100, 90, 95, 80, 120]), -0.19)


class FakeClient:
    """Mimics the parts of anthropic.Anthropic used by the bridge."""
    def __init__(self, text, in_tok=5000, out_tok=800):
        self.text, self.in_tok, self.out_tok = text, in_tok, out_tok
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason="end_turn", model=kwargs["model"],
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=self.in_tok, output_tokens=self.out_tok,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


class TestAgentTrigger(unittest.TestCase):
    def test_disabled_bridge_costs_nothing(self):
        l = Ledger(":memory:")
        r = AgentTrigger(l, enabled=False).maybe_invoke("weekly_review", {})
        self.assertFalse(r.invoked)
        self.assertEqual(l.credit_spent_cad(), 0.0)

    def test_invocation_records_cost_and_clamps_overrides(self):
        l = Ledger(":memory:")
        fake = FakeClient('Looks fine.\n{"param_overrides": {"atr_stop_mult": 3.0, "max_position_pct": 0.5}, '
                          '"halt_new_entries": false, "notes": "ok"}')
        a = AgentTrigger(l, enabled=True, api_key="test", client=fake)
        r = a.maybe_invoke("weekly_review", {"equity": 100})
        self.assertTrue(r.invoked)
        self.assertEqual(r.param_overrides, {"atr_stop_mult": 3.0})
        self.assertAlmostEqual(r.cost_cad, estimate_cost_cad("claude-fable-5-1", 5000, 800))
        self.assertAlmostEqual(l.credit_spent_cad(), r.cost_cad)
        self.assertEqual(fake.kwargs["model"], "claude-fable-5-1")
        self.assertEqual(fake.kwargs["output_config"], {"effort": "low"})
        self.assertIn("fallbacks", fake.kwargs)
        # second call within the interval is refused locally, no API hit
        r2 = a.maybe_invoke("weekly_review", {})
        self.assertFalse(r2.invoked)
        self.assertEqual(fake.calls, 1)

    def test_credit_floor_blocks_calls(self):
        l = Ledger(":memory:")
        l.record_tokens(purpose="weekly_review", model="claude-fable-5-1", input_tokens=0, output_tokens=0,
                        cost_usd=0, cost_cad=96.0)
        fake = FakeClient("x")
        r = AgentTrigger(l, enabled=True, api_key="k", client=fake).maybe_invoke("regime_shift", {})
        self.assertFalse(r.invoked)
        self.assertIn("floor", r.skipped_because)
        self.assertEqual(fake.calls, 0)

    def test_api_error_is_contained(self):
        l = Ledger(":memory:")
        class Boom:
            beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("503"))))
        r = AgentTrigger(l, enabled=True, api_key="k", client=Boom()).maybe_invoke("self_heal", {})
        self.assertFalse(r.invoked)
        self.assertEqual(l.credit_spent_cad(), 0.0)


class CrashFeed(SyntheticFeed):
    """Synthetic feed that gaps every symbol down 80% on the second live bar (stress test)."""
    def __init__(self, **kw):
        self.live = 0
        self.armed = False
        super().__init__(**kw)       # warm-up bars run with armed=False
        self.armed = True

    def next_bars(self):
        bars = super().next_bars()
        if not self.armed:
            return bars
        self.live += 1
        if self.live == 2:
            for s, b in bars.items():
                b.close = b.open = b.high = b.low = b.close * 0.20
                self._last_close[s] = b.close
        return bars


class BrokenFeed(SyntheticFeed):
    def __init__(self, **kw):
        self.armed = False
        super().__init__(**kw)
        self.armed = True

    def next_bars(self):
        if self.armed:
            raise RuntimeError("feed exploded")
        return super().next_bars()


def make_daemon(feed, ledger=None):
    ledger = ledger or Ledger(":memory:")
    broker = MockBroker(feed, starting_cash=100.0, seed=1)
    return Daemon(ledger, broker, feed, TrendPullbackStrategy(), RiskManager(), AgentTrigger(ledger, enabled=False), 0)


class TestDaemonSafety(unittest.TestCase):
    def test_drawdown_halt_flattens_and_blocks(self):
        feed = CrashFeed(seed=42, history_bars=200)
        d = make_daemon(feed)
        d.tick()                                   # cycle 1: enters (seed 42 produces an entry)
        self.assertTrue(d.broker.get_positions())
        d.tick()                                   # cycle 2: 80% gap on a ~4.6% position -> dd > 3% -> flatten
        self.assertTrue(d.risk.halted_for_day)
        self.assertEqual(d.broker.get_positions(), {})
        self.assertTrue(d.ledger.events("drawdown_halt"))
        self.assertGreaterEqual(d.broker.cash, 0)

    def test_consecutive_errors_enter_safe_mode_without_crashing(self):
        d = make_daemon(BrokenFeed(seed=1, history_bars=60))
        for _ in range(config.SELF_HEAL_CONSECUTIVE_ERRORS):
            d.safe_tick()
        self.assertTrue(d.safe_mode)
        self.assertEqual(d.ledger.credit_spent_cad(), 0.0)          # bridge disabled -> $0
        self.assertTrue(d.ledger.events("safe_mode"))

    def test_hundred_cycle_dry_run_invariants(self):
        for seed in (42, 7):
            report = run_dry(cycles=100, seed=seed)
            self.assertEqual(report["failures"], [], report)
            self.assertEqual(report["token_calls"], 0)
            self.assertLessEqual(report["max_buy_pct_of_equity"], config.RISK.max_position_pct * 100)


if __name__ == "__main__":
    unittest.main()
