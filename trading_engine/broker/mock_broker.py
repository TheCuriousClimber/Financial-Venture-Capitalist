"""Paper broker with realistic Canadian micro-lot fee, spread and slippage modelling.

Fills are simulated against the feed's quote:
  * BUY fills at ask (+ slippage), SELL fills at bid (- slippage)
  * slippage ~ N(mean, std) bps, floored at 0, scaled up for larger notional vs. volume
  * commission per fee schedule (flat + bps, with minimum), FX fee for non-CAD instruments
Positions use average-cost accounting; realized PnL on SELL is (fill - avg_cost) * qty (gross).
"""
from __future__ import annotations

import random
from typing import Dict, Optional

from .. import config
from ..data.feed import Feed
from .base import Broker, BrokerError, Fill, Order, Position, Quote


class MockBroker(Broker):
    name = "mock"

    def __init__(self, feed: Feed, starting_cash: float = config.CAPITAL_BASE_CAD,
                 fee_schedule: Optional[config.FeeSchedule] = None, seed: int = 7,
                 reject_prob: float = 0.0, enforce_venue_minimums: bool = True):
        super().__init__(fee_schedule or config.FEE_TABLES[config.DEFAULT_FEE_TABLE])
        self.feed = feed
        # Paper-soak realism: if the feed knows the venue's order minimums (KrakenFeed), enforce them.
        self.enforce_venue_minimums = enforce_venue_minimums and hasattr(feed, "min_qty")
        self.cash = float(starting_cash)
        self.positions: Dict[str, Position] = {}
        self.rng = random.Random(seed)
        self.reject_prob = reject_prob
        self.withdrawn_total = 0.0
        self.fill_count = 0

    # ---- market data
    def get_quote(self, symbol: str) -> Quote:
        return self.feed.quote(symbol)

    def min_qty(self, symbol: str) -> float:
        if not self.enforce_venue_minimums:
            return 0.0
        try:
            return float(self.feed.min_qty(symbol))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - network hiccup must not break paper trading
            asset = config.ASSETS.get(symbol)
            return asset.ordermin_fallback if asset else 0.0

    # ---- account
    def get_cash(self) -> float:
        return self.cash

    def get_positions(self) -> Dict[str, Position]:
        return {s: Position(p.symbol, p.qty, p.avg_cost) for s, p in self.positions.items() if p.qty > 1e-9}

    # ---- fee model
    def _commission(self, side: str, notional: float) -> float:
        flat = self.fees.commission_per_trade if side == "BUY" else self.fees.sell_commission_per_trade
        prop = notional * self.fees.commission_bps / 1e4
        c = flat + prop
        if c > 0:
            c = max(c, self.fees.min_commission)
        return round(c, 4)

    def _fx_fee(self, symbol: str, notional: float) -> float:
        asset = config.ASSETS.get(symbol)
        if asset is None or asset.currency == "CAD":
            return 0.0
        return round(notional * self.fees.fx_conversion_bps / 1e4, 4)

    def _slippage_bps(self, notional: float) -> float:
        base = self.rng.gauss(self.fees.slippage_bps_mean, self.fees.slippage_bps_std)
        # micro lots have no market impact; keep a tiny size term for completeness
        impact = 0.0005 * notional
        return max(0.0, base + impact)

    # ---- trading
    def submit_order(self, order: Order, quote: Optional[Quote] = None) -> Fill:
        if order.order_type != "MARKET":
            raise BrokerError("mock broker supports MARKET orders only")
        if self.reject_prob and self.rng.random() < self.reject_prob:
            raise BrokerError("simulated broker rejection")
        q = quote or self.get_quote(order.symbol)
        qty = self.round_qty(order.qty, order.symbol)
        if qty <= 0:
            raise BrokerError("qty rounds to zero for this broker's lot rules")
        if order.side == "BUY" and qty < self.min_qty(order.symbol):
            raise BrokerError(f"volume {qty} below venue minimum {self.min_qty(order.symbol)} for {order.symbol}")

        ref = q.ask if order.side == "BUY" else q.bid
        notional_est = qty * ref
        slip = self._slippage_bps(notional_est)
        price = ref * (1 + slip / 1e4) if order.side == "BUY" else ref * (1 - slip / 1e4)
        notional = qty * price
        # minimum notional applies to opening trades only; a position must always be closable
        if order.side == "BUY" and notional < self.fees.min_order_notional:
            raise BrokerError(f"notional {notional:.2f} below broker minimum {self.fees.min_order_notional:.2f}")
        commission = self._commission(order.side, notional)
        fees = self._fx_fee(order.symbol, notional)
        realized = 0.0

        if order.side == "BUY":
            total = notional + commission + fees
            if total > self.cash + 1e-9:
                raise BrokerError(f"insufficient cash: need {total:.2f}, have {self.cash:.2f} (no leverage)")
            pos = self.positions.get(order.symbol)
            if pos is None:
                self.positions[order.symbol] = Position(order.symbol, qty, price)
            else:
                new_qty = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / new_qty
                pos.qty = new_qty
            self.cash -= total
        else:
            pos = self.positions.get(order.symbol)
            if pos is None or pos.qty + 1e-9 < qty:
                raise BrokerError("cannot sell more than held (short selling disabled)")
            realized = (price - pos.avg_cost) * qty
            pos.qty -= qty
            if pos.qty <= 1e-9:
                del self.positions[order.symbol]
            self.cash += notional - commission - fees

        self.fill_count += 1
        return Fill(order.client_id, order.symbol, order.side, qty, price, commission, fees, slip, realized, ts=q.ts)

    def withdraw(self, amount: float, memo: str = "") -> bool:
        amount = round(amount, 2)
        if amount <= 0:
            return False
        if amount > self.cash + 1e-9:
            raise BrokerError(f"withdraw {amount:.2f} exceeds cash {self.cash:.2f}")
        self.cash -= amount
        self.withdrawn_total += amount
        return True
