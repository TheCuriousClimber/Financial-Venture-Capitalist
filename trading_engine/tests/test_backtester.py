"""Backtester: regime dynamics behave as specified and the harness reuses the production stack."""
from __future__ import annotations

import _env  # noqa: F401
import math
import statistics
import unittest

from trading_engine import config
from trading_engine.core.backtester import (BACKTEST_FEES, CRYPTO_PARAMS, REGIMES, RegimeFeed, run_one,
                                            summarize)


def year_log_return(regime: str, seed: int, symbol: str = "SOL/CAD", days: int = 365) -> float:
    feed = RegimeFeed(REGIMES[regime], seed, symbols=[symbol], history_bars=60)
    start = feed._last_close[symbol]
    for _ in range(days):
        feed.next_bars()
    return math.log(feed._last_close[symbol] / start)


class TestRegimes(unittest.TestCase):
    def test_bull_and_bear_drift_signs(self):
        bull = statistics.fmean(year_log_return("bull", s) for s in range(1, 13))
        bear = statistics.fmean(year_log_return("bear", s) for s in range(1, 13))
        self.assertGreater(bull, 0.3)          # +100% drift, log ~ 0.7 - 0.5 vol^2
        self.assertLess(bear, -0.3)

    def test_chop_is_range_bound(self):
        rets = [year_log_return("chop", s) for s in range(1, 13)]
        self.assertLess(abs(statistics.fmean(rets)), 0.35)
        feed = RegimeFeed(REGIMES["chop"], 3, symbols=["SOL/CAD"], history_bars=10)
        closes = [feed.next_bars()["SOL/CAD"].close for _ in range(120)]
        # oscillation: price crosses its own 120-day mean many times
        m = statistics.fmean(closes)
        crossings = sum(1 for a, b in zip(closes, closes[1:]) if (a - m) * (b - m) < 0)
        self.assertGreaterEqual(crossings, 4)

    def test_black_swan_has_exact_single_day_gap_then_higher_vol(self):
        feed = RegimeFeed(REGIMES["black_swan"], 5, symbols=["BTC/CAD"], history_bars=10)
        bars = [feed.next_bars()["BTC/CAD"] for _ in range(365)]
        rets = [b.close / a.close - 1 for a, b in zip(bars, bars[1:])]
        worst = min(rets)
        self.assertAlmostEqual(worst, -0.25, places=6)
        k = rets.index(worst) + 1
        self.assertTrue(60 <= k <= 200)
        self.assertEqual(bars[k].open, bars[k - 1].close)      # gap: opens at prior close, closes -25%
        pre = statistics.pstdev(rets[:k - 1])
        post = statistics.pstdev(rets[k + 1:])
        self.assertGreater(post, pre * 1.5)

    def test_zero_spread_quote_and_exact_cost_model(self):
        feed = RegimeFeed(REGIMES["bull"], 1, symbols=["SOL/CAD"], history_bars=5)
        q = feed.quote("SOL/CAD")
        self.assertEqual(q.bid, q.ask)
        self.assertEqual(BACKTEST_FEES.commission_bps, 40.0)
        self.assertEqual(BACKTEST_FEES.slippage_bps_mean, 10.0)
        self.assertEqual(BACKTEST_FEES.slippage_bps_std, 0.0)


class TestHarness(unittest.TestCase):
    def test_run_one_uses_production_gates(self):
        r = run_one(("bull", 42, 120, CRYPTO_PARAMS))
        self.assertEqual(r.token_calls, 0)
        self.assertTrue(r.survived)
        self.assertAlmostEqual(r.total_value, r.final_equity + r.swept_total, places=9)
        self.assertAlmostEqual(r.net_pnl, r.total_value - 100.0, places=9)
        self.assertEqual(r.wins + r.losses, r.round_trips)
        self.assertGreaterEqual(r.max_drawdown, -1.0)
        self.assertLessEqual(r.max_drawdown, 0.0)
        self.assertEqual(r.fee_gate_rejections + r.other_rejections, sum(r.rejection_reasons.values()))
        if r.owner or r.reserve:
            self.assertAlmostEqual(r.reserve / (r.owner + r.reserve), config.OPERATIONAL_SURPLUS_PCT, delta=0.02)

    def test_black_swan_triggers_kill_switch_when_exposed(self):
        # gate off so the strategy is exposed; at least one seed with a position on the shock day must halt
        params = {**CRYPTO_PARAMS, "regime_gate": 0}
        halts = sum(run_one(("black_swan", s, 240, params)).dd_halts for s in range(1, 9))
        self.assertGreaterEqual(halts, 1)


class TestCashGate(unittest.TestCase):
    def history(self, regime: str, seed: int = 3, days: int = 200):
        feed = RegimeFeed(REGIMES[regime], seed, history_bars=150)
        for _ in range(days):
            feed.next_bars()
        return {s: feed.history(s) for s in feed.symbols}

    def test_bear_basket_forces_cash(self):
        from trading_engine.core.strategy import TrendPullbackStrategy
        from trading_engine.broker.base import Position
        strat = TrendPullbackStrategy(); strat.p.update(CRYPTO_PARAMS)
        hist = self.history("bear")
        gate = strat.evaluate_gate(hist)
        self.assertFalse(gate.entries_allowed)
        self.assertTrue(gate.force_exit, gate.reasons)
        held = {"SOL/CAD": Position("SOL/CAD", 0.05, 100.0)}
        sigs = strat.generate(hist, held, 100.0, 10.0)
        self.assertEqual([x.action for x in sigs], ["EXIT"])
        self.assertIn("macro bear", sigs[0].reason)

    def test_bull_basket_opens_gate(self):
        from trading_engine.core.strategy import TrendPullbackStrategy
        strat = TrendPullbackStrategy(); strat.p.update(CRYPTO_PARAMS)
        gate = strat.evaluate_gate(self.history("bull"))
        self.assertTrue(gate.entries_allowed, gate.reasons)
        self.assertFalse(gate.force_exit)
        self.assertGreater(gate.basket_mom, 0)

    def test_gate_off_never_blocks(self):
        from trading_engine.core.strategy import TrendPullbackStrategy
        strat = TrendPullbackStrategy(); strat.p.update({**CRYPTO_PARAMS, "regime_gate": 0})
        gate = strat.evaluate_gate(self.history("bear"))
        self.assertTrue(gate.entries_allowed)
        self.assertFalse(gate.force_exit)

    def test_chop_filters_entries(self):
        from trading_engine.core.strategy import TrendPullbackStrategy
        strat = TrendPullbackStrategy(); strat.p.update(CRYPTO_PARAMS)
        blocked = 0
        for seed in range(1, 6):
            hist = self.history("chop", seed=seed)
            strat.generate(hist, {}, 100.0, 10.0)
            blocked += len(strat.asset_filters) + (0 if strat.gate.entries_allowed else 1)
        self.assertGreater(blocked, 0)

    def test_summarize_pools_profit_factor(self):
        rs = [run_one(("chop", s, 60, CRYPTO_PARAMS)) for s in (1, 2)]
        s = summarize(rs, "chop")
        self.assertEqual(s.runs, 2)
        self.assertEqual(s.survival_pct, 100.0)
        gw, gl = sum(r.gross_win for r in rs), sum(r.gross_loss for r in rs)
        if gl:
            self.assertAlmostEqual(s.profit_factor, gw / gl)


if __name__ == "__main__":
    unittest.main()
