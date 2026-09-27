# Minimal image for the zero-token trading daemon. Runtime is stdlib-only, so nothing is pip-installed.
# python:3.11-alpine is ~50 MB compressed; the engine adds well under 1 MB.
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEDGER_PATH=/app/data/ledger.db \
    LOG_FILE=/app/data/daemon.log

# /app/data holds the ledger (+ WAL side-files), logs and proposed patches. The process runs as root:
# platform volumes (Railway, Fly) are mounted root-owned at deploy time, and a non-root USER cannot
# create ledger.db / daemon.log inside them. The container has no secrets beyond .env and no shell exposure.
RUN mkdir -p /app/data && chmod 755 /app/data

WORKDIR /app
COPY trading_engine/ /app/trading_engine/
# .env is intentionally NOT copied: mount it or pass env vars via compose/env_file (see DEPLOYMENT.md)
RUN rm -f /app/trading_engine/.env /app/trading_engine/ledger.db

# Optional: the cost-gated Claude bridge needs the SDK. Uncomment to enable in-container:
# RUN pip install --no-cache-dir anthropic>=1.4.0

HEALTHCHECK --interval=10m --timeout=20s --start-period=60s --retries=3 \
    CMD python3 -c "import sqlite3,os,sys,time; c=sqlite3.connect(os.environ['LEDGER_PATH']); \
        ts=c.execute(\"select max(ts) from equity_snapshots\").fetchone()[0]; \
        sys.exit(0 if ts and time.time()-ts < 3*int(os.environ.get('POLL_INTERVAL_SECONDS','300')) else 1)"

ENTRYPOINT ["python3", "-m", "trading_engine.daemon"]
