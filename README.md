# Financial Venture Capitalist — autonomous micro-account trading engine

A self-contained trading daemon that manages two strictly finite pools:

| Pool | Size | Tracked in |
|---|---|---|
| Investment capital | $100.00 CAD | `ledger.db` → `trades`, `equity_snapshots`, `profit_splits` |
| Compute credits (Claude Fable 5.1) | $100.00 CAD | `ledger.db` → `token_expenditures` |

Neither pool is ever topped up. The daemon runs 100% locally at **zero token cost**; Claude is invoked
only through a cost-gated bridge for weekly strategy reviews, volatility regime shifts, or self-healing.

## Layout

```
trading_engine/
  .env / .env.example      capital base, credit budget, broker + data feed selection, API keys
  config.py                watchlist (TSX ETFs), fee tables (Wealthsimple / Questrade / IBKR CA), risk limits, schedules
  ledger.py                SQLite: trades, equity, token spend, realized PnL, 90/10 split, events, state
  broker/base.py           Broker / Order / Fill / Position / Quote abstractions + shared cost model
  broker/mock_broker.py    paper broker: spread, slippage, commission, FX fee modelling, avg-cost PnL
  broker/live_broker.py    KrakenBroker: CAD spot pairs, HMAC-SHA512 auth (stdlib), no withdrawal capability
  broker/questrade_broker.py  reference-only fixed-commission adapter (non-viable at $5 trades)
  notifications.py         stdlib webhook dispatcher (Discord / Telegram / generic JSON) for sweeps + alerts
  core/indicators.py       SMA/EMA/RSI/ATR/momentum/realized vol/vol z-score (pure Python)
  core/risk_manager.py     5% per-position cap, 3% daily drawdown halt, no leverage/shorts, fee-vs-edge gate
  core/strategy.py         trend + RSI pullback entries, ATR trailing stops, regime detector
  bridge/agent_trigger.py  the ONLY module that can spend credits; every call is gated and metered
  data/feed.py             SyntheticFeed (seeded GBM), YahooChartFeed, KrakenFeed (public OHLC/Ticker + order minimums)
  daemon.py                zero-cost polling loop (time.sleep), self-healing, profit sweeps
  dry_run.py               100-cycle proof: $0 tokens, position cap, drawdown gate, 90/10 reconciliation
  diagnostics.py           pre-flight: Kraken connectivity, live spreads, minimum-order math, ledger I/O
  core/backtester.py       4-regime Monte Carlo (bull/bear/chop/black swan) through the production stack
  tests/                   47 unit tests (python -m unittest discover -s trading_engine/tests)
```

No third-party packages are required to run the daemon or the tests. `anthropic` is imported lazily
only when the bridge actually fires.

## Quick start

```bash
python3 -m unittest discover -s trading_engine/tests          # 47 tests
python3 -m trading_engine.dry_run --cycles 100 --verbose       # mock broker, synthetic feed, hard assertions
python3 -m trading_engine.daemon --cycles 100 --fast           # same loop via the daemon CLI
python3 -m trading_engine.daemon                               # 24/7: polls every POLL_INTERVAL_SECONDS
ASSET_UNIVERSE=crypto python3 -m trading_engine.dry_run        # offline crypto universe with Kraken minimums
```

## Going live: Kraken CAD pairs

Fixed-commission Canadian equity brokers ($1.00-$4.95 per trade) consume 20-99% of a $5 trade, so the
production venue is Kraken spot with percentage fees (tier-0: 0.25% maker / 0.40% taker, about 4c per side
on $10). `broker/live_broker.py` talks to Kraken's REST API with `urllib`, `hmac` and `hashlib` only.

**Three-stage rollout, all controlled from `trading_engine/.env`:**

| Stage | Settings | What executes |
|---|---|---|
| 1. Offline dry run | `DATA_FEED=synthetic` `BROKER=mock` | mock fills on synthetic bars |
| 2. Paper soak (48h) | `ASSET_UNIVERSE=crypto` `DATA_FEED=kraken_live` `PAPER_LIVE_FEED=true` | mock fills at Kraken's **live bid/ask**, `PAPER_FEE_BPS` (0.40% taker) fee, Kraken order minimums enforced |
| 3. Live | `BROKER=kraken` `PAPER_LIVE_FEED=false` `LIVE_TRADING_ENABLED=true` + keys | real market orders |

`PAPER_LIVE_FEED=true` overrides `BROKER=kraken`, so a mis-set flag can only ever paper trade.
`python -m trading_engine.daemon` refuses to start when `config.validate()` finds a problem.

**API key scope.** Create the Kraken key with *Query Funds* and *Create & Modify Orders* only. The adapter
has no withdrawal method, refuses every `Withdraw*` / `WalletTransfer` / deposit endpoint in code, and only
calls an explicit allow-list. Profit sweeps are therefore booked in the ledger as *pending manual transfer*
and announced on the webhook; you move the 90% to your bank from the Kraken UI.

**Order minimums vs. the 10% cap.** Kraken enforces a minimum volume per pair. At recent prices BTC/CAD
(0.00005 BTC ≈ $7) and ETH/CAD (0.002 ETH ≈ $9) fit under a $10 position with little headroom, so a BTC rally
above ~$200k CAD would push BTC/CAD back under the minimum and the gate would reject it. Run
`python -m trading_engine.diagnostics` to check the live math. Minimums are refreshed from
`/0/public/AssetPairs` at startup and mirrored into paper mode.

**Webhook.** Set `WEBHOOK_URL` (Discord webhook, Telegram `sendMessage` URL + `TELEGRAM_CHAT_ID`, or any
HTTPS JSON endpoint). Every `reconcile_90_10_split()` sweep posts the realized profit, the 10% compute
reserve retained and the 90% segregated for withdrawal; drawdown halts and safe-mode entries also alert.

## Safety model

* **Risk gates are owner-defined and immutable at runtime.** The agent can only tune
  `STRATEGY_PARAMS` inside `STRATEGY_PARAM_BOUNDS`; anything else in its reply is discarded.
* **Sizing:** 10% of min(equity, principal) per position ($10), 30% gross, 3 positions max, 0.5% haircut
  so slippage cannot push a fill over the cap. Cash-only, long-only, whitelisted CAD instruments only.
* **Drawdown:** day-start equity is the prior close. Breaching -3% flattens everything and blocks new
  entries until the next day.
* **Fees:** every entry must pass `round_trip_cost_bps < 40% of expected edge` and an absolute cap of
  60 bps (equities) / 120 bps (crypto, where 2 × taker + spread ≈ 70-90 bps). This is what rejects
  Questrade/IBKR-style commissions on $5 trades (≈9,900 bps round trip).
* **Profit split:** realized profit above principal is swept in ≥ $0.10 lots as 90% owner disbursement
  / 10% operational reserve, rounded to cents. The reserve extends the compute credit pool.
* **Bridge gating:** enabled flag, API key, credit floor ($5), per-call cap ($1.50), 25%-of-remaining cap,
  per-reason minimum interval, trailing-365-day cap on scheduled spend. Scheduled review is monthly (~$0.18 CAD
  per call, ~$2.16/yr); intermediate calls only on a circuit-breaker trip, a feed outage, or a code exception.
* **Self-heal:** after 3 consecutive tick failures the daemon enters safe mode (no new entries) and
  asks Claude (cost-gated) for a diagnosis. Any proposed diff is written to `patches/` for human review;
  `AUTO_APPLY_PATCHES` is hard-coded `False`.
* **Live trading:** `KrakenBroker.submit_order` refuses unless `LIVE_TRADING_ENABLED=true`, and
  `PAPER_LIVE_FEED=true` forces the mock broker regardless. Credentials live in the git-ignored `.env`.

## Backtest verdict (2026-09-07)

`python3 -m trading_engine.core.backtester --seeds 100` runs 365 daily cycles × 100 seeds × 4 regimes through the
real daemon with 40 bps taker + 10 bps slippage per side.

| | pooled net PnL mean / median | PF | max DD mean | fees/yr |
|---|---|---|---|---|
| original RSI/pullback defaults | -$1.20 / -$3.22 | 0.92 | -11.1% | $1.75 |
| slow-trend + cash gate (current defaults) | **+$2.82 / -$0.85** | **1.46** | **-5.7%** | $0.48 |

The cash gate (basket below its 100d SMA with negative 60d momentum → 100% CAD) removed 69% of chop turnover and
made the bear regime break-even; chop is still net negative (PF 0.31). Details and next steps:
`docs/backtest/2026-09-07-gated-verdict.md` (v2) and `docs/backtest/2026-09-07-verdict.md` (v1).

## Honest caveats

* With $100 and a 5% cap, positions are ~$5. Percentage-fee venues (Kraken) or commission-free
  fractional equity brokers are the only viable execution paths; Questrade's adapter is kept for reference.
* Kraken's public API was unreachable from the build sandbox, so the live adapter is verified against the
  documented signature test vector and fixture responses, not against the exchange. Run the paper soak first.
* Crypto at 50-100% annualised vol with a 3% daily halt means a single bad day on 15% gross exposure can
  trip the halt; that is the intended behaviour, not a bug.
* Expected absolute returns are cents per trade. Over 12 seeds × 100 cycles the mock P&L ranged
  roughly −$1.07 to +$1.53 including swept profit — the system is a capital-preservation and
  process-correctness proof at this scale, not an income source.
* Canadian tax: every fill is logged with timestamp, price, and fees so CRA adjusted-cost-base and
  capital-gains reporting can be reconstructed from `trades`.
