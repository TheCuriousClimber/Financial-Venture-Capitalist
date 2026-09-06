"""Market data feeds.

* SyntheticFeed - seeded geometric-Brownian-motion bars with volatility regimes. Zero network,
  zero cost, deterministic. Used for dry runs and tests.
* YahooChartFeed - free daily bars via Yahoo Finance's public chart endpoint (stdlib urllib only).
  No API key, no LLM. Intended for paper trading against real prices; a live broker would
  normally supply quotes itself.
"""
from __future__ import annotations

import json
import math
import random
import time
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


class YahooChartFeed(Feed):
    """Daily bars from Yahoo Finance chart API. Free, keyless, zero-token. Cache per calendar day."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"

    def __init__(self, symbols: Optional[List[str]] = None, lookback: str = "1y", timeout: float = 10.0):
        self.symbols = list(symbols or config.SYMBOLS)
        self.lookback = lookback
        self.timeout = timeout
        self._hist: Dict[str, List[Bar]] = {}
        self._fetched_day: Dict[str, str] = {}

    def _fetch(self, symbol: str) -> List[Bar]:
        req = urllib.request.Request(
            self.URL.format(symbol=symbol, range=self.lookback),
            headers={"User-Agent": "Mozilla/5.0 (trading-engine; local daemon)"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
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
        bars = self.history(symbol)
        last = bars[-1].close
        half = last * config.ASSETS[symbol].typical_spread_bps / 1e4 / 2
        return Quote(symbol, bid=last - half, ask=last + half, last=last, ts=bars[-1].ts)


def make_feed(kind: str = config.DATA_FEED, **kwargs) -> Feed:
    if kind == "synthetic":
        return SyntheticFeed(**kwargs)
    if kind == "yahoo":
        return YahooChartFeed(**kwargs)
    raise ValueError(f"unknown DATA_FEED {kind}")
