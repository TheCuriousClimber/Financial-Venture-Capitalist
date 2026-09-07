"""Multi-regime Monte Carlo backtester. stdlib only.

Drives the REAL production stack (Daemon -> TrendPullbackStrategy -> RiskManager -> MockBroker -> Ledger)
over synthetic daily bars, so every gate that would fire live fires here: 10% position cap, 3% daily
drawdown kill-switch, fee-vs-edge gate, venue minimums, 90/10 profit sweep.

Cost model (exact, per side): 40 bps taker fee + 10 bps slippage, zero spread  => 100 bps round trip.

Regimes (365 live daily cycles after a 200-bar neutral warm-up):
  bull        +100% annualised drift, moderate vol
  bear        -50% annualised drift, high vol
  chop        0% drift, sinusoidal oscillation (+/-8%, 18-35 day period) that traps RSI/trend filters
  black_swan  mild drift, then a -25% single-day gap on a random day (60-200) followed by 2.5x vol

Run:  python3 -m trading_engine.core.backtester [--seeds 100] [--cycles 365] [--regimes bull,bear,chop,black_swan]
      [--variant name=key:val,key:val ...] [--json out.json] [--workers N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from multiprocessing import Pool
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config
from ..bridge.agent_trigger import AgentTrigger
from ..broker.mock_broker import MockBroker
from ..core.risk_manager import RiskManager
from ..core.strategy import TrendPullbackStrategy
from ..daemon import Daemon
from ..data.feed import TRADING_DAYS, Bar, SyntheticFeed
from ..ledger import Ledger

# Exact cost model requested for the stress test: 40 bps taker + 10 bps slippage per side, no spread.
BACKTEST_FEES = config.FeeSchedule("backtest_kraken_taker", 0.0, 0.0, 40.0, 0.0, 0.0, 10.0, 0.0, 0.0, True, 1.00)

CRYPTO_SYMBOLS = [a.symbol for a in config.CRYPTO_WATCHLIST]
# Crypto-tuned strategy params (identical to config.STRATEGY_PARAMS when ASSET_UNIVERSE=crypto)
CRYPTO_PARAMS: Dict[str, float] = {**config.STRATEGY_PARAMS, "vol_target_annual": 0.80, "regime_vol_z": 3.0}


@dataclass(frozen=True)
class Regime:
    name: str
    drift: float                        # annualised log drift during the live window
    vol_mult: float                     # multiplier on each asset's annual_vol
    osc_amp: float = 0.0                # sinusoidal log-price amplitude (chop)
    osc_period: Tuple[int, int] = (18, 35)
    shock_day: Tuple[int, int] = (0, 0) # (lo, hi) live-day window for a one-day gap; (0,0) = none
    shock_pct: float = 0.0
    post_shock_vol_mult: float = 1.0
    post_shock_drift: float = 0.0


REGIMES: Dict[str, Regime] = {
    "bull": Regime("bull", drift=1.00, vol_mult=0.8),
    "bear": Regime("bear", drift=-0.50, vol_mult=1.5),
    "chop": Regime("chop", drift=0.0, vol_mult=0.6, osc_amp=0.08),
    "black_swan": Regime("black_swan", drift=0.20, vol_mult=1.0, shock_day=(60, 200), shock_pct=-0.25,
                         post_shock_vol_mult=2.5, post_shock_drift=0.0),
}


class RegimeFeed(SyntheticFeed):
    """Synthetic daily bars: neutral GBM during warm-up, then the regime's dynamics. Zero spread quotes."""

    def __init__(self, regime: Regime, seed: int, symbols: Sequence[str] = CRYPTO_SYMBOLS, history_bars: int = 200):
        self.regime = regime
        self.live = False
        self.live_day = 0
        self._phase: Dict[str, float] = {}
        self._period: Dict[str, int] = {}
        self._shock_day: int = 0
        self._shocked = False
        self._pre_regime_rng = random.Random(seed * 7919 + 13)
        super().__init__(symbols=list(symbols), seed=seed, history_bars=history_bars, regime_prob=0.0)
        for s in self.symbols:
            self._phase[s] = self._pre_regime_rng.uniform(0, 2 * math.pi)
            self._period[s] = self._pre_regime_rng.randint(*regime.osc_period)
        if regime.shock_day != (0, 0):
            self._shock_day = self._pre_regime_rng.randint(*regime.shock_day)
        self.live = True

    def _step(self, symbol: str) -> Bar:
        a = config.ASSETS[symbol]
        dt = 1.0 / TRADING_DAYS
        prev = self._last_close[symbol]
        if not self.live:
            drift, vol = 0.10, a.annual_vol
        else:
            r = self.regime
            after_shock = self._shock_day and self.live_day > self._shock_day
            drift = r.post_shock_drift if after_shock else r.drift
            vol = a.annual_vol * (r.post_shock_vol_mult if after_shock else r.vol_mult)
        z = self.rng.gauss(0.0, 1.0)
        log_ret = (drift - 0.5 * vol * vol) * dt + vol * math.sqrt(dt) * z
        if self.live and self.regime.osc_amp:
            t, p, ph = self.live_day, self._period[symbol], self._phase[symbol]
            log_ret += self.regime.osc_amp * (math.sin(2 * math.pi * (t + 1) / p + ph) - math.sin(2 * math.pi * t / p + ph))
        close = prev * math.exp(log_ret)
        o = prev
        if self.live and self._shock_day and self.live_day == self._shock_day:
            close = prev * (1 + self.regime.shock_pct)          # single-day gap: opens at prev, closes -25%
            hi, lo = prev, close
        else:
            rng_frac = abs(self.rng.gauss(0.0, 1.0)) * vol * math.sqrt(dt)
            hi = max(o, close) * (1 + rng_frac / 2)
            lo = min(o, close) * (1 - rng_frac / 2)
        self._last_close[symbol] = close
        bar = Bar(symbol, self.ts, o, hi, lo, close, 1e6)
        self._hist[symbol].append(bar)
        if len(self._hist[symbol]) > config.MAX_HISTORY_BARS:
            del self._hist[symbol][: len(self._hist[symbol]) - config.MAX_HISTORY_BARS]
        return bar

    def next_bars(self) -> Dict[str, Bar]:
        bars = super().next_bars()
        if self.live:
            self.live_day += 1
        return bars

    def quote(self, symbol: str):
        from ..broker.base import Quote
        last = self._last_close[symbol]
        return Quote(symbol, bid=last, ask=last, last=last, ts=self.ts)   # zero spread: cost is fee + slippage only


@dataclass
class RunResult:
    regime: str
    seed: int
    cycles: int
    trades: int = 0                 # fills (buys + sells)
    round_trips: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0          # sum of positive net round-trip PnL
    gross_loss: float = 0.0         # abs sum of negative net round-trip PnL
    fee_gate_rejections: int = 0
    other_rejections: int = 0
    fees_paid: float = 0.0
    realized_net: float = 0.0
    unrealized: float = 0.0
    net_pnl: float = 0.0            # realized_net + unrealized (== total_value - principal)
    final_equity: float = 0.0       # trading equity at broker after sweeps
    swept_total: float = 0.0
    owner: float = 0.0
    reserve: float = 0.0
    total_value: float = 0.0        # final_equity + swept
    max_drawdown: float = 0.0       # on total value curve, fraction (negative)
    worst_daily_dd: float = 0.0
    dd_halts: int = 0
    token_calls: int = 0
    survived: bool = True
    exposure_days: int = 0
    rejection_reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.round_trips if self.round_trips else 0.0


def run_one(args: Tuple[str, int, int, Dict[str, float]]) -> RunResult:
    regime_name, seed, cycles, params = args
    regime = REGIMES[regime_name]
    ledger = Ledger(":memory:")
    feed = RegimeFeed(regime, seed)
    broker = MockBroker(feed, starting_cash=ledger.principal, fee_schedule=BACKTEST_FEES, seed=seed)
    strategy = TrendPullbackStrategy()
    strategy.p.update(params)                       # research tool: bypass universe-dependent clamps
    daemon = Daemon(ledger, broker, feed, strategy, RiskManager(principal=ledger.principal),
                    AgentTrigger(ledger, enabled=False), poll_interval=0)
    daemon.run(max_cycles=cycles, sleep_seconds=0)

    res = RunResult(regime_name, seed, cycles)
    quotes = broker.get_quotes(feed.symbols)
    positions = broker.get_positions()
    res.final_equity = broker.equity(quotes)
    res.swept_total, res.owner, res.reserve = ledger.swept_total(), ledger.owner_total(), ledger.reserve_total()
    res.total_value = res.final_equity + res.swept_total
    res.realized_net = ledger.realized_pnl_total()
    res.unrealized = sum(p.unrealized_pnl(quotes[s].mid) for s, p in positions.items())
    res.net_pnl = res.total_value - ledger.principal
    res.token_calls = ledger.total_tokens()["calls"]
    res.survived = res.total_value > 0.0

    # round trips: pair each SELL with the BUY fees of the same symbol since the last flat
    open_fees: Dict[str, float] = {}
    for t in reversed(ledger.trades(limit=100000)):          # chronological
        res.trades += 1
        res.fees_paid += t["commission"] + t["fees"]
        if t["side"] == "BUY":
            open_fees[t["symbol"]] = open_fees.get(t["symbol"], 0.0) + t["commission"] + t["fees"]
        else:
            net = t["realized_pnl"] - t["commission"] - t["fees"] - open_fees.pop(t["symbol"], 0.0)
            res.round_trips += 1
            if net > 0:
                res.wins += 1
                res.gross_win += net
            else:
                res.losses += 1
                res.gross_loss += -net
    for r in daemon.rejections:
        reason = r.split(": ", 1)[1] if ": " in r else r
        key = ("fee gate" if ("fees" in reason or "cost" in reason) else
               "venue minimum" if "venue minimum" in reason else
               "re-entry cooldown" if "cooldown" in reason else
               "max open positions" if "max open positions" in reason else
               "at position cap" if "cap" in reason else
               "notional below minimum" if "below minimum" in reason else
               "drawdown halt" if "drawdown" in reason else
               "max trades/day" if "trades per day" in reason else reason[:40])
        res.rejection_reasons[key] = res.rejection_reasons.get(key, 0) + 1
        if key == "fee gate":
            res.fee_gate_rejections += 1
        else:
            res.other_rejections += 1
    res.dd_halts = len(ledger.events("drawdown_halt", limit=10000))
    curve = [row["equity"] + row["swept_total"] for row in ledger.equity_curve(limit=100000)]
    peak, mdd = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    res.max_drawdown = mdd
    res.worst_daily_dd = min((row["drawdown_pct"] or 0.0) for row in ledger.equity_curve(limit=100000)) if curve else 0.0
    res.exposure_days = sum(1 for row in ledger.equity_curve(limit=100000) if row["open_positions"] > 0)
    ledger.close()
    return res


@dataclass
class RegimeSummary:
    regime: str
    runs: int
    win_rate: float
    profit_factor: float
    trades_mean: float
    round_trips_mean: float
    fee_rejections_mean: float
    other_rejections_mean: float
    net_pnl_mean: float
    net_pnl_median: float
    net_pnl_p10: float
    net_pnl_p90: float
    final_equity_mean: float
    total_value_mean: float
    profitable_pct: float
    max_dd_mean: float
    max_dd_worst: float
    dd_halt_runs_pct: float
    owner_total: float
    reserve_total: float
    fees_total: float
    survival_pct: float
    exposure_pct: float
    top_rejections: Dict[str, float] = field(default_factory=dict)   # reason -> mean per run


def _pct(xs: List[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(results: List[RunResult], label: str) -> RegimeSummary:
    n = len(results)
    wins = sum(r.wins for r in results)
    rts = sum(r.round_trips for r in results)
    gw = sum(r.gross_win for r in results)
    gl = sum(r.gross_loss for r in results)
    pnls = [r.net_pnl for r in results]
    return RegimeSummary(
        regime=label, runs=n,
        win_rate=wins / rts if rts else 0.0,
        profit_factor=(gw / gl) if gl > 0 else (math.inf if gw > 0 else 0.0),
        trades_mean=statistics.fmean(r.trades for r in results),
        round_trips_mean=rts / n,
        fee_rejections_mean=statistics.fmean(r.fee_gate_rejections for r in results),
        other_rejections_mean=statistics.fmean(r.other_rejections for r in results),
        net_pnl_mean=statistics.fmean(pnls), net_pnl_median=statistics.median(pnls),
        net_pnl_p10=_pct(pnls, 0.10), net_pnl_p90=_pct(pnls, 0.90),
        final_equity_mean=statistics.fmean(r.final_equity for r in results),
        total_value_mean=statistics.fmean(r.total_value for r in results),
        profitable_pct=100.0 * sum(1 for p in pnls if p > 0) / n,
        max_dd_mean=statistics.fmean(r.max_drawdown for r in results),
        max_dd_worst=min(r.max_drawdown for r in results),
        dd_halt_runs_pct=100.0 * sum(1 for r in results if r.dd_halts) / n,
        owner_total=sum(r.owner for r in results), reserve_total=sum(r.reserve for r in results),
        fees_total=sum(r.fees_paid for r in results),
        survival_pct=100.0 * sum(1 for r in results if r.survived) / n,
        exposure_pct=100.0 * statistics.fmean(r.exposure_days / r.cycles for r in results),
        top_rejections=_top_rejections(results),
    )


def _top_rejections(results: List[RunResult], k: int = 5) -> Dict[str, float]:
    agg: Dict[str, int] = {}
    for r in results:
        for reason, n in r.rejection_reasons.items():
            agg[reason] = agg.get(reason, 0) + n
    n = max(1, len(results))
    return {reason: round(v / n, 1) for reason, v in sorted(agg.items(), key=lambda kv: -kv[1])[:k]}


def render(summaries: List[RegimeSummary], title: str) -> str:
    cols = [("regime", 11, "{}"), ("runs", 5, "{}"), ("win%", 6, "{:.1f}"), ("PF", 6, "{:.2f}"), ("trades", 7, "{:.1f}"),
            ("rt", 6, "{:.1f}"), ("feeRej", 7, "{:.1f}"), ("othRej", 7, "{:.1f}"), ("netPnL", 8, "{:+.2f}"), ("median", 8, "{:+.2f}"),
            ("p10", 8, "{:+.2f}"), ("p90", 8, "{:+.2f}"), ("equity", 8, "{:.2f}"), ("value", 8, "{:.2f}"), ("prof%", 6, "{:.0f}"),
            ("maxDD", 7, "{:.1%}"), ("worst", 7, "{:.1%}"), ("halt%", 6, "{:.0f}"), ("owner", 8, "{:.2f}"), ("reserve", 8, "{:.2f}"),
            ("fees", 8, "{:.2f}"), ("surv%", 6, "{:.0f}"), ("expo%", 6, "{:.0f}")]
    keymap = {"regime": "regime", "runs": "runs", "win%": "win_rate", "PF": "profit_factor", "trades": "trades_mean",
              "rt": "round_trips_mean", "feeRej": "fee_rejections_mean", "othRej": "other_rejections_mean",
              "netPnL": "net_pnl_mean", "median": "net_pnl_median", "p10": "net_pnl_p10", "p90": "net_pnl_p90",
              "equity": "final_equity_mean", "value": "total_value_mean", "prof%": "profitable_pct", "maxDD": "max_dd_mean",
              "worst": "max_dd_worst", "halt%": "dd_halt_runs_pct", "owner": "owner_total", "reserve": "reserve_total",
              "fees": "fees_total", "surv%": "survival_pct", "expo%": "exposure_pct"}
    lines = [f"=== {title} ===", " ".join(f"{name:>{w}}" for name, w, _ in cols)]
    for s in summaries:
        row = []
        for name, w, fmt in cols:
            v = getattr(s, keymap[name])
            if name == "win%":
                v *= 100
            if name == "PF" and v == math.inf:
                cell = "inf"
            else:
                cell = fmt.format(v)
            row.append(f"{cell:>{w}}")
        lines.append(" ".join(row))
    for s in summaries:
        lines.append(f"  rejections/run [{s.regime}]: " + ", ".join(f"{k}={v}" for k, v in s.top_rejections.items()))
    lines.append("cols: win% of round trips | PF = gross wins / gross losses (net of fees) | trades = fills/run | rt = round trips/run | "
                 "feeRej/othRej = rejected entries per run | netPnL/median/p10/p90 = CAD per run (value - 100) | equity = trading equity "
                 "after sweeps | value = equity + swept | prof% = runs with netPnL > 0 | maxDD on value curve | halt% = runs that hit the 3% "
                 "kill-switch | owner/reserve/fees = CAD summed over all runs | surv% = value > 0 | expo% = days with a position")
    return "\n".join(lines)


def parse_variant(spec: str) -> Tuple[str, Dict[str, float]]:
    name, _, kv = spec.partition("=")
    params: Dict[str, float] = {}
    for item in kv.split(",") if kv else []:
        k, _, v = item.partition(":")
        params[k.strip()] = float(v)
    return name.strip(), params


def run_grid(regimes: List[str], seeds: int, cycles: int, params: Dict[str, float], workers: int) -> List[RunResult]:
    jobs = [(rg, seed, cycles, params) for rg in regimes for seed in range(1, seeds + 1)]
    if workers <= 1:
        return [run_one(j) for j in jobs]
    with Pool(processes=workers) as pool:
        return pool.map(run_one, jobs, chunksize=4)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--cycles", type=int, default=365)
    ap.add_argument("--regimes", default=",".join(REGIMES))
    ap.add_argument("--variant", action="append", default=[], help="name=key:val,key:val (strategy param overrides)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    regimes = [r.strip() for r in args.regimes.split(",") if r.strip()]
    variants = [("baseline", {})] + [parse_variant(v) for v in args.variant]
    out: Dict[str, object] = {"seeds": args.seeds, "cycles": args.cycles, "cost_model": asdict(BACKTEST_FEES), "variants": {}}
    for vname, overrides in variants:
        params = {**CRYPTO_PARAMS, **overrides}
        t0 = time.time()
        results = run_grid(regimes, args.seeds, args.cycles, params, args.workers)
        summaries = [summarize([r for r in results if r.regime == rg], rg) for rg in regimes]
        summaries.append(summarize(results, "ALL"))
        print(render(summaries, f"variant={vname} params={ {k: params[k] for k in sorted(overrides) } or 'crypto defaults'} "
                                f"seeds={args.seeds} cycles={args.cycles} fees=40bps+10bps/side elapsed={time.time() - t0:.0f}s"))
        print()
        out["variants"][vname] = {"params": params, "summaries": [asdict(s) for s in summaries],
                                  "runs": [asdict(r) for r in results]}
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
