"""100-cycle dry run against the mock broker + synthetic feed.

Proves, with hard assertions:
  1. zero token burn - no bridge call, $0.00 credit spent
  2. every BUY fill's notional <= 5% of equity at order time
  3. daily drawdown gate armed (never breached silently; if breached, positions were flattened)
  4. no leverage / no shorts: cash never negative, no negative positions
  5. profit split reconciles: reserve == 10%, owner == 90%, both sum to swept; ledger == broker
Run: python -m trading_engine.dry_run [--cycles 100] [--seed 42]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import List

from . import config
from .bridge.agent_trigger import AgentTrigger
from .broker.mock_broker import MockBroker
from .core.risk_manager import RiskManager
from .core.strategy import TrendPullbackStrategy
from .daemon import Daemon
from .data.feed import SyntheticFeed
from .ledger import Ledger, cents


def run_dry(cycles: int = 100, seed: int = config.SYNTHETIC_SEED, ledger_path: str = ":memory:",
            verbose: bool = False) -> dict:
    if ledger_path != ":memory:" and os.path.exists(ledger_path):
        os.remove(ledger_path)
    ledger = Ledger(ledger_path)
    feed = SyntheticFeed(seed=seed, history_bars=200)
    broker = MockBroker(feed, starting_cash=ledger.principal, seed=seed)
    daemon = Daemon(ledger, broker, feed, TrendPullbackStrategy(), RiskManager(principal=ledger.principal),
                    AgentTrigger(ledger, enabled=False), poll_interval=0)
    daemon.run(max_cycles=cycles, sleep_seconds=0)

    quotes = broker.get_quotes(feed.symbols)
    positions = broker.get_positions()
    equity = broker.equity(quotes)
    failures: List[str] = []

    # 1. zero token burn
    tokens = ledger.total_tokens()
    if tokens["calls"] != 0 or ledger.credit_spent_cad() != 0.0:
        failures.append(f"token burn detected: {tokens} spent={ledger.credit_spent_cad()}")
    skipped = len(ledger.events("bridge_skipped", limit=1000))

    # 2. 5% max allocation gate on every BUY
    max_ratio = 0.0
    for t in ledger.trades(limit=100000):
        if t["side"] == "BUY":
            ratio = t["notional"] / t["equity_at_order"]
            max_ratio = max(max_ratio, ratio)
            if ratio > config.RISK.max_position_pct + 1e-9:
                failures.append(f"trade {t['id']} {t['symbol']} notional {t['notional']:.4f} = {ratio:.4%} of equity")

    # 3. drawdown gate: any snapshot beyond -3% must coincide with the halt having flattened
    worst_dd = 0.0
    for row in ledger.equity_curve():
        dd = row["drawdown_pct"] or 0.0
        worst_dd = min(worst_dd, dd)
        if dd < -config.RISK.max_daily_drawdown_pct - 0.005 and row["open_positions"] > 0:
            failures.append(f"cycle {row['cycle']} drawdown {dd:.2%} with {row['open_positions']} open positions")

    # 4. no leverage / no shorts
    if broker.cash < -1e-9:
        failures.append(f"negative cash {broker.cash}")
    if any(p.qty < 0 for p in positions.values()):
        failures.append("short position present")
    gross = sum(p.qty * quotes[s].mid for s, p in positions.items())
    if gross > equity * config.RISK.max_gross_exposure_pct + 0.05:
        failures.append(f"gross exposure {gross:.2f} exceeds cap")

    # 5. profit split reconciliation
    swept, owner, reserve = ledger.swept_total(), ledger.owner_total(), ledger.reserve_total()
    if abs(swept - (owner + reserve)) > 0.005:
        failures.append(f"split legs {owner}+{reserve} != swept {swept}")
    for row in ledger.conn.execute("SELECT * FROM profit_splits").fetchall():
        exp_reserve = cents(row["distributable"] * config.OPERATIONAL_SURPLUS_PCT)
        if abs(row["operational_reserve"] - exp_reserve) > 1e-9 or \
                abs(row["owner_disbursement"] - (row["distributable"] - exp_reserve)) > 1e-9:
            failures.append(f"split row {row['id']} not 90/10 at cent precision: {dict(row)}")
        if row["distributable"] < config.MIN_SWEEP_CAD - 1e-9:
            failures.append(f"split row {row['id']} below minimum sweep")
        if row["equity_after"] < ledger.principal - 0.005:
            failures.append(f"split row {row['id']} left trading equity {row['equity_after']:.2f} below principal")
    if abs(broker.withdrawn_total - swept) > 0.005:
        failures.append(f"broker withdrawn {broker.withdrawn_total} != ledger swept {swept}")
    realized = ledger.realized_pnl_total()
    unrealized = sum(p.unrealized_pnl(quotes[s].mid) for s, p in positions.items())
    identity = ledger.principal + realized + unrealized - swept
    if abs(identity - equity) > 0.01:
        failures.append(f"accounting identity broken: principal+realized+unrealized-swept={identity:.4f} vs equity {equity:.4f}")
    if ledger.credit_remaining_cad() != ledger.credit_budget_cad() + reserve:
        failures.append("credit pool did not absorb operational reserve")

    report = {
        "cycles": cycles, "seed": seed, "trades": ledger.trade_count(), "fills": len(daemon.fills),
        "rejections": len(daemon.rejections), "open_positions": len(positions),
        "equity_cad": cents(equity), "swept_cad": cents(swept), "owner_cad": cents(owner), "reserve_cad": cents(reserve),
        "total_value_cad": cents(equity + swept), "realized_net_cad": cents(realized), "unrealized_cad": cents(unrealized),
        "max_buy_pct_of_equity": round(max_ratio * 100, 3), "worst_daily_drawdown_pct": round(worst_dd * 100, 3),
        "token_calls": tokens["calls"], "credit_spent_cad": ledger.credit_spent_cad(),
        "credit_remaining_cad": round(ledger.credit_remaining_cad(), 4), "bridge_skips_logged": skipped,
        "safe_mode": daemon.safe_mode, "failures": failures,
    }
    if verbose:
        for t in reversed(ledger.trades(limit=100000)):
            print(f"  c{t['cycle']:>3} {t['side']:<4} {t['symbol']:<9} qty={t['qty']:.4f} @ {t['price']:.3f} "
                  f"notional={t['notional']:.2f} pnl={t['realized_pnl']:+.4f} {t['reason']}")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--seed", type=int, default=config.SYNTHETIC_SEED)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    report = run_dry(args.cycles, args.seed, verbose=args.verbose)
    print(json.dumps(report, indent=2))
    ok = not report["failures"]
    print("DRY RUN " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
