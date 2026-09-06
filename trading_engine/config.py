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
    max_position_pct: float = 0.05          # 5% of equity per single trade / position
    max_daily_drawdown_pct: float = 0.03    # hard stop: flatten and halt for the day
    max_open_positions: int = 3
    max_gross_exposure_pct: float = 0.15    # sum of positions <= 15% equity (3 x 5%)
    min_order_notional_cad: float = 1.00    # below this, fees/spread dominate
    max_cost_to_edge_ratio: float = 0.40    # round-trip cost must be < 40% of expected edge
    max_round_trip_cost_bps: float = 60.0   # absolute cap on round-trip cost
    allow_short: bool = False
    allow_leverage: bool = False
    allow_options: bool = False
    allow_crypto: bool = False              # spread fees (~2%) exceed alpha on micro lots
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


WATCHLIST: List[Asset] = [
    Asset("XIU.TO", "iShares S&P/TSX 60", "equity_ca", typical_spread_bps=3.0, ref_price=38.0, annual_drift=0.07, annual_vol=0.14),
    Asset("XIC.TO", "iShares Core S&P/TSX Capped Composite", "equity_ca", typical_spread_bps=4.0, ref_price=40.0, annual_drift=0.07, annual_vol=0.14),
    Asset("ZSP.TO", "BMO S&P 500 Index (CAD)", "equity_us_cadhedged", typical_spread_bps=4.0, ref_price=90.0, annual_drift=0.09, annual_vol=0.17),
    Asset("XEQT.TO", "iShares Core Equity ETF Portfolio", "equity_global", typical_spread_bps=5.0, ref_price=34.0, annual_drift=0.08, annual_vol=0.15),
    Asset("ZAG.TO", "BMO Aggregate Bond Index", "fixed_income", typical_spread_bps=6.0, ref_price=14.0, annual_drift=0.03, annual_vol=0.06),
    Asset("CGL-C.TO", "iShares Gold Bullion (non-hedged)", "commodity", typical_spread_bps=8.0, ref_price=28.0, annual_drift=0.05, annual_vol=0.16),
]
SYMBOLS: List[str] = [a.symbol for a in WATCHLIST]
ASSETS: Dict[str, Asset] = {a.symbol: a for a in WATCHLIST}


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
DEFAULT_FEE_TABLE = "wealthsimple"


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
    "vol_target_annual": (0.05, 0.25),
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
# Runtime
# --------------------------------------------------------------------------------------
BROKER: str = env("BROKER", "mock")
LIVE_TRADING_ENABLED: bool = env_bool("LIVE_TRADING_ENABLED", False)
DATA_FEED: str = env("DATA_FEED", "synthetic")
SYNTHETIC_SEED: int = env_int("SYNTHETIC_SEED", 42)
LEDGER_PATH: str = env("LEDGER_PATH", str(PACKAGE_DIR / "ledger.db"))
LOG_LEVEL: str = env("LOG_LEVEL", "INFO")
PATCH_DIR: Path = PROJECT_DIR / "patches"
AUTO_APPLY_PATCHES: bool = False   # self-heal proposals are written to disk for review, never hot-applied
