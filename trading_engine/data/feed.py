"""Market data feeds.

* SyntheticFeed - seeded geometric-Brownian-motion bars with volatility regimes. Zero network,
  zero cost, deterministic. Used for dry runs and tests.
* YahooChartFeed - free daily bars via Yahoo Finance's public chart endpoint (stdlib urllib only).
  No API key, no LLM. Intended for paper trading against real prices; a live broker would
  normally supply quotes itself.
* KrakenFeed - Kraken public OHLC (daily) + Ticker for CAD pairs. Keyless, zero-token. Also exposes the
  exchange's per-pair order minimums so paper mode rejects the same orders the live venue would.
"""
from __future__ import annotations

import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from .. import config
from ..broker.base import Quote

TRADING_DAYS = 252


@dataclass
class Bar:
    symbol: str
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Feed:
    symbols: List[str]

    def history(self, symbol: str) -> List[Bar]:
        raise NotImplementedError

    def next_bars(self) -> Dict[str, Bar]:
        """Advance one bar for every symbol (synthetic) or fetch the latest completed bar (live)."""
        raise NotImplementedError

    def quote(self, symbol: str) -> Quote:
        raise NotImplementedError


class SyntheticFeed(Feed):
    """GBM with occasional volatility-regime spikes so the regime detector and stops get exercised."""

    def __init__(self, symbols: Optional[List[str]] = None, seed: int = config.SYNTHETIC_SEED,
                 history_bars: int = 200, start_ts: Optional[float] = None,
                 regime_prob: float = 0.01, regime_vol_mult: float = 3.0, regime_len: int = 8):
        self.symbols = list(symbols or config.SYMBOLS)
        self.rng = random.Random(seed)
        self.bar_seconds = config.BAR_SECONDS
        self.ts = start_ts if start_ts is not None else time.time() - history_bars * self.bar_seconds
        self._hist: Dict[str, List[Bar]] = {s: [] for s in self.symbols}
        self._last_close: Dict[str, float] = {}
        self._regime_left: Dict[str, int] = {s: 0 for s in self.symbols}
        self.regime_prob = regime_prob
        self.regime_vol_mult = regime_vol_mult
        self.regime_len = regime_len
        for s in self.symbols:
            a = config.ASSETS[s]
            self._last_close[s] = a.ref_price
        for _ in range(history_bars):
            self.next_bars()

    def _step(self, symbol: str) -> Bar:
        a = config.ASSETS[symbol]
        dt = 1.0 / TRADING_DAYS
        vol = a.annual_vol
        if self._regime_left[symbol] > 0:
            vol *= self.regime_vol_mult
            self._regime_left[symbol] -= 1
        elif self.rng.random() < self.regime_prob:
            self._regime_left[symbol] = self.regime_len
        prev = self._last_close[symbol]
        z = self.rng.gauss(0.0, 1.0)
        close = prev * math.exp((a.annual_drift - 0.5 * vol * vol) * dt + vol * math.sqrt(dt) * z)
        # intrabar range roughly proportional to daily vol
        rng_frac = abs(self.rng.gauss(0.0, 1.0)) * vol * math.sqrt(dt)
        o = prev * (1 + self.rng.gauss(0.0, 0.2) * vol * math.sqrt(dt))
        hi = max(o, close) * (1 + rng_frac / 2)
        lo = min(o, close) * (1 - rng_frac / 2)
        vol_shares = max(1000.0, self.rng.gauss(2e6, 5e5))
        self._last_close[symbol] = close
        bar = Bar(symbol, self.ts, o, hi, lo, close, vol_shares)
        self._hist[symbol].append(bar)
        return bar

    def next_bars(self) -> Dict[str, Bar]:
        self.ts += self.bar_seconds
        return {s: self._step(s) for s in self.symbols}

    def history(self, symbol: str) -> List[Bar]:
        return list(self._hist[symbol])

    def quote(self, symbol: str) -> Quote:
        last = self._last_close[symbol]
        half = last * config.ASSETS[symbol].typical_spread_bps / 1e4 / 2
        return Quote(symbol, bid=last - half, ask=last + half, last=last, ts=self.ts)

    def min_qty(self, symbol: str) -> float:
        """Mirror the venue's published order minimum so offline crypto runs reject what Kraken would."""
        return config.ASSETS[symbol].ordermin_fallback


class YahooChartFeed(Feed):
    """Daily bars from Yahoo Finance chart API. Free, keyless, zero-token. Cache per calendar day."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"

    def __init__(self, symbols: Optional[List[str]] = None, lookback: str = "1y", timeout: float = config.HTTP_TIMEOUT_SECONDS,
                 symbol_map: Optional[Dict[str, str]] = None, opener=None, drop_in_progress: bool = False):
        self.symbols = list(symbols or config.SYMBOLS)
        self.lookback = lookback
        self.timeout = timeout
        self.symbol_map = symbol_map or {}          # our symbol -> Yahoo ticker (e.g. BTC/CAD -> BTC-CAD)
        self._open = opener or urllib.request.urlopen
        self.drop_in_progress = drop_in_progress     # crypto trades 24/7: today's candle is never complete
        self._hist: Dict[str, List[Bar]] = {}
        self._fetched_day: Dict[str, str] = {}
        self._latest: Dict[str, Bar] = {}            # newest bar incl. the in-progress one (for quotes)

    def _fetch(self, symbol: str) -> List[Bar]:
        req = urllib.request.Request(
            self.URL.format(symbol=urllib.parse.quote(self.symbol_map.get(symbol, symbol)), range=self.lookback),
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        )
        with self._open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        ts = result["timestamp"]
        q = result["indicators"]["quote"][0]
        bars: List[Bar] = []
        for i, t in enumerate(ts):
            c = q["close"][i]
            if c is None:
                continue
            bars.append(Bar(symbol, float(t), q["open"][i] or c, q["high"][i] or c, q["low"][i] or c, c,
                            float(q["volume"][i] or 0)))
        if bars:
            self._latest[symbol] = bars[-1]
        if self.drop_in_progress and len(bars) > 1 and time.time() - bars[-1].ts < 86400:
            bars = bars[:-1]
        return bars

    def _refresh(self, symbol: str) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self._fetched_day.get(symbol) == day and symbol in self._hist:
            return
        self._hist[symbol] = self._fetch(symbol)
        self._fetched_day[symbol] = day

    def history(self, symbol: str) -> List[Bar]:
        self._refresh(symbol)
        return list(self._hist[symbol])

    def next_bars(self) -> Dict[str, Bar]:
        out: Dict[str, Bar] = {}
        for s in self.symbols:
            self._refresh(s)
            if self._hist[s]:
                out[s] = self._hist[s][-1]
        return out

    def quote(self, symbol: str) -> Quote:
        self.history(symbol)                          # ensures a fresh fetch for today
        latest = self._latest[symbol]
        last = latest.close
        half = last * config.ASSETS[symbol].typical_spread_bps / 1e4 / 2
        return Quote(symbol, bid=last - half, ask=last + half, last=last, ts=latest.ts)


class KrakenFeed(Feed):
    """Daily bars + live quotes from Kraken's public REST API. Only completed daily candles feed the strategy;
    the in-progress candle is excluded so signals are not recomputed on a moving bar."""

    API = "https://api.kraken.com/0/public"

    def __init__(self, symbols: Optional[List[str]] = None, timeout: float = config.HTTP_TIMEOUT_SECONDS, opener=None,
                 refresh_seconds: int = 300, allow_yahoo_fallback: bool = True, on_source_change=None):
        self.symbols = list(symbols or [a.symbol for a in config.CRYPTO_WATCHLIST])
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.refresh_seconds = refresh_seconds
        # Fallback: if Kraken's public API is unreachable (firewall / Cloudflare), pull candles from Yahoo
        # (BTC-CAD, ETH-CAD, SOL-CAD ...). Kraken fee tables and order minimums still apply via config.
        self.allow_yahoo_fallback = allow_yahoo_fallback
        self.source = "kraken"
        self.on_source_change = on_source_change
        self._yahoo = YahooChartFeed(symbols=self.symbols, symbol_map={s: s.replace("/", "-") for s in self.symbols},
                                     timeout=timeout, opener=self._open, drop_in_progress=True)
        self._hist: Dict[str, List[Bar]] = {}
        self._hist_ts: Dict[str, float] = {}
        self._quotes: Dict[str, Quote] = {}
        self._quotes_ts: float = 0.0
        self.pair_rules: Dict[str, Dict[str, float]] = {}
        self.bar_seconds = config.BAR_SECONDS

    # ---- http
    NET_ERRORS = (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError, StopIteration)

    def _get(self, method: str, params: Dict[str, str]) -> Dict:
        url = f"{self.API}/{method}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        with self._open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError("kraken: " + "; ".join(payload["error"]))
        return payload["result"]

    def _set_source(self, source: str, reason: str = "") -> None:
        if source != self.source:
            self.source = source
            if self.on_source_change:
                self.on_source_change(source, reason)

    @staticmethod
    def _asset(symbol: str) -> config.Asset:
        return config.ASSETS[symbol]

    def _match(self, key: str) -> Optional[config.Asset]:
        for a in config.CRYPTO_WATCHLIST:
            if key in (a.exchange_pair, f"{a.base_asset}ZCAD", f"{a.base_asset}CAD"):
                return a
        return None

    # ---- pair rules (order minimums)
    def load_pair_rules(self) -> Dict[str, Dict[str, float]]:
        for s in self.symbols:
            a = self._asset(s)
            self.pair_rules[s] = {"ordermin": a.ordermin_fallback, "lot_decimals": a.lot_decimals, "costmin": 1.0}
        try:
            res = self._get("AssetPairs", {"pair": ",".join(self._asset(s).exchange_pair for s in self.symbols)})
        except (RuntimeError, urllib.error.URLError, OSError, KeyError, ValueError):
            return self.pair_rules
        for _k, info in res.items():
            a = self._match(info.get("altname", ""))
            if a and a.symbol in self.pair_rules:
                self.pair_rules[a.symbol] = {"ordermin": float(info.get("ordermin", a.ordermin_fallback)),
                                             "lot_decimals": int(info.get("lot_decimals", a.lot_decimals)),
                                             "costmin": float(info.get("costmin", 1.0) or 1.0)}
        return self.pair_rules

    def min_qty(self, symbol: str) -> float:
        if not self.pair_rules:
            self.load_pair_rules()
        return float(self.pair_rules.get(symbol, {}).get("ordermin", self._asset(symbol).ordermin_fallback))

    # ---- bars
    @staticmethod
    def parse_ohlc(symbol: str, rows: List[List]) -> List[Bar]:
        bars: List[Bar] = []
        for r in rows:
            # [time, open, high, low, close, vwap, volume, count]
            bars.append(Bar(symbol, float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])))
        return bars

    def _refresh_history(self, symbol: str) -> None:
        now = time.time()
        if symbol in self._hist and now - self._hist_ts.get(symbol, 0) < self.refresh_seconds:
            return
        try:
            res = self._get("OHLC", {"pair": self._asset(symbol).exchange_pair, "interval": "1440"})
            rows = next(v for k, v in res.items() if k != "last")
            bars = self.parse_ohlc(symbol, rows)
            self._hist[symbol] = bars[:-1] if len(bars) > 1 else bars     # drop the in-progress candle
            self._set_source("kraken", "kraken reachable")
        except self.NET_ERRORS as e:
            if not self.allow_yahoo_fallback:
                raise
            self._hist[symbol] = self._yahoo.history(symbol)              # raises if Yahoo is down too
            self._set_source("yahoo", f"kraken unreachable: {e}")
        self._hist_ts[symbol] = now

    def history(self, symbol: str) -> List[Bar]:
        self._refresh_history(symbol)
        return list(self._hist[symbol])

    def next_bars(self) -> Dict[str, Bar]:
        out: Dict[str, Bar] = {}
        for s in self.symbols:
            self._refresh_history(s)
            if self._hist[s]:
                out[s] = self._hist[s][-1]
        return out

    # ---- quotes
    def _refresh_quotes(self) -> None:
        pairs = ",".join(self._asset(s).exchange_pair for s in self.symbols)
        now = time.time()
        try:
            res = self._get("Ticker", {"pair": pairs})
            for key, t in res.items():
                a = self._match(key)
                if a is not None:
                    self._quotes[a.symbol] = Quote(a.symbol, float(t["b"][0]), float(t["a"][0]), float(t["c"][0]), now)
            self._set_source("kraken", "kraken reachable")
        except self.NET_ERRORS as e:
            if not self.allow_yahoo_fallback:
                raise
            # Yahoo has no order book: quote = last price +/- half the pair's typical Kraken spread
            for s in self.symbols:
                self._quotes[s] = self._yahoo.quote(s)
            self._set_source("yahoo", f"kraken unreachable: {e}")
        self._quotes_ts = now

    def quote(self, symbol: str) -> Quote:
        if symbol not in self._quotes or time.time() - self._quotes_ts > 1.0:
            self._refresh_quotes()
        return self._quotes[symbol]


def make_feed(kind: str = config.DATA_FEED, **kwargs) -> Feed:
    if kind == "synthetic":
        return SyntheticFeed(**kwargs)
    if kind == "yahoo":
        return YahooChartFeed(**kwargs)
    if kind == "kraken_live":
        return KrakenFeed(**kwargs)
    raise ValueError(f"unknown DATA_FEED {kind}")
