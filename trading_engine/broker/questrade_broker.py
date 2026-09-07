"""Questrade adapter (reference only). Fixed sell commissions make it non-viable at a $5 trade size;
kept for read-only account queries. The production live adapter is live_broker.KrakenBroker.

Safety model:
  * Nothing is sent unless LIVE_TRADING_ENABLED=true AND credentials are present.
  * When disabled, submit_order() raises BrokerError so the daemon can never trade by accident.
  * Questrade's retail API is read-only for orders unless the account is enrolled in their
    order-placement program; ``submit_order`` therefore calls the documented endpoint and surfaces
    any HTTP error verbatim instead of guessing.

Uses urllib only (no third-party dependency). Token refresh follows Questrade's OAuth2 flow:
POST https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token=...
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

from .. import config
from .base import Broker, BrokerError, Fill, Order, Position, Quote

LOGIN_URL = "https://login.questrade.com/oauth2/token"


class QuestradeBroker(Broker):
    name = "questrade"

    def __init__(self, refresh_token: Optional[str] = None, account_id: Optional[str] = None,
                 fee_schedule: Optional[config.FeeSchedule] = None, enabled: bool = config.LIVE_TRADING_ENABLED,
                 timeout: float = 15.0):
        super().__init__(fee_schedule or config.FEE_TABLES["questrade"])
        self.refresh_token = refresh_token or config.env("QUESTRADE_REFRESH_TOKEN")
        self.account_id = account_id or config.env("QUESTRADE_ACCOUNT_ID")
        self.enabled = enabled
        self.timeout = timeout
        self.api_server: Optional[str] = None
        self.access_token: Optional[str] = None
        self.token_expiry: float = 0.0
        self._symbol_ids: Dict[str, int] = {}

    # ---------------------------------------------------------------- auth/http
    def _authenticate(self) -> None:
        if not self.refresh_token:
            raise BrokerError("QUESTRADE_REFRESH_TOKEN missing")
        qs = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        req = urllib.request.Request(f"{LOGIN_URL}?{qs}", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise BrokerError(f"auth failed: HTTP {e.code} {e.read().decode(errors='ignore')[:200]}") from e
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)   # rotates on every refresh
        self.api_server = data["api_server"].rstrip("/")
        self.token_expiry = time.time() + float(data.get("expires_in", 1800)) - 60

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if not self.access_token or time.time() >= self.token_expiry:
            self._authenticate()
        url = f"{self.api_server}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method, headers={
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise BrokerError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}") from e
        except urllib.error.URLError as e:
            raise BrokerError(f"{method} {path} -> network error: {e.reason}") from e

    # ------------------------------------------------------------- market data
    def _symbol_id(self, symbol: str) -> int:
        if symbol in self._symbol_ids:
            return self._symbol_ids[symbol]
        base = symbol.replace(".TO", "")
        data = self._request("GET", f"/v1/symbols/search?prefix={urllib.parse.quote(base)}")
        for s in data.get("symbols", []):
            if s.get("symbol") == base and s.get("listingExchange") in ("TSX", "TSXV", "NEO"):
                self._symbol_ids[symbol] = int(s["symbolId"])
                return self._symbol_ids[symbol]
        raise BrokerError(f"symbol {symbol} not found on TSX")

    def get_quote(self, symbol: str) -> Quote:
        sid = self._symbol_id(symbol)
        data = self._request("GET", f"/v1/markets/quotes/{sid}")
        q = data["quotes"][0]
        last = float(q.get("lastTradePrice") or q.get("bidPrice") or 0)
        bid = float(q.get("bidPrice") or last)
        ask = float(q.get("askPrice") or last)
        return Quote(symbol, bid, ask, last, ts=time.time())

    # ----------------------------------------------------------------- account
    def get_cash(self) -> float:
        data = self._request("GET", f"/v1/accounts/{self.account_id}/balances")
        for b in data.get("perCurrencyBalances", []):
            if b.get("currency") == "CAD":
                return float(b.get("cash", 0.0))
        raise BrokerError("no CAD balance found")

    def get_positions(self) -> Dict[str, Position]:
        data = self._request("GET", f"/v1/accounts/{self.account_id}/positions")
        out: Dict[str, Position] = {}
        for p in data.get("positions", []):
            sym = p["symbol"] + ".TO"
            qty = float(p.get("openQuantity", 0))
            if qty > 0:
                out[sym] = Position(sym, qty, float(p.get("averageEntryPrice", 0)))
        return out

    # ----------------------------------------------------------------- trading
    def submit_order(self, order: Order, quote: Optional[Quote] = None) -> Fill:
        if not self.enabled:
            raise BrokerError("LIVE_TRADING_ENABLED is false; refusing to send a live order")
        if not self.account_id:
            raise BrokerError("QUESTRADE_ACCOUNT_ID missing")
        qty = self.round_qty(order.qty)
        if qty <= 0:
            raise BrokerError("live broker requires whole shares; qty rounds to zero")
        body = {
            "accountNumber": self.account_id,
            "symbolId": self._symbol_id(order.symbol),
            "quantity": int(qty),
            "icebergQuantity": None,
            "limitPrice": order.limit_price,
            "isAllOrNone": False,
            "isAnonymous": False,
            "orderType": "Market" if order.order_type == "MARKET" else "Limit",
            "timeInForce": "Day",
            "action": "Buy" if order.side == "BUY" else "Sell",
            "primaryRoute": "AUTO",
            "secondaryRoute": "AUTO",
        }
        data = self._request("POST", f"/v1/accounts/{self.account_id}/orders", body)
        orders = data.get("orders") or []
        if not orders:
            raise BrokerError(f"order response had no orders: {json.dumps(data)[:300]}")
        o = orders[0]
        if o.get("state") not in ("Executed", "Partial", "Accepted", "Queued"):
            raise BrokerError(f"order state {o.get('state')}: {o.get('rejectionReason')}")
        filled_qty = float(o.get("filledQuantity") or qty)
        price = float(o.get("avgExecPrice") or (quote.ask if (quote and order.side == "BUY") else (quote.bid if quote else 0)))
        commission = float(o.get("commissionCharged") or self._expected_commission(order.side, filled_qty * price))
        return Fill(str(o.get("id", order.client_id)), order.symbol, order.side, filled_qty, price,
                    commission, 0.0, 0.0, 0.0)  # realized PnL reconciled by the daemon from ledger avg cost

    def _expected_commission(self, side: str, notional: float) -> float:
        flat = self.fees.commission_per_trade if side == "BUY" else self.fees.sell_commission_per_trade
        c = flat + notional * self.fees.commission_bps / 1e4
        return max(c, self.fees.min_commission) if c > 0 else 0.0

    def withdraw(self, amount: float, memo: str = "") -> bool:
        # Brokerage APIs do not expose EFT withdrawals; the ledger records the pending transfer
        # and the owner executes it. Returning False tells the daemon the cash is still at the broker.
        return False
