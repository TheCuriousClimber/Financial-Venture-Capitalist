# Backtest audit v2 — slow-trend + cash gate (2026-09-07)

Same harness and cost model as `2026-09-07-verdict.md`: 365 daily cycles × 100 seeds × 4 regimes, 40 bps taker +
10 bps slippage per side, 10% cap, 3% kill-switch, Kraken minimums, 90/10 sweep, production daemon stack.

Changes under test (commit `707ee78`):
1. Slow-trend defaults: SMA 30/100, 60-day momentum, RSI entry ≤ 75, exit 90, 3× ATR, edge capture 0.35.
2. Cash gate on the equal-weight basket: below 100d SMA **and** negative 60d momentum → liquidate to 100% CAD;
   below SMA **or** momentum ≤ 0 **or** 20d vol z-score > 1.5 → no new entries. Per-asset: vol z ≤ 1.5,
   momentum ≥ 5%, Kaufman efficiency ratio ≥ 0.30.
3. Bridge: monthly review (30 d, ~$2.16/yr) + emergency-only triggers (circuit breaker, feed outage, self-heal).

`gate_off` (regime_gate=0) reproduces the earlier `slow_trend` variant to the cent, so the delta below is the gate alone.

## Comparison (per run-year on $100; owner/reserve/fees are per-run means)

| variant | regime | win% | PF | fills | net PnL mean | median | prof% | max DD mean | worst DD | kill-switch runs | owner | reserve | fees | exposure | min final value |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| old baseline | bull | 42.4 | 2.07 | 47.3 | +13.55 | +11.89 | 92% | -5.8% | -15.6% | 0% | 13.56 | 1.51 | 1.85 | 74% | 86.79 |
| old baseline | bear | 21.1 | 0.73 | 64.3 | -5.41 | -6.86 | 26% | -13.1% | -25.5% | 0% | 3.88 | 0.43 | 1.60 | 66% | 74.43 |
| old baseline | chop | 21.0 | 0.50 | 58.6 | -9.85 | -9.80 | 8% | -13.3% | -24.2% | 0% | 1.42 | 0.16 | 2.14 | 62% | 75.72 |
| old baseline | black swan | 21.2 | 0.83 | 63.1 | -3.08 | -3.62 | 35% | -12.3% | -27.0% | 30% | 4.76 | 0.53 | 1.42 | 61% | 77.54 |
| old baseline | **all** | 25.4 | 0.92 | 58.3 | **-1.20** | **-3.22** | 40% | -11.1% | -27.0% | 8% | 5.91 | 0.66 | 1.75 | 66% | 74.43 |
| slow-trend, gate off | bull | 41.1 | 3.39 | 28.6 | +23.67 | +21.15 | 95% | -7.4% | -16.5% | 17% | 18.95 | 2.11 | 1.17 | 86% | 86.96 |
| slow-trend, gate off | bear | 17.3 | 0.64 | 38.6 | -4.42 | -6.68 | 30% | -13.3% | -23.4% | 21% | 3.59 | 0.40 | 0.97 | 58% | 77.21 |
| slow-trend, gate off | chop | 23.0 | 0.48 | 46.9 | -7.09 | -8.38 | 17% | -12.2% | -24.1% | 0% | 1.43 | 0.16 | 1.73 | 75% | 79.14 |
| slow-trend, gate off | black swan | 18.0 | 0.76 | 36.7 | -2.73 | -4.70 | 33% | -12.9% | -23.4% | 52% | 4.22 | 0.47 | 0.88 | 55% | 78.94 |
| slow-trend, gate off | **all** | 23.7 | 1.06 | 37.7 | **+2.36** | **-2.32** | 44% | -11.5% | -24.1% | 22% | 7.05 | 0.78 | 1.19 | 69% | 77.21 |
| **slow-trend + gate** | bull | 47.6 | 3.70 | 19.0 | +16.59 | +14.73 | 92% | -6.4% | -10.5% | 11% | 14.25 | 1.58 | 0.80 | 63% | 92.05 |
| **slow-trend + gate** | bear | 31.7 | 0.98 | 6.3 | +0.10 | -0.84 | 31% | -4.0% | -11.8% | 6% | 1.68 | 0.19 | 0.18 | 10% | 89.58 |
| **slow-trend + gate** | chop | 21.3 | 0.31 | 18.1 | -4.84 | -4.35 | 13% | -7.1% | -15.0% | 0% | 0.68 | 0.08 | 0.68 | 26% | 85.45 |
| **slow-trend + gate** | black swan | 28.3 | 0.80 | 8.3 | -0.58 | -1.77 | 32% | -5.2% | -10.6% | 19% | 1.88 | 0.21 | 0.25 | 14% | 90.06 |
| **slow-trend + gate** | **all** | **33.3** | **1.46** | 12.9 | **+2.82** | **-0.85** | 42% | **-5.7%** | **-15.0%** | 9% | 4.62 | 0.51 | 0.48 | 28% | 85.45 |

## What changed

| metric (pooled unless noted) | old baseline | slow-trend + gate | change |
|---|---|---|---|
| net PnL mean / median | -1.20 / -3.22 | +2.82 / -0.85 | +4.02 / +2.37 |
| profit factor | 0.92 | 1.46 | +0.54 |
| win rate | 25.4% | 33.3% | +7.9 pts |
| chop fills per year (turnover) | 58.6 | 18.1 | **-69%** |
| chop max drawdown mean | -13.3% | -7.1% | -6.2 pts |
| chop net PnL | -9.85 | -4.84 | +5.01 |
| bear net PnL / exposure | -5.41 / 66% | +0.10 / 10% | break-even, sits in cash |
| black-swan net PnL / kill-switch runs | -3.08 / 30% | -0.58 / 19% | +2.50 |
| max drawdown mean / worst | -11.1% / -27.0% | -5.7% / -15.0% | roughly halved |
| fees per year | 1.75 | 0.48 | -73% |
| worst final value (any run) | 74.43 | 85.45 | +11.02 |
| survival (value > 0) | 100% | 100% | – |

## Sweeps vs. revised LLM cost (per run-year)

| | old baseline | slow-trend + gate |
|---|---|---|
| owner disbursement (90%) | $5.91 | $4.62 |
| compute reserve (10%) | $0.66 | $0.51 |
| scheduled LLM cost | ~$9.40 (weekly) | **~$2.16 (monthly)** |
| reserve covers scheduled cost? | no (7%) | no (24%) |
| $100 credit pool lifetime at scheduled cadence | ~10 yr | ~46 yr |

Sweeps are lower with the gate because it books fewer realized gains, but the pooled *net* result is higher:
the old system swept realized profit and then gave more back through unrealized losses.

## Verdict

- **Pooled edge after 100 bps round-trip costs is now positive in expectation**: +$2.82/yr, PF 1.46, drawdowns
  halved, worst-case year $85 instead of $74. The gate removed 69% of chop turnover and turned the bear regime
  into a cash-holding break-even.
- **Two target invariants are still missed.** Pooled median is -$0.85 (not > 0) and chop PF is 0.31 (not ≥ 1.0):
  the ±8% oscillation still passes the momentum floor and the 0.30 efficiency-ratio floor about a quarter of the time.
- **Compute is safe but not self-funded.** The monthly cadence cuts scheduled spend to ~$2.16/yr; the reserve
  earns ~$0.51/yr. At $100 the pool outlives the strategy horizon; it does not compound.

Next step, if you want to close the remaining gap: tighten the chop filters and re-run only chop and bull
(`--regimes chop,bull --variant "strict_chop=er_min:0.45,momentum_min:0.08"`); the risk is giving back bull
entries, which the bull row will show.

Raw data: `2026-09-07-gated.json` (baseline + gate_off, 800 runs), `2026-09-07-gated-backtest.log`.
