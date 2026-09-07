"""Pure-Python technical indicators. No numpy: the universe is 6 symbols x a few hundred bars."""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def sma_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(high: float, low: float, prev_close: Optional[float]) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    trs = [true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, n)]
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def returns(closes: Sequence[float]) -> List[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]


def log_returns(closes: Sequence[float]) -> List[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def stdev(values: Sequence[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def realized_vol(closes: Sequence[float], period: int = 20, periods_per_year: int = 252) -> Optional[float]:
    lr = log_returns(closes)
    if len(lr) < period:
        return None
    s = stdev(lr[-period:])
    return None if s is None else s * math.sqrt(periods_per_year)


def momentum(closes: Sequence[float], lookback: int) -> Optional[float]:
    if len(closes) <= lookback or closes[-1 - lookback] <= 0:
        return None
    return closes[-1] / closes[-1 - lookback] - 1.0


def zscore(value: float, sample: Sequence[float]) -> Optional[float]:
    s = stdev(sample)
    if not s:
        return None
    m = sum(sample) / len(sample)
    return (value - m) / s


def rolling_vol_zscore(closes: Sequence[float], short: int = 20, long: int = 100) -> Optional[float]:
    """z-score of the current short-window realized vol against its own history over ``long`` bars."""
    lr = log_returns(closes)
    if len(lr) < long + short:
        return None
    window_vols: List[float] = []
    for end in range(short, len(lr) + 1):
        s = stdev(lr[end - short:end])
        if s is not None:
            window_vols.append(s)
    hist = window_vols[-long:-1]
    current = window_vols[-1]
    return zscore(current, hist)


def max_drawdown(equity: Sequence[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, e / peak - 1.0)
    return mdd


def bollinger(closes: Sequence[float], period: int = 20, k: float = 2.0):
    m = sma(closes, period)
    if m is None:
        return None
    s = stdev(closes[-period:]) or 0.0
    return m - k * s, m, m + k * s


def efficiency_ratio(closes: Sequence[float], period: int = 20) -> Optional[float]:
    """Kaufman efficiency ratio: net move / sum of absolute daily moves over ``period`` bars (0 = pure chop, 1 = straight line)."""
    if len(closes) < period + 1:
        return None
    window = closes[-period - 1:]
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path <= 0:
        return 0.0
    return abs(window[-1] - window[0]) / path


def basket_index(series_by_symbol: Sequence[Sequence[float]]) -> List[float]:
    """Equal-weight index of several close series, each normalised to 1.0 at the start of the common window."""
    n = min((len(s) for s in series_by_symbol), default=0)
    if n == 0:
        return []
    out: List[float] = []
    for i in range(-n, 0):
        vals = [s[i] / s[-n] for s in series_by_symbol if s[-n] > 0]
        out.append(sum(vals) / len(vals) if vals else 1.0)
    return out


def donchian_upper(highs: Sequence[float], period: int = 20, exclude_last: bool = True) -> Optional[float]:
    """Highest high of the previous ``period`` bars (excluding the current bar by default)."""
    window = highs[-period - 1:-1] if exclude_last else highs[-period:]
    if len(window) < period:
        return None
    return max(window)
