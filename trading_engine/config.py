"""Central configuration: capital limits, risk gates, fee tables, watchlists, schedules.

All values are plain Python so the daemon has no runtime dependency on third-party
packages. Environment overrides are loaded from ``trading_engine/.env`` (KEY=VALUE lines)
and the process environment; process environment wins.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


def _load_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        values[key.strip()] = val
    return values


_DOTENV = _load_dotenv(PACKAGE_DIR / ".env")


def env(key: str, default: str = "") -> str:
    """Process environment first, then .env file, then default."""
    if key in os.environ:
        return os.environ[key]
    return _DOTENV.get(key, default)


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------------------
# Finite resource pools (Zero-Injection Constraint)
# --------------------------------------------------------------------------------------
CAPITAL_BASE_CAD: float = env_float("CAPITAL_BASE_CAD", 100.00)
CREDIT_BUDGET_CAD: float = env_float("CREDIT_BUDGET_CAD", 100.00)
USD_CAD_RATE: float = env_float("USD_CAD_RATE", 1.37)

# Profit split (applies only to realized profit above CAPITAL_BASE_CAD)
OPERATIONAL_SURPLUS_PCT: float = 0.10
OWNER_DISBURSEMENT_PCT: float = 0.90
MIN_SWEEP_CAD: float = 0.10          # sweep realized profit in >= 10c lots to keep cent-rounding noise tiny
assert abs(OPERATIONAL_SURPLUS_PCT + OWNER_DISBURSEMENT_PCT - 1.0) < 1e-9


# --------------------------------------------------------------------------------------
# Risk gates (Second Law: these are owner-defined and may not be widened by the agent)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = 0.10          # 10% of equity ($10) per single trade / position; clears Kraken BTC/ETH minimums
    max_daily_drawdown_pct: float = 0.03    # hard stop: flatten and halt for the day
    max_open_positions: int = 3
    max_gross_exposure_pct: float = 0.30    # sum of positions <= 30% equity (3 x 10%)
    min_order_notional_cad: float = 1.00    # below this, fees/spread dominate
    max_cost_to_edge_ratio: float = 0.40    # round-trip cost must be < 40% of expected edge
    max_round_trip_cost_bps: float = 60.0   # absolute cap on round-trip cost (equities/ETFs)
    max_round_trip_cost_bps_crypto: float = 120.0   # bps-fee venues: 2 x taker + spread must fit here
    allow_short: bool = False
    allow_leverage: bool = False
    allow_options: bool = False
    allow_crypto: bool = True               # only via basis-point-fee spot venues (Kraken); never Wealthsimple's ~2% spread

    def cost_cap_bps(self, asset_class: str) -> float:
        return self.max_round_trip_cost_bps_crypto if asset_class == "crypto" else self.max_round_trip_cost_bps
    entry_cooldown_bars: int = 3            # bars to wait after exiting a symbol before re-entry
    max_trades_per_day: int = 6


RISK = RiskLimits()


# --------------------------------------------------------------------------------------
# Universe: liquid, CAD-denominated, commission-free, fractional-share eligible TSX ETFs.
# No FX conversion fee, no options, no leverage, no crypto.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Asset:
    symbol: str
    name: str
    asset_class: str
    currency: str = "CAD"
    typical_spread_bps: float = 5.0
    ref_price: float = 30.0        # used by the synthetic feed as a starting level
    annual_drift: float = 0.06     # synthetic feed parameters
    annual_vol: float = 0.15
    exchange_pair: str = ""        # venue-native pair name (Kraken altname), "" for TSX listings
    base_asset: str = ""           # venue-native base asset code (Kraken: XXBT, XETH, SOL ...)
    ordermin_fallback: float = 0.0 # minimum order volume if the venue cannot be queried
    lot_decimals: int = 8


WATCHLIST: List[Asset] = [
    Asset("XIU.TO", "iShares S&P/TSX 60", "equity_ca", typical_spread_bps=3.0, ref_price=38.0, annual_drift=0.07, annual_vol=0.14),
    Asset("XIC.TO", "iShares Core S&P/TSX Capped Composite", "equity_ca", typical_spread_bps=4.0, ref_price=40.0, annual_drift=0.07, annual_vol=0.14),
    Asset("ZSP.TO", "BMO S&P 500 Index (CAD)", "equity_us_cadhedged", typical_spread_bps=4.0, ref_price=90.0, annual_drift=0.09, annual_vol=0.17),
    Asset("XEQT.TO", "iShares Core Equity ETF Portfolio", "equity_global", typical_spread_bps=5.0, ref_price=34.0, annual_drift=0.08, annual_vol=0.15),
    Asset("ZAG.TO", "BMO Aggregate Bond Index", "fixed_income", typical_spread_bps=6.0, ref_price=14.0, annual_drift=0.03, annual_vol=0.06),
    Asset("CGL-C.TO", "iShares Gold Bullion (non-hedged)", "commodity", typical_spread_bps=8.0, ref_price=28.0, annual_drift=0.05, annual_vol=0.16),
]
TSX_WATCHLIST: List[Asset] = WATCHLIST

# Kraken CAD spot pairs: percentage fees, 24/7, fractional volume. ordermin values are Kraken's
# published minimums (refreshed live from /0/public/AssetPairs at startup when reachable).
CRYPTO_WATCHLIST: List[Asset] = [
    Asset("BTC/CAD", "Bitcoin / CAD", "crypto", typical_spread_bps=8.0, ref_price=140000.0, annual_drift=0.20, annual_vol=0.55,
          exchange_pair="XBTCAD", base_asset="XXBT", ordermin_fallback=0.00005, lot_decimals=8),
    Asset("ETH/CAD", "Ether / CAD", "crypto", typical_spread_bps=10.0, ref_price=4500.0, annual_drift=0.15, annual_vol=0.70,
          exchange_pair="ETHCAD", base_asset="XETH", ordermin_fallback=0.002, lot_decimals=8),
    Asset("SOL/CAD", "Solana / CAD", "crypto", typical_spread_bps=15.0, ref_price=220.0, annual_drift=0.15, annual_vol=0.90,
          exchange_pair="SOLCAD", base_asset="SOL", ordermin_fallback=0.02, lot_decimals=8),
    Asset("XRP/CAD", "XRP / CAD", "crypto", typical_spread_bps=15.0, ref_price=3.0, annual_drift=0.10, annual_vol=0.85,
          exchange_pair="XRPCAD", base_asset="XXRP", ordermin_fallback=2.0, lot_decimals=8),
    Asset("ADA/CAD", "Cardano / CAD", "crypto", typical_spread_bps=20.0, ref_price=1.0, annual_drift=0.10, annual_vol=0.90,
          exchange_pair="ADACAD", base_asset="ADA", ordermin_fallback=5.0, lot_decimals=8),
    Asset("DOGE/CAD", "Dogecoin / CAD", "crypto", typical_spread_bps=20.0, ref_price=0.25, annual_drift=0.05, annual_vol=1.00,
          exchange_pair="DOGECAD", base_asset="XXDG", ordermin_fallback=20.0, lot_decimals=8),
]

# Active universe: "tsx" (synthetic/yahoo + wealthsimple-style fees) or "crypto" (Kraken CAD pairs)
ASSET_UNIVERSE: str = env("ASSET_UNIVERSE", "tsx")
WATCHLIST = CRYPTO_WATCHLIST if ASSET_UNIVERSE == "crypto" else TSX_WATCHLIST
SYMBOLS: List[str] = [a.symbol for a in WATCHLIST]
# Whitelist covers both universes so a symbol from the other universe is still recognised (and gated by class)
ASSETS: Dict[str, Asset] = {a.symbol: a for a in TSX_WATCHLIST + CRYPTO_WATCHLIST}


# --------------------------------------------------------------------------------------
# Fee tables for Canadian retail brokers (CAD accounts). Values in CAD unless bps.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FeeSchedule:
    broker: str
    commission_per_trade: float          # flat commission (buy)
    sell_commission_per_trade: float     # flat commission (sell)
    commission_bps: float                # proportional commission
    min_commission: float
    spread_bps_default: float            # half-spread paid each side ~ spread/2
    slippage_bps_mean: float             # market impact / latency on top of spread
    slippage_bps_std: float
    fx_conversion_bps: float             # only charged for non-CAD instruments
    supports_fractional: bool
    min_order_notional: float


FEE_TABLES: Dict[str, FeeSchedule] = {
    # Wealthsimple Trade: $0 commission on CAD stocks/ETFs, fractional shares on eligible names.
    "wealthsimple": FeeSchedule("wealthsimple", 0.0, 0.0, 0.0, 0.0, 5.0, 1.0, 1.5, 150.0, True, 1.00),
    # Questrade: ETF buys free, sells $4.95-$9.95 -> non-viable for $5 trades (gate will reject).
    "questrade": FeeSchedule("questrade", 0.0, 4.95, 1.0, 4.95, 5.0, 1.0, 1.5, 175.0, False, 1.00),
    # IBKR Canada Pro tiered: CAD 0.008/share, min CAD 1.00 -> ~20% of a $5 trade; rejected by gate.
    "ibkr_ca": FeeSchedule("ibkr_ca", 1.00, 1.00, 0.0, 1.00, 4.0, 0.5, 1.0, 20.0, True, 1.00),
}
# Kraken spot, percentage fees. Published tier-0 (< $10k 30-day volume) is 0.25% maker / 0.40% taker;
# older docs quote 0.16% / 0.26%. Override with KRAKEN_MAKER_BPS / KRAKEN_TAKER_BPS when your tier differs.
KRAKEN_MAKER_BPS: float = env_float("KRAKEN_MAKER_BPS", 25.0)
KRAKEN_TAKER_BPS: float = env_float("KRAKEN_TAKER_BPS", 40.0)
# Market orders are taker orders; the live schedule therefore models the taker rate. A $5 trade pays ~2c/side.
FEE_TABLES["kraken"] = FeeSchedule("kraken", 0.0, 0.0, KRAKEN_TAKER_BPS, 0.0, 10.0, 2.0, 2.0, 0.0, True, 1.00)
# Paper-soak schedule: same venue, fee rate pinned by PAPER_FEE_BPS (default 0.40% = tier-0 taker, what market orders pay).
PAPER_FEE_BPS: float = env_float("PAPER_FEE_BPS", 40.0)
FEE_TABLES["kraken_paper"] = FeeSchedule("kraken_paper", 0.0, 0.0, PAPER_FEE_BPS, 0.0, 10.0, 2.0, 2.0, 0.0, True, 1.00)
DEFAULT_FEE_TABLE = "kraken_paper" if ASSET_UNIVERSE == "crypto" else "wealthsimple"


# --------------------------------------------------------------------------------------
# Strategy parameters. The agent may tune these ONLY within STRATEGY_PARAM_BOUNDS.
# --------------------------------------------------------------------------------------
STRATEGY_PARAMS: Dict[str, float] = {
    "fast_sma": 20,
    "slow_sma": 50,
    "rsi_period": 14,
    "rsi_entry_min": 45.0,
    "rsi_entry_max": 65.0,
    "rsi_exit": 75.0,
    "momentum_lookback": 20,
    "atr_period": 14,
    "atr_stop_mult": 2.0,
    "vol_target_annual": 0.12,      # scale position size down when realized vol is above this
    "edge_capture": 0.25,           # fraction of trailing momentum assumed capturable (for fee gate)
    "regime_vol_z": 2.5,            # realized-vol z-score that flags a regime shift
}
if ASSET_UNIVERSE == "crypto":
    # Crypto runs at 50-100% annualised vol; the ETF vol target would shrink a $5 cap to ~$1 and fall
    # under every Kraken order minimum. Sizing still never exceeds the 5% cap.
    STRATEGY_PARAMS.update({"vol_target_annual": 0.80, "atr_stop_mult": 2.5, "regime_vol_z": 3.0})
STRATEGY_PARAM_BOUNDS: Dict[str, tuple] = {
    "fast_sma": (5, 40),
    "slow_sma": (30, 200),
    "rsi_period": (7, 28),
    "rsi_entry_min": (30.0, 55.0),
    "rsi_entry_max": (55.0, 75.0),
    "rsi_exit": (65.0, 90.0),
    "momentum_lookback": (10, 120),
    "atr_period": (7, 30),
    "atr_stop_mult": (1.0, 4.0),
    "vol_target_annual": (0.05, 0.25) if ASSET_UNIVERSE != "crypto" else (0.20, 1.50),
    "edge_capture": (0.05, 0.5),
    "regime_vol_z": (1.5, 4.0),
}


# --------------------------------------------------------------------------------------
# Schedules (seconds). The polling loop is local & free; only the bridge costs money.
# --------------------------------------------------------------------------------------
POLL_INTERVAL_SECONDS: int = env_int("POLL_INTERVAL_SECONDS", 300)
BAR_SECONDS: int = 24 * 3600                         # strategy operates on daily bars
WEEKLY_REVIEW_INTERVAL_SECONDS: int = 7 * 24 * 3600  # scheduled strategy evaluation
REGIME_TRIGGER_MIN_INTERVAL_SECONDS: int = 3 * 24 * 3600
SELF_HEAL_MIN_INTERVAL_SECONDS: int = 6 * 3600
SELF_HEAL_CONSECUTIVE_ERRORS: int = 3
EQUITY_SNAPSHOT_EVERY_CYCLES: int = 1


# --------------------------------------------------------------------------------------
# Claude bridge economics (USD per million tokens; converted to CAD in the ledger)
# --------------------------------------------------------------------------------------
CLAUDE_MODEL: str = env("CLAUDE_MODEL", "claude-fable-5-1")
CLAUDE_FALLBACK_MODEL: str = "claude-opus-4-8"
CLAUDE_PRICING_USD_PER_M: Dict[str, Dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
}
CLAUDE_MAX_OUTPUT_TOKENS: int = 1500
CLAUDE_MAX_INPUT_CHARS: int = 24_000              # ~6k tokens hard cap on context size
CLAUDE_MAX_COST_PER_CALL_CAD: float = 1.50
CLAUDE_CREDIT_FLOOR_CAD: float = 5.00              # never spend below this reserve
CLAUDE_BRIDGE_ENABLED: bool = env_bool("CLAUDE_BRIDGE_ENABLED", False)

# --------------------------------------------------------------------------------------
# HTTP: Cloudflare-fronted APIs (Kraken, Yahoo) reject Python's default User-Agent with 403.
# Every urllib request in this package sends USER_AGENT and a bounded timeout.
# --------------------------------------------------------------------------------------
USER_AGENT: str = env("HTTP_USER_AGENT", "AutonomousQuantTrading/1.0 (Macintosh; Intel Mac OS X 10_15_7)")
HTTP_TIMEOUT_SECONDS: float = env_float("HTTP_TIMEOUT_SECONDS", 10.0)

# --------------------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------------------
BROKER: str = env("BROKER", "mock")                       # mock | kraken
LIVE_TRADING_ENABLED: bool = env_bool("LIVE_TRADING_ENABLED", False)
# Paper-soak: live market data (kraken_live / yahoo) but every order routes to MockBroker with the venue's
# real spreads and the PAPER_FEE_BPS fee model. Overrides BROKER=kraken so no real order can be sent.
PAPER_LIVE_FEED: bool = env_bool("PAPER_LIVE_FEED", True)
DATA_FEED: str = env("DATA_FEED", "synthetic")           # synthetic | yahoo | kraken_live
KRAKEN_API_KEY: str = env("KRAKEN_API_KEY", "")
KRAKEN_PRIVATE_KEY: str = env("KRAKEN_PRIVATE_KEY", "")
WEBHOOK_URL: str = env("WEBHOOK_URL", "")                 # Discord webhook, Telegram bot sendMessage URL, or generic JSON endpoint
TELEGRAM_CHAT_ID: str = env("TELEGRAM_CHAT_ID", "")
SYNTHETIC_SEED: int = env_int("SYNTHETIC_SEED", 42)
LEDGER_PATH: str = env("LEDGER_PATH", str(PACKAGE_DIR / "ledger.db"))
LOG_LEVEL: str = env("LOG_LEVEL", "INFO")
PATCH_DIR: Path = PROJECT_DIR / "patches"
AUTO_APPLY_PATCHES: bool = False   # self-heal proposals are written to disk for review, never hot-applied


# --------------------------------------------------------------------------------------
# Startup validation: refuse to run a mode whose prerequisites are missing.
# --------------------------------------------------------------------------------------
def validate(strict_live: bool = True) -> List[str]:
    """Return a list of configuration problems (empty list == OK)."""
    problems: List[str] = []
    if CAPITAL_BASE_CAD <= 0 or CREDIT_BUDGET_CAD <= 0:
        problems.append("CAPITAL_BASE_CAD and CREDIT_BUDGET_CAD must be positive")
    if DATA_FEED not in ("synthetic", "yahoo", "kraken_live"):
        problems.append(f"DATA_FEED={DATA_FEED!r} unknown (synthetic | yahoo | kraken_live)")
    if BROKER not in ("mock", "kraken"):
        problems.append(f"BROKER={BROKER!r} unknown (mock | kraken)")
    if ASSET_UNIVERSE not in ("tsx", "crypto"):
        problems.append(f"ASSET_UNIVERSE={ASSET_UNIVERSE!r} unknown (tsx | crypto)")
    if DATA_FEED == "kraken_live" and ASSET_UNIVERSE != "crypto":
        problems.append("DATA_FEED=kraken_live requires ASSET_UNIVERSE=crypto")
    if BROKER == "kraken" and ASSET_UNIVERSE != "crypto":
        problems.append("BROKER=kraken requires ASSET_UNIVERSE=crypto")
    if BROKER == "kraken" and not PAPER_LIVE_FEED:
        if not KRAKEN_API_KEY or not KRAKEN_PRIVATE_KEY:
            problems.append("BROKER=kraken with PAPER_LIVE_FEED=false needs KRAKEN_API_KEY and KRAKEN_PRIVATE_KEY")
        if strict_live and not LIVE_TRADING_ENABLED:
            problems.append("BROKER=kraken with PAPER_LIVE_FEED=false also needs LIVE_TRADING_ENABLED=true (explicit opt-in)")
        if DATA_FEED == "synthetic":
            problems.append("live broker cannot run on the synthetic feed")
    if CLAUDE_BRIDGE_ENABLED and not env("ANTHROPIC_API_KEY"):
        problems.append("CLAUDE_BRIDGE_ENABLED=true but ANTHROPIC_API_KEY is empty")
    if WEBHOOK_URL and "api.telegram.org" in WEBHOOK_URL and not TELEGRAM_CHAT_ID:
        problems.append("Telegram WEBHOOK_URL requires TELEGRAM_CHAT_ID")
    if WEBHOOK_URL and not WEBHOOK_URL.startswith("https://"):
        problems.append("WEBHOOK_URL must use https")
    return problems
