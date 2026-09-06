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
  broker/live_broker.py    Questrade-shaped CAD adapter (urllib only) behind LIVE_TRADING_ENABLED
  core/indicators.py       SMA/EMA/RSI/ATR/momentum/realized vol/vol z-score (pure Python)
  core/risk_manager.py     5% per-position cap, 3% daily drawdown halt, no leverage/shorts, fee-vs-edge gate
  core/strategy.py         trend + RSI pullback entries, ATR trailing stops, regime detector
  bridge/agent_trigger.py  the ONLY module that can spend credits; every call is gated and metered
  data/feed.py             SyntheticFeed (seeded GBM) and YahooChartFeed (free daily bars)
  daemon.py                zero-cost polling loop (time.sleep), self-healing, profit sweeps
  dry_run.py               100-cycle proof: $0 tokens, 5% gate, drawdown gate, 90/10 reconciliation
  tests/                   27 unit tests (python -m unittest discover -s trading_engine/tests)
```

No third-party packages are required to run the daemon or the tests. `anthropic` is imported lazily
only when the bridge actually fires.

## Quick start

```bash
python3 -m unittest discover -s trading_engine/tests          # 27 tests
python3 -m trading_engine.dry_run --cycles 100 --verbose       # mock broker, synthetic feed, hard assertions
python3 -m trading_engine.daemon --cycles 100 --fast           # same loop via the daemon CLI
python3 -m trading_engine.daemon                               # 24/7: polls every POLL_INTERVAL_SECONDS
```

## Safety model

* **Risk gates are owner-defined and immutable at runtime.** The agent can only tune
  `STRATEGY_PARAMS` inside `STRATEGY_PARAM_BOUNDS`; anything else in its reply is discarded.
* **Sizing:** 5% of min(equity, principal) per position, 15% gross, 3 positions max, 0.5% haircut so
  slippage cannot push a fill over the cap. Cash-only, long-only, whitelisted CAD ETFs only.
* **Drawdown:** day-start equity is the prior close. Breaching -3% flattens everything and blocks new
  entries until the next day.
* **Fees:** every entry must pass `round_trip_cost_bps < 40% of expected edge` and `< 60 bps` absolute.
  This is what rejects Questrade/IBKR-style commissions on $5 trades (≈9,900 bps round trip).
* **Profit split:** realized profit above principal is swept in ≥ $0.10 lots as 90% owner disbursement
  / 10% operational reserve, rounded to cents. The reserve extends the compute credit pool.
* **Bridge gating:** enabled flag, API key, credit floor ($5), per-call cap ($1.50), 25%-of-remaining cap,
  per-reason minimum interval. Estimated cost of one weekly review ≈ $0.18 CAD.
* **Self-heal:** after 3 consecutive tick failures the daemon enters safe mode (no new entries) and
  asks Claude (cost-gated) for a diagnosis. Any proposed diff is written to `patches/` for human review;
  `AUTO_APPLY_PATCHES` is hard-coded `False`.
* **Live trading:** `LiveBroker.submit_order` refuses unless `LIVE_TRADING_ENABLED=true`. Real
  credentials go in `trading_engine/.env`, which is git-ignored.

## Honest caveats

* With $100 and a 5% cap, positions are ~$5. Only a commission-free broker with fractional shares
  (Wealthsimple-style) makes this viable; Questrade's public API is read-only for retail order
  placement, so `live_broker.py` is a documented adapter shape, not a turnkey path to live fills.
* Expected absolute returns are cents per trade. Over 12 seeds × 100 cycles the mock P&L ranged
  roughly −$1.07 to +$1.53 including swept profit — the system is a capital-preservation and
  process-correctness proof at this scale, not an income source.
* Canadian tax: every fill is logged with timestamp, price, and fees so CRA adjusted-cost-base and
  capital-gains reporting can be reconstructed from `trades`.
