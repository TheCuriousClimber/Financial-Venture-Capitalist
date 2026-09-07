"""Kraken spot adapter for CAD pairs. Production live broker. stdlib only (urllib, hmac, hashlib).

Scope model
-----------
* The API key MUST be created with only "Query Funds" and "Create & Modify Orders" (and optionally
  "Query Open/Closed Orders & Trades"). Never grant "Withdraw Funds".
* This class has no withdrawal method and ``_private`` refuses every endpoint in FORBIDDEN_ENDPOINTS,
  so even a compromised strategy layer cannot move funds off-exchange through this code path.
* No order is sent unless ``enabled`` is True (LIVE_TRADING_ENABLED=true). When disabled, submit_order
  raises before touching the network.

Fees are percentage-based (tier-0 taker 0.40%, maker 0.25%); market orders are taker orders, so a $5
trade costs about two cents per side.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from .. import config
from .base import Broker, BrokerError, Fill, Order, Position, Quote

API_URL = "https://api.kraken.com"
API_VERSION = "0"

# Any private endpoint that can move value off the exchange or alter key/account settings.
FORBIDDEN_ENDPOINTS = frozenset({
    "Withdraw", "WithdrawInfo", "WithdrawAddresses", "WithdrawMethods", "WithdrawCancel", "WithdrawStatus",
    "WalletTransfer", "DepositAddresses", "DepositMethods", "DepositStatus", "CreateSubaccount",
    "AccountTransfer", "Earn/Allocate", "Earn/Deallocate", "Stake", "Unstake",
})
ALLOWED_ENDPOINTS = frozenset({
    "Balance", "BalanceEx", "TradeBalance", "OpenOrders", "ClosedOrders", "QueryOrders", "TradesHistory",
    "QueryTrades", "AddOrder", "CancelOrder", "CancelAll",
})

# Kraken asset code -> our base symbol; Kraken uses legacy X/Z prefixes for some assets.
KRAKEN_QUOTE_CAD = "ZCAD"


def kraken_signature(urlpath: str, data: Dict[str, Any], secret_b64: str) -> str:
    """API-Sign = base64(HMAC-SHA512(base64decode(secret), urlpath + SHA256(nonce + urlencode(data))))."""
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret_b64), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class NonceGenerator:
    """Strictly increasing millisecond nonce (Kraken rejects reused or decreasing nonces)."""

    def __init__(self) -> None:
        self._last = 0

    def __call__(self) -> int:
        n = int(time.time() * 1000)
        if n <= self._last:
            n = self._last + 1
        self._last = n
        return n


class KrakenBroker(Broker):
    name = "kraken"

    def __init__(self, api_key: Optional[str] = None, private_key: Optional[str] = None,
                 enabled: bool = config.LIVE_TRADING_ENABLED, fee_schedule: Optional[config.FeeSchedule] = None,
                 timeout: float = config.HTTP_TIMEOUT_SECONDS, opener: Optional[Callable] = None, fill_poll_seconds: float = 10.0,
                 cost_basis_store: Optional[Any] = None):
        super().__init__(fee_schedule or config.FEE_TABLES["kraken"])
        self.api_key = api_key if api_key is not None else config.KRAKEN_API_KEY
        self.private_key = private_key if private_key is not None else config.KRAKEN_PRIVATE_KEY
        self.enabled = enabled
        self.timeout = timeout
        self.fill_poll_seconds = fill_poll_seconds
        self._open = opener or urllib.request.urlopen          # injectable for tests
        self._nonce = NonceGenerator()
        self.pair_rules: Dict[str, Dict[str, Any]] = {}          # symbol -> {ordermin, lot_decimals, pair_decimals, costmin}
        self._store = cost_basis_store                           # object with get_state_json/set_state_json (the Ledger)
        self._cost_basis: Dict[str, Dict[str, float]] = {}
        if self._store is not None:
            self._cost_basis = self._store.get_state_json("kraken_cost_basis", {}) or {}
        self.symbols = [a.symbol for a in config.CRYPTO_WATCHLIST]
        self._by_pair = {a.exchange_pair: a for a in config.CRYPTO_WATCHLIST}
        self._by_base = {a.base_asset: a for a in config.CRYPTO_WATCHLIST}

    # ------------------------------------------------------------------ http
    def _http(self, url: str, data: Optional[bytes], headers: Dict[str, str]) -> Dict[str, Any]:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json", **headers},
                                     method="POST" if data is not None else "GET")
        try:
            with self._open(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise BrokerError(f"kraken HTTP {e.code}: {e.read().decode(errors='ignore')[:200]}") from e
        except urllib.error.URLError as e:
            raise BrokerError(f"kraken network error: {e.reason}") from e
        if payload.get("error"):
            raise BrokenApiError(payload["error"])
        return payload.get("result", {})

    def _public(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._http(f"{API_URL}/{API_VERSION}/public/{method}{qs}", None, {})

    def _private(self, method: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if method in FORBIDDEN_ENDPOINTS or method.split("/")[0] in FORBIDDEN_ENDPOINTS:
            raise BrokerError(f"endpoint {method} is forbidden by policy (no withdrawals / transfers)")
        if method not in ALLOWED_ENDPOINTS:
            raise BrokerError(f"endpoint {method} not in allow-list")
        if not self.api_key or not self.private_key:
            raise BrokerError("KRAKEN_API_KEY / KRAKEN_PRIVATE_KEY missing")
        body = dict(data or {})
        body["nonce"] = self._nonce()
        urlpath = f"/{API_VERSION}/private/{method}"
        headers = {
            "API-Key": self.api_key,
            "API-Sign": kraken_signature(urlpath, body, self.private_key),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self._http(f"{API_URL}{urlpath}", urllib.parse.urlencode(body).encode(), headers)

    # ------------------------------------------------------------ pair rules
    def load_pair_rules(self) -> Dict[str, Dict[str, Any]]:
        """Fetch ordermin / lot_decimals / costmin for the watchlist; fall back to config values."""
        for a in config.CRYPTO_WATCHLIST:
            self.pair_rules[a.symbol] = {"ordermin": a.ordermin_fallback, "lot_decimals": a.lot_decimals,
                                         "pair_decimals": 2, "costmin": 1.0, "source": "fallback"}
        try:
            res = self._public("AssetPairs", {"pair": ",".join(a.exchange_pair for a in config.CRYPTO_WATCHLIST)})
        except BrokerError:
            return self.pair_rules
        for _key, info in res.items():
            asset = self._by_pair.get(info.get("altname"))
            if asset is None:
                continue
            self.pair_rules[asset.symbol] = {
                "ordermin": float(info.get("ordermin", asset.ordermin_fallback)),
                "lot_decimals": int(info.get("lot_decimals", asset.lot_decimals)),
                "pair_decimals": int(info.get("pair_decimals", 2)),
                "costmin": float(info.get("costmin", 1.0) or 1.0),
                "source": "exchange",
            }
        return self.pair_rules

    def min_qty(self, symbol: str) -> float:
        rules = self.pair_rules.get(symbol)
        if rules is None:
            a = config.ASSETS.get(symbol)
            return a.ordermin_fallback if a else 0.0
        return float(rules["ordermin"])

    def round_qty(self, qty: float, symbol: Optional[str] = None) -> float:
        dec = int(self.pair_rules.get(symbol, {}).get("lot_decimals", 8)) if symbol else 8
        return float(f"{qty:.{dec}f}")

    # ------------------------------------------------------------ market data
    def get_quote(self, symbol: str) -> Quote:
        asset = config.ASSETS[symbol]
        res = self._public("Ticker", {"pair": asset.exchange_pair})
        tick = next(iter(res.values()))
        return Quote(symbol, bid=float(tick["b"][0]), ask=float(tick["a"][0]), last=float(tick["c"][0]), ts=time.time())

    def get_quotes(self, symbols):
        pairs = ",".join(config.ASSETS[s].exchange_pair for s in symbols)
        res = self._public("Ticker", {"pair": pairs})
        out: Dict[str, Quote] = {}
        for key, tick in res.items():
            asset = self._by_pair.get(key) or self._match_pair_key(key)
            if asset is not None:
                out[asset.symbol] = Quote(asset.symbol, float(tick["b"][0]), float(tick["a"][0]), float(tick["c"][0]), time.time())
        missing = [s for s in symbols if s not in out]
        if missing:
            raise BrokerError(f"ticker missing pairs {missing}: keys={list(res)}")
        return out

    def _match_pair_key(self, key: str):
        """Kraken returns legacy keys (XXBTZCAD) for some pairs; match on base asset + CAD quote."""
        for a in config.CRYPTO_WATCHLIST:
            if key in (a.exchange_pair, f"{a.base_asset}{KRAKEN_QUOTE_CAD}", f"{a.base_asset}CAD"):
                return a
        return None

    # ----------------------------------------------------------------- account
    def get_cash(self) -> float:
        bal = self._private("Balance")
        return float(bal.get(KRAKEN_QUOTE_CAD, bal.get("CAD", 0.0)))

    def get_positions(self) -> Dict[str, Position]:
        bal = self._private("Balance")
        out: Dict[str, Position] = {}
        for code, amount in bal.items():
            asset = self._by_base.get(code)
            if asset is None:
                continue
            qty = float(amount)
            if qty <= 0:
                continue
            basis = self._cost_basis.get(asset.symbol, {})
            out[asset.symbol] = Position(asset.symbol, qty, float(basis.get("avg_cost", 0.0)))
        return out

    # ----------------------------------------------------------------- trading
    def submit_order(self, order: Order, quote: Optional[Quote] = None) -> Fill:
        if not self.enabled:
            raise BrokerError("LIVE_TRADING_ENABLED is false; refusing to send a live order")
        asset = config.ASSETS.get(order.symbol)
        if asset is None or asset.asset_class != "crypto" or not asset.exchange_pair:
            raise BrokerError(f"{order.symbol} is not a Kraken CAD pair")
        if not self.pair_rules:
            self.load_pair_rules()
        qty = self.round_qty(order.qty, order.symbol)
        if qty < self.min_qty(order.symbol):
            raise BrokerError(f"volume {qty} below Kraken ordermin {self.min_qty(order.symbol)} for {order.symbol}")
        body: Dict[str, Any] = {
            "pair": asset.exchange_pair,
            "type": "buy" if order.side == "BUY" else "sell",
            "ordertype": "market" if order.order_type == "MARKET" else "limit",
            "volume": f"{qty:.{self.pair_rules.get(order.symbol, {}).get('lot_decimals', 8)}f}",
            "userref": abs(hash(order.client_id)) % 2_000_000_000,
        }
        if body["ordertype"] == "limit":
            if order.limit_price is None:
                raise BrokerError("limit order without limit_price")
            body["price"] = f"{order.limit_price:.{self.pair_rules.get(order.symbol, {}).get('pair_decimals', 2)}f}"
            body["oflags"] = "post"      # post-only: maker fee or rejected, never taker
        res = self._private("AddOrder", body)
        txids = res.get("txid") or []
        if not txids:
            raise BrokerError(f"AddOrder returned no txid: {json.dumps(res)[:200]}")
        txid = txids[0]
        info = self._await_fill(txid)
        vol_exec = float(info.get("vol_exec", 0.0))
        if vol_exec <= 0:
            raise BrokerError(f"order {txid} status={info.get('status')} with no execution")
        price = float(info.get("price") or 0.0)
        cost = float(info.get("cost") or (vol_exec * price))
        if price <= 0 and vol_exec > 0:
            price = cost / vol_exec
        fee = float(info.get("fee") or 0.0)
        ref = (quote.ask if order.side == "BUY" else quote.bid) if quote else price
        slippage_bps = abs(price - ref) / ref * 1e4 if ref else 0.0
        realized = self._update_cost_basis(order.symbol, order.side, vol_exec, price)
        return Fill(txid, order.symbol, order.side, vol_exec, price, fee, 0.0, slippage_bps, realized, ts=time.time())

    def _await_fill(self, txid: str) -> Dict[str, Any]:
        deadline = time.time() + self.fill_poll_seconds
        info: Dict[str, Any] = {}
        while True:
            res = self._private("QueryOrders", {"txid": txid, "trades": "true"})
            info = res.get(txid, {})
            if info.get("status") in ("closed", "canceled", "expired") or time.time() >= deadline:
                return info
            time.sleep(0.5)

    def _update_cost_basis(self, symbol: str, side: str, qty: float, price: float) -> float:
        basis = self._cost_basis.setdefault(symbol, {"qty": 0.0, "avg_cost": 0.0})
        realized = 0.0
        if side == "BUY":
            new_qty = basis["qty"] + qty
            basis["avg_cost"] = (basis["avg_cost"] * basis["qty"] + price * qty) / new_qty if new_qty > 0 else price
            basis["qty"] = new_qty
        else:
            realized = (price - basis["avg_cost"]) * qty
            basis["qty"] = max(0.0, basis["qty"] - qty)
            if basis["qty"] <= 1e-12:
                basis["avg_cost"] = 0.0
        if self._store is not None:
            self._store.set_state_json("kraken_cost_basis", self._cost_basis)
        return realized

    def withdraw(self, amount: float, memo: str = "") -> bool:
        """Deliberately not implemented: this adapter has no withdrawal capability. The ledger records
        the sweep as a pending manual transfer for the owner to execute in the Kraken UI."""
        return False


class BrokenApiError(BrokerError):
    """Kraken returned a non-empty ``error`` array (e.g. EOrder:Insufficient funds, EAPI:Invalid nonce)."""

    def __init__(self, errors):
        super().__init__("kraken API error: " + "; ".join(map(str, errors)))
        self.errors = list(errors)


LiveBroker = KrakenBroker
