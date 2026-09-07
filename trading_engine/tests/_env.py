"""Pin the configuration the unit tests assume, regardless of the developer's .env or shell.

Import this module FIRST in every test file (before any ``trading_engine`` import). The config loader lets
process environment win over .env, so setting these here makes the suite deterministic.
"""
import os

os.environ.update({
    "ASSET_UNIVERSE": "tsx",
    "DATA_FEED": "synthetic",
    "BROKER": "mock",
    "PAPER_LIVE_FEED": "true",
    "LIVE_TRADING_ENABLED": "false",
    "CLAUDE_BRIDGE_ENABLED": "false",
    "WEBHOOK_URL": "",
    "TELEGRAM_CHAT_ID": "",
    "KRAKEN_API_KEY": "",
    "KRAKEN_PRIVATE_KEY": "",
    "PAPER_FEE_BPS": "40",
    "KRAKEN_TAKER_BPS": "40",
    "CAPITAL_BASE_CAD": "100.00",
    "CREDIT_BUDGET_CAD": "100.00",
    "LEDGER_PATH": ":memory:",
})
