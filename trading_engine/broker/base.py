"""Broker abstraction shared by the mock and live implementations."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .. import config


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return 0.0 if self.mid <= 0 else (self.ask - self.bid) / self.mid * 1e4


@dataclass
class Order:
    symbol: str
    side: str                     # 'BUY' | 'SELL'
    qty: float                    # fractional allowed if broker supports it
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    reason: str = ""
    expected_edge_bps: float = 0.0
    client_id: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side {self.side}")
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if not self.client_id:
            self.client_id = f"{self.symbol}-{self.side}-{int(time.time()*1000)}"


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    commission: float
    fees: float                   # exchange/regulatory/fx fees other than commission
    slippage_bps: float
    realized_pnl: float           # gross realized PnL on the closed portion (SELL only)
    ts: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def total_cost(self) -> float:
        return self.commission + self.fees


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_cost) * self.qty


class BrokerError(Exception):
    pass


class Broker(abc.ABC):
    name: str = "base"

    def __init__(self, fee_schedule: config.FeeSchedule):
        self.fees = fee_schedule

    @property
    def supports_fractional(self) -> bool:
        return self.fees.supports_fractional

    # ---- market data
    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return {s: self.get_quote(s) for s in symbols}

    # ---- account
    @abc.abstractmethod
    def get_cash(self) -> float: ...

    @abc.abstractmethod
    def get_positions(self) -> Dict[str, Position]: ...

    def equity(self, quotes: Optional[Dict[str, Quote]] = None) -> float:
        positions = self.get_positions()
        if quotes is None:
            quotes = self.get_quotes(list(positions))
        mv = sum(p.qty * quotes[s].mid for s, p in positions.items() if s in quotes)
        return self.get_cash() + mv

    # ---- trading
    @abc.abstractmethod
    def submit_order(self, order: Order, quote: Optional[Quote] = None) -> Fill: ...

    @abc.abstractmethod
    def withdraw(self, amount: float, memo: str = "") -> bool:
        """Move realized profit out of trading capital. Returns True if executed at the broker."""

    # ---- cost model (shared): estimated one-way + round-trip cost in bps of notional
    def estimate_round_trip_cost_bps(self, symbol: str, notional: float, quote: Optional[Quote] = None) -> float:
        if notional <= 0:
            return float("inf")
        asset = config.ASSETS.get(symbol)
        spread_bps = quote.spread_bps if quote and quote.spread_bps > 0 else (
            asset.typical_spread_bps if asset else self.fees.spread_bps_default)
        # pay half-spread on each side + slippage on each side
        variable = spread_bps + 2 * self.fees.slippage_bps_mean + 2 * self.fees.commission_bps
        fixed = self.fees.commission_per_trade + self.fees.sell_commission_per_trade
        fixed_bps = fixed / notional * 1e4
        fx = 0.0 if (asset is None or asset.currency == "CAD") else 2 * self.fees.fx_conversion_bps
        return variable + fixed_bps + fx

    def round_qty(self, qty: float) -> float:
        if self.supports_fractional:
            return round(qty, 4)
        return float(int(qty))
