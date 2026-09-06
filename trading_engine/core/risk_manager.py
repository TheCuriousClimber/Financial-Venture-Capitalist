"""Pre-trade and portfolio-level risk gates. Owner-defined limits (Second Law) - never widened.

Every order passes through ``evaluate`` which returns a RiskDecision with the approved quantity
(possibly reduced) or a rejection reason.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .. import config
from ..broker.base import Position, Quote


@dataclass
class RiskDecision:
    approved: bool
    qty: float = 0.0
    notional: float = 0.0
    reason: str = ""
    checks: Dict[str, str] = field(default_factory=dict)


@dataclass
class OrderIntent:
    symbol: str
    side: str
    target_notional: float        # desired CAD notional (BUY) or ignored for full EXIT
    expected_edge_bps: float
    reason: str
    exit_all: bool = False


class RiskManager:
    SIZING_HAIRCUT = 0.995

    def __init__(self, limits: config.RiskLimits = config.RISK, principal: float = config.CAPITAL_BASE_CAD):
        self.limits = limits
        self.principal = principal
        self.day_key: Optional[str] = None
        self.day_start_equity: Optional[float] = None
        self.day_start_ts: float = 0.0
        self.halted_for_day: bool = False
        self.trades_today: int = 0
        self.last_exit_cycle: Dict[str, int] = {}

    # ------------------------------------------------------------- daily state
    @staticmethod
    def _day_key(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def roll_day(self, ts: float, equity: float) -> bool:
        """Call every cycle. Returns True if a new trading day started."""
        key = self._day_key(ts)
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = equity
            self.day_start_ts = ts
            self.halted_for_day = False
            self.trades_today = 0
            return True
        return False

    def drawdown_pct(self, equity: float) -> float:
        if not self.day_start_equity or self.day_start_equity <= 0:
            return 0.0
        return equity / self.day_start_equity - 1.0

    def check_daily_drawdown(self, equity: float) -> bool:
        """Returns True when the hard daily limit is breached (caller must flatten & halt)."""
        if self.halted_for_day:
            return True
        if self.drawdown_pct(equity) <= -self.limits.max_daily_drawdown_pct:
            self.halted_for_day = True
            return True
        return False

    def note_fill(self, symbol: str, side: str, cycle: int) -> None:
        self.trades_today += 1
        if side == "SELL":
            self.last_exit_cycle[symbol] = cycle

    # ------------------------------------------------------------- order gate
    def max_position_notional(self, equity: float) -> float:
        # Cap against current equity AND principal so a lucky run cannot inflate trade size before
        # profits are swept.
        return self.limits.max_position_pct * min(equity, max(self.principal, 1e-9)) if equity > self.principal \
            else self.limits.max_position_pct * equity

    def evaluate(self, intent: OrderIntent, quote: Quote, equity: float, cash: float,
                 positions: Dict[str, Position], round_trip_cost_bps: float, cycle: int,
                 supports_fractional: bool) -> RiskDecision:
        checks: Dict[str, str] = {}
        asset = config.ASSETS.get(intent.symbol)
        if asset is None:
            return RiskDecision(False, reason="symbol not in whitelist", checks=checks)
        if asset.asset_class == "crypto" and not self.limits.allow_crypto:
            return RiskDecision(False, reason="crypto disallowed", checks=checks)
        checks["whitelist"] = "ok"

        held = positions.get(intent.symbol)
        price = quote.ask if intent.side == "BUY" else quote.bid
        if price <= 0:
            return RiskDecision(False, reason="no valid price", checks=checks)

        if intent.side == "SELL":
            if held is None or held.qty <= 0:
                return RiskDecision(False, reason="nothing to sell (no shorting)", checks=checks)
            qty = held.qty if intent.exit_all else min(held.qty, intent.target_notional / price)
            checks["no_short"] = "ok"
            return RiskDecision(True, qty=qty, notional=qty * price, reason="exit", checks=checks)

        # ---- BUY path
        if self.halted_for_day:
            return RiskDecision(False, reason="daily drawdown halt active", checks=checks)
        checks["drawdown"] = "ok"
        if self.trades_today >= self.limits.max_trades_per_day:
            return RiskDecision(False, reason="max trades per day reached", checks=checks)
        if len(positions) >= self.limits.max_open_positions and held is None:
            return RiskDecision(False, reason="max open positions reached", checks=checks)
        checks["position_count"] = "ok"
        last_exit = self.last_exit_cycle.get(intent.symbol)
        if last_exit is not None and cycle - last_exit < self.limits.entry_cooldown_bars:
            return RiskDecision(False, reason="re-entry cooldown", checks=checks)
        checks["cooldown"] = "ok"

        cap = self.max_position_notional(equity)
        existing = held.qty * price if held else 0.0
        room = cap - existing
        if room <= 0:
            return RiskDecision(False, reason=f"position already at {self.limits.max_position_pct:.0%} cap", checks=checks)
        gross = sum(p.qty * price for p in positions.values()) if positions else 0.0
        gross_room = self.limits.max_gross_exposure_pct * equity - gross
        # 0.5% haircut so fill slippage can never push the executed notional over the cap
        notional = min(intent.target_notional, room, gross_room) * self.SIZING_HAIRCUT
        checks["max_position_pct"] = f"cap={cap:.2f}"

        if not self.limits.allow_leverage:
            cost_buffer = 1 + round_trip_cost_bps / 1e4
            notional = min(notional, cash / cost_buffer)
        checks["no_leverage"] = "ok"

        if notional < self.limits.min_order_notional_cad:
            return RiskDecision(False, reason=f"notional {notional:.2f} below minimum", checks=checks)

        if round_trip_cost_bps > self.limits.max_round_trip_cost_bps:
            return RiskDecision(False, reason=f"round-trip cost {round_trip_cost_bps:.1f}bps exceeds cap", checks=checks)
        if intent.expected_edge_bps <= 0 or round_trip_cost_bps / intent.expected_edge_bps > self.limits.max_cost_to_edge_ratio:
            return RiskDecision(False, reason=f"fees {round_trip_cost_bps:.1f}bps vs edge {intent.expected_edge_bps:.1f}bps fails ratio gate",
                                checks=checks)
        checks["fee_vs_edge"] = "ok"

        qty = notional / price
        if not supports_fractional:
            qty = float(int(qty))
            if qty < 1:
                return RiskDecision(False, reason="whole-share broker: cap below one share", checks=checks)
        qty = round(qty, 4)
        final_notional = qty * price
        if final_notional > cap + 1e-6:
            return RiskDecision(False, reason="rounding pushed notional over cap", checks=checks)
        return RiskDecision(True, qty=qty, notional=final_notional, reason=intent.reason, checks=checks)
