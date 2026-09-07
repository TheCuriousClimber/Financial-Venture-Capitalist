"""Tests for the Kraken adapter, Kraken feed parsing, webhook dispatcher, paper-soak routing and validation."""
from __future__ import annotations

import _env  # noqa: F401  pins config before trading_engine is imported

import io
import json
import unittest
import urllib.error
import urllib.parse
from typing import Callable, Dict, List
from unittest import mock

from trading_engine import config
from trading_engine.broker.base import BrokerError, Order, Quote
from trading_engine.broker.live_broker import (ALLOWED_ENDPOINTS, FORBIDDEN_ENDPOINTS, KrakenBroker, NonceGenerator,
                                                kraken_signature)
from trading_engine.broker.mock_broker import MockBroker
from trading_engine.core.risk_manager import OrderIntent, RiskManager
from trading_engine.data.feed import KrakenFeed, SyntheticFeed
from trading_engine.ledger import Ledger
from trading_engine.notifications import WebhookNotifier


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(router: Callable[[str, Dict[str, str]], dict], calls: List[dict]):
    """Return an urlopen-compatible callable that records requests and routes by URL path."""
    def _open(req, timeout=None):
        body = req.data.decode() if req.data else ""
        form = dict(urllib.parse.parse_qsl(body))
        calls.append({"url": req.full_url, "method": req.get_method(), "form": form, "headers": dict(req.header_items())})
        payload = router(req.full_url, form)
        return FakeResponse(json.dumps(payload).encode())
    return _open


# ------------------------------------------------------------------- signature
class TestKrakenSignature(unittest.TestCase):
    def test_matches_documented_vector(self):
        secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
        data = {"nonce": "1616492376594", "ordertype": "limit", "pair": "XBTUSD", "price": 37500, "type": "buy", "volume": 1.25}
        sig = kraken_signature("/0/private/AddOrder", data, secret)
        self.assertEqual(sig, "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==")

    def test_signature_changes_with_path_and_nonce(self):
        secret = "a2V5a2V5a2V5a2V5"
        a = kraken_signature("/0/private/Balance", {"nonce": 1}, secret)
        b = kraken_signature("/0/private/Balance", {"nonce": 2}, secret)
        c = kraken_signature("/0/private/OpenOrders", {"nonce": 1}, secret)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_nonce_strictly_increasing(self):
        n = NonceGenerator()
        values = [n() for _ in range(1000)]
        self.assertTrue(all(b > a for a, b in zip(values, values[1:])))


# --------------------------------------------------------------------- broker
class TestKrakenBroker(unittest.TestCase):
    def setUp(self):
        self.calls: List[dict] = []
        self.orders: Dict[str, dict] = {}

        def router(url, form):
            path = url.split("kraken.com")[1].split("?")[0]
            if path.endswith("/public/AssetPairs"):
                return {"error": [], "result": {
                    "XXBTZCAD": {"altname": "XBTCAD", "ordermin": "0.00005", "lot_decimals": 8, "pair_decimals": 1, "costmin": "0.5"},
                    "SOLCAD": {"altname": "SOLCAD", "ordermin": "0.02", "lot_decimals": 8, "pair_decimals": 3, "costmin": "0.5"},
                }}
            if path.endswith("/public/Ticker"):
                return {"error": [], "result": {"SOLCAD": {"a": ["221.500", "1", "1.000"], "b": ["221.300", "1", "1.000"], "c": ["221.400", "0.1"]},
                                                 "XXBTZCAD": {"a": ["140010.0", "1", "1.000"], "b": ["139990.0", "1", "1.000"], "c": ["140000.0", "0.01"]}}}
            if path.endswith("/private/Balance"):
                return {"error": [], "result": {"ZCAD": "94.5000", "SOL": "0.02000000", "XXBT": "0.0000"}}
            if path.endswith("/private/AddOrder"):
                txid = f"TX{len(self.orders) + 1}"
                self.orders[txid] = {"status": "closed", "vol_exec": form["volume"], "price": "221.55" if form["type"] == "buy" else "230.00",
                                     "cost": str(float(form["volume"]) * (221.55 if form["type"] == "buy" else 230.0)), "fee": "0.0177"}
                return {"error": [], "result": {"txid": [txid], "descr": {"order": f"{form['type']} {form['volume']} {form['pair']} @ market"}}}
            if path.endswith("/private/QueryOrders"):
                return {"error": [], "result": {form["txid"]: self.orders[form["txid"]]}}
            if path.endswith("/private/Withdraw"):
                raise AssertionError("withdraw endpoint must never be reached")
            return {"error": ["EGeneral:Unknown method"], "result": {}}

        self.ledger = Ledger(":memory:")
        self.broker = KrakenBroker(api_key="key", private_key="a2V5a2V5a2V5a2V5", enabled=True,
                                   opener=fake_opener(router, self.calls), fill_poll_seconds=0, cost_basis_store=self.ledger)

    def test_forbidden_and_allowlist(self):
        for ep in ("Withdraw", "WithdrawInfo", "WalletTransfer", "Earn/Allocate", "DepositAddresses"):
            with self.assertRaises(BrokerError):
                self.broker._private(ep, {})
        with self.assertRaises(BrokerError):
            self.broker._private("SomeNewEndpoint", {})
        self.assertFalse(FORBIDDEN_ENDPOINTS & ALLOWED_ENDPOINTS)
        self.assertFalse(hasattr(self.broker, "withdraw_funds"))
        self.assertFalse(self.broker.withdraw(5.0))                       # never moves money
        self.assertEqual(self.calls, [])                                     # nothing hit the network

    def test_disabled_broker_never_sends(self):
        b = KrakenBroker(api_key="k", private_key="a2V5", enabled=False, opener=fake_opener(lambda u, f: {}, self.calls))
        with self.assertRaises(BrokerError):
            b.submit_order(Order("SOL/CAD", "BUY", 0.02))
        self.assertEqual(self.calls, [])

    def test_private_request_is_signed(self):
        cash = self.broker.get_cash()
        self.assertEqual(cash, 94.5)
        call = self.calls[-1]
        self.assertEqual(call["headers"]["Api-key"], "key")
        expected = kraken_signature("/0/private/Balance", {"nonce": call["form"]["nonce"]}, "a2V5a2V5a2V5a2V5")
        self.assertEqual(call["headers"]["Api-sign"], expected)

    def test_pair_rules_and_quotes(self):
        rules = self.broker.load_pair_rules()
        self.assertEqual(rules["SOL/CAD"]["ordermin"], 0.02)
        self.assertEqual(rules["SOL/CAD"]["source"], "exchange")
        self.assertEqual(rules["ETH/CAD"]["source"], "fallback")           # not returned -> fallback
        q = self.broker.get_quotes(["SOL/CAD", "BTC/CAD"])
        self.assertAlmostEqual(q["SOL/CAD"].ask, 221.5)
        self.assertAlmostEqual(q["BTC/CAD"].bid, 139990.0)                 # legacy XXBTZCAD key matched

    def test_order_round_trip_fees_and_cost_basis(self):
        self.broker.load_pair_rules()
        q = self.broker.get_quote("SOL/CAD")
        buy = self.broker.submit_order(Order("SOL/CAD", "BUY", 0.02), q)
        self.assertEqual(buy.qty, 0.02)
        self.assertAlmostEqual(buy.price, 221.55)
        self.assertAlmostEqual(buy.commission, 0.0177)
        self.assertLess(buy.commission / buy.notional * 1e4, 45)             # percentage fee, ~40 bps
        add = [c for c in self.calls if c["url"].endswith("/private/AddOrder")][0]
        self.assertEqual(add["form"]["ordertype"], "market")
        self.assertEqual(add["form"]["pair"], "SOLCAD")
        sell = self.broker.submit_order(Order("SOL/CAD", "SELL", 0.02), q)
        self.assertAlmostEqual(sell.realized_pnl, (230.0 - 221.55) * 0.02)
        self.assertEqual(self.ledger.get_state_json("kraken_cost_basis")["SOL/CAD"]["qty"], 0.0)

    def test_rejects_below_ordermin(self):
        self.broker.load_pair_rules()
        with self.assertRaises(BrokerError):
            self.broker.submit_order(Order("BTC/CAD", "BUY", 0.00001))

    def test_positions_from_balance(self):
        pos = self.broker.get_positions()
        self.assertIn("SOL/CAD", pos)
        self.assertNotIn("BTC/CAD", pos)


# ----------------------------------------------------------------------- feed
class TestKrakenFeed(unittest.TestCase):
    def test_parses_ohlc_drops_live_candle_and_reads_minimums(self):
        calls: List[dict] = []
        rows = [[86400 * i, "100", "101", "99", str(100 + i), "100", "5", 10] for i in range(5)]

        def router(url, form):
            if "OHLC" in url:
                return {"error": [], "result": {"SOLCAD": rows, "last": 86400 * 4}}
            if "Ticker" in url:
                return {"error": [], "result": {"SOLCAD": {"a": ["105.5", "1", "1"], "b": ["105.3", "1", "1"], "c": ["105.4", "1"]}}}
            if "AssetPairs" in url:
                return {"error": [], "result": {"SOLCAD": {"altname": "SOLCAD", "ordermin": "0.02", "lot_decimals": 8}}}
            return {"error": ["unknown"]}

        feed = KrakenFeed(symbols=["SOL/CAD"], opener=fake_opener(router, calls))
        hist = feed.history("SOL/CAD")
        self.assertEqual(len(hist), 4)                                       # in-progress candle removed
        self.assertEqual(hist[-1].close, 103.0)
        self.assertEqual(feed.next_bars()["SOL/CAD"].ts, 86400 * 3)
        q = feed.quote("SOL/CAD")
        self.assertAlmostEqual(q.spread_bps, (105.5 - 105.3) / 105.4 * 1e4, places=6)
        self.assertEqual(feed.min_qty("SOL/CAD"), 0.02)
        # history is cached: a second call within refresh window does not refetch
        n = len(calls)
        feed.history("SOL/CAD")
        self.assertEqual(len(calls), n)


# ------------------------------------------------------------------- webhook
class TestWebhookNotifier(unittest.TestCase):
    def capture(self, url, chat_id="", fail=False):
        calls: List[dict] = []

        def _open(req, timeout=None):
            if fail:
                raise urllib.error.URLError("boom")
            calls.append({"url": req.full_url, "body": json.loads(req.data.decode()), "ct": req.get_header("Content-type")})
            r = FakeResponse(b"ok")
            r.status = 204
            return r
        ledger = Ledger(":memory:")
        return WebhookNotifier(url=url, telegram_chat_id=chat_id, ledger=ledger, opener=_open), calls, ledger

    def test_discord_payload(self):
        n, calls, _ = self.capture("https://discord.com/api/webhooks/1/abc")
        self.assertTrue(n.notify_sweep(realized_profit=1.23, distributable=1.00, owner=0.90, reserve=0.10, owner_total=0.90,
                                       reserve_total=0.10, equity_after=100.0, credit_remaining=100.10,
                                       withdrawn_at_broker=False, cycle=7))
        body = calls[0]["body"]
        self.assertIn("content", body)
        self.assertIn("$0.10", body["content"])
        self.assertIn("$0.90", body["content"])
        self.assertIn("PENDING manual transfer", body["content"])
        self.assertEqual(calls[0]["ct"], "application/json")

    def test_telegram_payload(self):
        n, calls, _ = self.capture("https://api.telegram.org/botTOKEN/sendMessage", chat_id="42")
        n.notify_alert("safe mode", "entering safe mode")
        self.assertEqual(calls[0]["body"]["chat_id"], "42")
        self.assertIn("safe mode", calls[0]["body"]["text"])

    def test_generic_payload_carries_data(self):
        n, calls, _ = self.capture("https://example.com/hook")
        n.send("t", "x", {"k": 1})
        self.assertEqual(calls[0]["body"]["data"], {"k": 1})
        self.assertEqual(calls[0]["body"]["title"], "t")

    def test_disabled_and_failure_are_silent(self):
        n = WebhookNotifier(url="")
        self.assertFalse(n.send("t", "x"))
        n, calls, ledger = self.capture("https://example.com/hook", fail=True)
        self.assertFalse(n.send("t", "x"))
        self.assertEqual(n.failed, 1)
        self.assertTrue(ledger.events("webhook_failed"))


# ------------------------------------------------------------- paper routing
class TestPaperSoakAndGates(unittest.TestCase):
    def test_paper_live_feed_forces_mock_broker(self):
        from trading_engine.daemon import build
        with mock.patch.object(config, "ASSET_UNIVERSE", "crypto"):
            d = build(broker_kind="kraken", feed_kind="synthetic", ledger_path=":memory:", paper_live_feed=True,
                      live_enabled=True)
        self.assertIsInstance(d.broker, MockBroker)
        self.assertEqual(d.broker.fees.broker, "kraken_paper")
        self.assertEqual(d.broker.fees.commission_bps, config.PAPER_FEE_BPS)
        self.assertTrue(d.ledger.events("paper_override"))

    def test_paper_fee_is_pennies_on_five_dollars(self):
        feed = SyntheticFeed(symbols=["SOL/CAD"], seed=1, history_bars=5)
        b = MockBroker(feed, fee_schedule=config.FEE_TABLES["kraken_paper"])
        q = b.get_quote("SOL/CAD")
        f = b.submit_order(Order("SOL/CAD", "BUY", 5.0 / q.ask), q)
        self.assertAlmostEqual(f.commission, 5.0 * config.PAPER_FEE_BPS / 1e4, places=3)
        self.assertLess(b.estimate_round_trip_cost_bps("SOL/CAD", 5.0, q), config.RISK.max_round_trip_cost_bps_crypto)

    def test_mock_enforces_venue_minimum_from_feed(self):
        feed = SyntheticFeed(symbols=["BTC/CAD"], seed=1, history_bars=5)
        b = MockBroker(feed, fee_schedule=config.FEE_TABLES["kraken_paper"])
        self.assertEqual(b.min_qty("BTC/CAD"), 0.00005)
        with self.assertRaises(BrokerError):
            b.submit_order(Order("BTC/CAD", "BUY", 0.00001))

    def test_risk_manager_rejects_below_min_qty_and_uses_crypto_cost_cap(self):
        rm = RiskManager()
        rm.roll_day(0, 100.0)
        q = Quote("BTC/CAD", 139990.0, 140010.0, 140000.0)
        d = rm.evaluate(OrderIntent("BTC/CAD", "BUY", 5.0, 400.0, "t"), q, 100.0, 100.0, {}, 90.0, 1, True, min_qty=0.00005)
        self.assertFalse(d.approved)
        self.assertIn("venue minimum", d.reason)
        q2 = Quote("SOL/CAD", 221.3, 221.5, 221.4)
        ok = rm.evaluate(OrderIntent("SOL/CAD", "BUY", 5.0, 400.0, "t"), q2, 100.0, 100.0, {}, 90.0, 1, True, min_qty=0.02)
        self.assertTrue(ok.approved, ok.reason)                               # 90 bps < 120 bps crypto cap
        too_pricey = rm.evaluate(OrderIntent("SOL/CAD", "BUY", 5.0, 400.0, "t"), q2, 100.0, 100.0, {}, 130.0, 1, True)
        self.assertFalse(too_pricey.approved)
        etf = rm.evaluate(OrderIntent("XIU.TO", "BUY", 5.0, 400.0, "t"), Quote("XIU.TO", 39.9, 40.1, 40.0), 100.0, 100.0, {}, 90.0, 1, True)
        self.assertFalse(etf.approved)                                        # 90 bps > 60 bps equity cap

    def test_config_validate_catches_live_without_keys(self):
        with mock.patch.multiple(config, BROKER="kraken", PAPER_LIVE_FEED=False, ASSET_UNIVERSE="crypto",
                                 DATA_FEED="kraken_live", KRAKEN_API_KEY="", KRAKEN_PRIVATE_KEY="", LIVE_TRADING_ENABLED=False):
            problems = config.validate()
        self.assertTrue(any("KRAKEN_API_KEY" in p for p in problems))
        self.assertTrue(any("LIVE_TRADING_ENABLED" in p for p in problems))
        with mock.patch.multiple(config, BROKER="kraken", PAPER_LIVE_FEED=True, ASSET_UNIVERSE="crypto", DATA_FEED="kraken_live"):
            self.assertEqual(config.validate(), [])
        with mock.patch.multiple(config, WEBHOOK_URL="https://api.telegram.org/botX/sendMessage", TELEGRAM_CHAT_ID=""):
            self.assertTrue(any("TELEGRAM_CHAT_ID" in p for p in config.validate()))


if __name__ == "__main__":
    unittest.main()
