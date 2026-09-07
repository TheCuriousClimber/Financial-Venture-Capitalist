# Deployment & operational runbook

The engine is a stdlib-only Python 3.11 daemon. It needs outbound HTTPS to `api.kraken.com` (and optionally
`query1.finance.yahoo.com` as a data fallback, `api.anthropic.com` for the cost-gated bridge, and your webhook host).
All state lives in one SQLite file (`ledger.db`), so any of the three deployment shapes below can be swapped for
another by copying that file.

Requirements: Python 3.11+ (no packages), or Docker. `trading_engine/.env` holds all configuration and is git-ignored.

---

## a. 48-hour live paper soak on a local machine

Real Kraken prices and spreads, mock execution with the 0.40% taker fee model, real order minimums, no keys needed.

**macOS / Linux**
```bash
git clone https://github.com/TheCuriousClimber/Financial-Venture-Capitalist.git
cd Financial-Venture-Capitalist
cp trading_engine/.env.example trading_engine/.env      # defaults: ASSET_UNIVERSE=crypto DATA_FEED=kraken_live PAPER_LIVE_FEED=true LIVE_TRADING_ENABLED=false
python3 -m unittest discover -s trading_engine/tests   # 67 tests
python3 -m trading_engine.diagnostics                  # must show [PASS] kraken_connectivity and live bid/ask
nohup python3 -m trading_engine.daemon --log-file daemon.log > /dev/null 2>&1 &
echo $! > daemon.pid
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/TheCuriousClimber/Financial-Venture-Capitalist.git
cd Financial-Venture-Capitalist
Copy-Item trading_engine\.env.example trading_engine\.env
python -m unittest discover -s trading_engine\tests
python -m trading_engine.diagnostics
Start-Process -WindowStyle Hidden python -ArgumentList "-m trading_engine.daemon --log-file daemon.log"
```

**Watch it (any OS)**
```bash
tail -f daemon.log            # one line per fill / gate change / sweep; errors are explicit
python3 - <<'PYEOF'
from trading_engine.ledger import Ledger
l = Ledger("trading_engine/ledger.db")
print(l.summary())
for e in reversed(l.events(limit=20)):
    print(e["kind"], "|", e["message"][:120])
PYEOF
```

**Pass criteria after 48 h** (check the ledger, not the log):
- `tokens.calls == 0` and `credit_spent_cad == 0.0`
- no `tick_error` events, or only transient ones each followed by a `safe_mode_exit`
- `regime_gate` events present; `order_rejected` reasons are the expected ones (venue minimum, fee gate, cooldown)
- every `trades` row satisfies `notional <= 0.10 * equity_at_order`

Stop it: `kill $(cat daemon.pid)` on macOS/Linux, `Stop-Process -Name python` on Windows.

---

## b. Free daily cron with `--once`

The strategy evaluates daily candles, so one pass per day is equivalent to the loop. A pass takes about 1-3 s live
(network bound) and exits 0 on success, 1 if the pass raised, 2 on a config error. State (cycle/bar counters,
trailing stops, cooldowns, cost basis, review timestamps) is persisted in `ledger.db` between runs.

```bash
python3 -m trading_engine.daemon --once              # try it once by hand first
crontab -e
```
Add one line. Kraken daily candles close at 00:00 UTC, so run a few minutes after:
```
5 0 * * * cd /path/to/Financial-Venture-Capitalist && /usr/bin/python3 -m trading_engine.daemon --once --log-file daemon.log >> cron.log 2>&1
```
For intraday trailing-stop checks on live quotes add a second cadence. The pass is idempotent: entries only
happen once per new daily bar, so extra passes only ever check stops.
```
*/30 * * * * cd /path/to/Financial-Venture-Capitalist && /usr/bin/python3 -m trading_engine.daemon --once >> cron.log 2>&1
```
GitHub Actions or any serverless runner works the same way, provided `ledger.db` is persisted between runs (cache
or artifact). Without persistence the engine restarts from cycle 0 and loses its trailing stops.

Windows Task Scheduler: Program `python`, Arguments `-m trading_engine.daemon --once`, Start in: the repo folder.

---

## c. Docker Compose on a VPS or cloud runner

```bash
git clone https://github.com/TheCuriousClimber/Financial-Venture-Capitalist.git && cd Financial-Venture-Capitalist
cp trading_engine/.env.example trading_engine/.env && nano trading_engine/.env
mkdir -p data
docker compose build                                   # python:3.11-alpine base, nothing pip-installed
docker compose run --rm diagnostics                    # connectivity, spreads, minimums, ledger I/O
docker compose up -d                                   # 24/7 loop
docker compose logs -f daemon
```
State persists in `./data` (`ledger.db`, `-wal`/`-shm` side-files, `daemon.log`, proposed patches). Back it up with
`sqlite3 data/ledger.db ".backup data/ledger-$(date +%F).db"`.

Single pass inside the container, for a host cron on the VPS: `docker compose run --rm once`.

| provider | notes |
|---|---|
| DigitalOcean / Hetzner / any VPS | the commands above as-is; the container is capped at 128 MB RAM and 0.25 CPU |
| Fly.io | `fly launch --no-deploy` (Dockerfile detected), `fly volumes create data --size 1`, mount it at `/app/data` in `fly.toml`, `fly secrets set` each `.env` key, `fly deploy` |
| Railway | new service from the repo, add a volume mounted at `/app/data`, paste the `.env` keys as variables, deploy |

For the Claude bridge inside the container, uncomment the `pip install anthropic` line in the Dockerfile and set
`ANTHROPIC_API_KEY` plus `CLAUDE_BRIDGE_ENABLED=true`. The earned-compute rule still applies: routine reviews only
fire once the 10% reserve has funded them.

---

## d. Flipping from paper to live Kraken execution

Do this only after a clean 48 h soak (section a pass criteria) on the machine that will run live.

**1. Create the Kraken API key** (Kraken, Settings, API, Add key) with exactly these permissions:

| permission | setting |
|---|---|
| Query Funds | enabled |
| Query Open Orders & Trades, Query Closed Orders & Trades | enabled (fill polling) |
| Create & Modify Orders | enabled |
| Cancel/Close Orders | enabled |
| **Withdraw Funds** | **disabled** |
| Deposit, Export Data, Staking, Earn, Sub-accounts | disabled |
| Key restrictions | IP whitelist = the deployment host |

The adapter also refuses every withdrawal and transfer endpoint in code (`FORBIDDEN_ENDPOINTS`), but the key must
not carry the permission in the first place.

**2. Fund the CAD account** with exactly the principal (`CAPITAL_BASE_CAD`, $100.00). Profit sweeps are booked as
pending manual transfers; you move the 90% owner share out through the Kraken UI when the webhook tells you.

**3. Edit `trading_engine/.env`:**
```
ASSET_UNIVERSE=crypto
DATA_FEED=kraken_live
BROKER=kraken
PAPER_LIVE_FEED=false          # the switch: true forces the mock broker regardless of the rest
LIVE_TRADING_ENABLED=true      # explicit opt-in: the adapter refuses to send orders without it
KRAKEN_API_KEY=...
KRAKEN_PRIVATE_KEY=...
WEBHOOK_URL=...                # sweeps, drawdown halts and safe-mode entries alert here
```
`python3 -m trading_engine.daemon` refuses to start if `config.validate()` finds these inconsistent.

**4. Pre-flight**
```bash
python3 -m trading_engine.diagnostics        # RESULT: PASS, order minimums fit the $10 cap at today's prices
python3 -m trading_engine.daemon --once      # first live pass; inspect the ledger events table afterwards
```
Then start the loop (a, b or c). Keep the paper `ledger.db` as `ledger-paper.db` and start live from a fresh ledger
so realized PnL and the 90/10 split begin at the true principal.

**Kill switches, in order of severity**
- `LIVE_TRADING_ENABLED=false` then restart: the adapter refuses all orders (positions stay open, stops stop firing).
- `PAPER_LIVE_FEED=true` then restart: same, but the mock broker keeps evaluating so you can see what it would do.
- Kraken UI, disable the API key: immediate and unconditional.
