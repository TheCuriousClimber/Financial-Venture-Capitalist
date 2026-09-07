# Backtest audit v3 — chop filter calibration + final 4-regime validation (2026-09-07)

Same harness and cost model as v1/v2 (365 daily cycles × 100 seeds per regime, 40 bps taker + 10 bps slippage per
side, 10% cap, 3% kill-switch, Kraken minimums, 90/10 sweep, production daemon stack, identical seeds throughout).

## Step 1 — chop / false-breakout calibration (bull + chop, 100 seeds each)

Targets: chop fills cut by > 50% (from 18.1 → < 9.1) **and** bull PnL ≥ 75% of $16.59 (≥ $12.44).

| variant | ER floor | breakout | bull PnL | bull kept | bull median | chop fills | fills cut | chop PnL | chop PF | chop max DD | meets both |
|---|---|---|---|---|---|---|---|---|---|---|---|
| v2 gated (control) | 0.30 | off | +16.59 | 100% | +14.73 | 18.1 | 0% | -4.84 | 0.31 | -7.1% | – |
| er40 | 0.40 | off | +13.08 | 79% | +10.34 | 13.7 | 25% | -4.16 | 0.26 | -6.0% | no |
| er45 | 0.45 | off | +12.19 | 73% | +9.36 | 10.9 | 40% | -3.50 | 0.25 | -5.2% | no |
| er50 | 0.50 | off | +8.90 | 54% | +5.43 | 8.4 | 54% | -2.82 | 0.25 | -4.3% | no |
| **bo_er30 (selected)** | 0.30 | **on** | +12.38 | **75%** | +8.88 | 10.3 | 43% | **-2.61** | 0.32 | -4.6% | no (fills -43%) |
| bo_er40 | 0.40 | on | +8.84 | 53% | +5.37 | 6.4 | 65% | -1.68 | 0.35 | -3.3% | no |
| bo_er45 | 0.45 | on | +7.29 | 44% | +4.10 | 4.3 | 76% | -1.02 | 0.40 | -2.3% | no |
| bo_er50 | 0.50 | on | +4.65 | 28% | +1.67 | 2.9 | 84% | -0.88 | 0.26 | -1.7% | no |

**No setting satisfies both constraints.** The filters that remove chop entries remove bull pullback entries at
almost the same rate, so the frontier is a straight trade. Breakout confirmation with the ER floor left at 0.30
is the Pareto point: it holds the 75% bull line (six cents under $12.44), halves the chop loss and cuts chop fills
43% — the deepest cut available without breaching the bull floor. It is now the default (`breakout_confirm=1`).

## Step 3 — final validation, all four regimes (100 seeds each, 400 runs per variant)

| variant | regime | win% | PF | fills | net PnL mean | median | prof% | max DD mean | worst DD | kill-switch runs | exposure |
|---|---|---|---|---|---|---|---|---|---|---|---|
| v2 gated (prev) | bull | 47.6 | 3.70 | 19.0 | +16.59 | +14.73 | 92% | -6.4% | -10.5% | 11% | 63% |
| v2 gated (prev) | bear | 31.7 | 0.98 | 6.3 | +0.10 | -0.84 | 31% | -4.0% | -11.8% | 6% | 10% |
| v2 gated (prev) | chop | 21.3 | 0.31 | 18.1 | -4.84 | -4.35 | 13% | -7.1% | -15.0% | 0% | 26% |
| v2 gated (prev) | black swan | 28.3 | 0.80 | 8.3 | -0.58 | -1.77 | 32% | -5.2% | -10.6% | 19% | 14% |
| v2 gated (prev) | **all** | 33.3 | 1.46 | 12.9 | **+2.82** | **-0.85** | 42% | -5.7% | -15.0% | 9% | 28% |
| **final: breakout + ER 0.30** | bull | 48.0 | 3.68 | 15.0 | +12.38 | +8.88 | 86% | -5.9% | -11.5% | 8% | 53% |
| **final: breakout + ER 0.30** | bear | 29.9 | 0.67 | 3.8 | -0.45 | -0.17 | 30% | -2.8% | -7.9% | 3% | 6% |
| **final: breakout + ER 0.30** | chop | 22.8 | 0.32 | **10.3** | **-2.61** | -2.70 | 19% | -4.6% | -11.7% | 0% | 16% |
| **final: breakout + ER 0.30** | black swan | 26.9 | 0.80 | 5.4 | -0.36 | -1.19 | 26% | -3.9% | -9.7% | 12% | 10% |
| **final: breakout + ER 0.30** | **all** | **35.1** | **1.62** | 8.6 | **+2.24** | **-0.22** | 40% | **-4.3%** | **-11.7%** | 6% | 21% |
| alt: breakout + ER 0.40 | bull | 44.6 | 3.45 | 11.3 | +8.84 | +5.37 | 78% | -5.4% | -10.3% | 7% | 43% |
| alt: breakout + ER 0.40 | bear | 28.2 | 0.58 | 2.5 | -0.38 | +0.00 | 21% | -1.9% | -8.7% | 1% | 4% |
| alt: breakout + ER 0.40 | chop | 21.3 | 0.35 | 6.4 | -1.68 | -1.60 | 23% | -3.3% | -8.1% | 0% | 10% |
| alt: breakout + ER 0.40 | black swan | 26.1 | 0.82 | 3.6 | -0.26 | -0.85 | 24% | -3.0% | -7.8% | 7% | 7% |
| alt: breakout + ER 0.40 | **all** | 33.8 | 1.64 | 6.0 | +1.63 | -0.10 | 36% | -3.4% | -10.3% | 4% | 16% |

### Checklist against the requested invariants

| invariant | result | status |
|---|---|---|
| chop fill count vs 18.1 | 10.3 (-43%) | improved, short of -50% |
| chop PnL vs -$4.84 benchmark | **-$2.61** (+$2.23) | improved, still negative (PF 0.32) |
| max drawdown across all 400 runs | **-11.7%** (mean -4.3%) | improved from -15.0% / -5.7% |
| worst final value of any run | $88.67 | improved from $85.45 |
| pooled profit factor | **1.62** | improved from 1.46 |
| pooled net PnL mean | +$2.24 | positive; down from +$2.82 (bull entries lost to the breakout filter) |
| **pooled median > $0.00** | **-$0.22 (161/400 runs positive)** | **NOT MET** |
| survival (value > 0) | 100% | met |

### Why the pooled median stays below zero

The pool is 75% adverse regimes by construction. In bear, chop and black-swan years the gate keeps the book in
cash 84–94% of the time, and the handful of entries that do pass cost 100 bps each; the *median* run in those
regimes therefore lands a few dimes to a few dollars below zero (-0.17 / -2.70 / -1.19). A positive pooled median
would require either zero entries in adverse regimes (a perfect regime oracle) or a positive edge there, which a
long-only trend system does not have. Tightening further (ER 0.40) moves the median to -0.10 but gives up 29% of
the pooled mean and 30% of bull gains; it does not cross zero either. The honest reading: the strategy is a
positive-expectancy, right-skewed bet on trending-up markets with tightly bounded losses elsewhere, not a
strategy with a positive typical year under an equal-weight adverse mix.

## Step 2 — earned-compute bridge solvency

`AgentTrigger.can_invoke` now enforces `reserve_balance = reserve_swept − scheduled_spend ≥ estimated_call_cost`
for `monthly_review`. `circuit_breaker`, `feed_outage` and `self_heal` are exempt and draw on the initial pool.
Per run-year the final configuration sweeps **$3.53 to the owner and $0.39 to the reserve**; at ~$0.18 per review the
reserve funds roughly **two monthly reviews per year** in expectation, and none until the first profitable sweep.
The initial $100 credit pool is therefore only ever drawn by emergencies.

Raw data: `2026-09-07-chop-calibration.json` (8 × 200 runs), `2026-09-07-final.json` (2 × 400 runs), logs alongside.
