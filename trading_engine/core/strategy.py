"""Rule-based trend + pullback strategy on daily bars with ATR trailing stops.

Entry (long only):  close > SMA(slow) and close > SMA(fast)  (uptrend)
                    momentum(lookback) > 0
                    rsi_entry_min <= RSI <= rsi_entry_max      (not chasing an overbought bar)
Exit:               close < SMA(slow)  or  RSI > rsi_exit  or  close <= trailing stop
Sizing:             vol-targeted fraction of the 5% cap, never above the cap.
Regime detection:   realized-vol z-score above ``regime_vol_z`` flags an anomaly for the bridge.

Everything here is deterministic and free. No network, no LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .. import config
from ..broker.base import Position
from ..data.feed import Bar
from . import indicators as ind


@dataclass
class Signal:
    symbol: str
    action: str                  # 'ENTER_LONG' | 'EXIT'
    expected_edge_bps: float
    target_notional: float
    stop_price: Optional[float]
    reason: str
    score: float = 0.0


@dataclass
class RegimeReport:
    anomalies: Dict[str, float] = field(default_factory=dict)   # symbol -> vol z-score

    @property
    def shifted(self) -> bool:
        return bool(self.anomalies)


class TrendPullbackStrategy:
    def __init__(self, params: Optional[Dict[str, float]] = None, limits: config.RiskLimits = config.RISK):
        self.p: Dict[str, float] = dict(config.STRATEGY_PARAMS)
        if params:
            self.update_params(params)
        self.limits = limits
        self.trailing_stops: Dict[str, float] = {}

    # ------------------------------------------------------------ parameters
    def update_params(self, overrides: Dict[str, float]) -> Dict[str, float]:
        """Apply overrides clamped to STRATEGY_PARAM_BOUNDS. Unknown keys are ignored. Returns applied."""
        applied: Dict[str, float] = {}
        for k, v in overrides.items():
            if k not in config.STRATEGY_PARAM_BOUNDS:
                continue
            lo, hi = config.STRATEGY_PARAM_BOUNDS[k]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            clamped = max(lo, min(hi, fv))
            if isinstance(lo, int) and isinstance(hi, int):
                clamped = int(round(clamped))
            self.p[k] = clamped
            applied[k] = clamped
        if self.p["fast_sma"] >= self.p["slow_sma"]:
            self.p["fast_sma"] = max(config.STRATEGY_PARAM_BOUNDS["fast_sma"][0], int(self.p["slow_sma"]) // 2)
        return applied

    @property
    def warmup_bars(self) -> int:
        return int(max(self.p["slow_sma"], self.p["momentum_lookback"], self.p["atr_period"] + 1,
                       self.p["rsi_period"] + 1)) + 1

    # ------------------------------------------------------------ analytics
    def features(self, bars: Sequence[Bar]) -> Optional[Dict[str, float]]:
        if len(bars) < self.warmup_bars:
            return None
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        fast = ind.sma(closes, int(self.p["fast_sma"]))
        slow = ind.sma(closes, int(self.p["slow_sma"]))
        r = ind.rsi(closes, int(self.p["rsi_period"]))
        a = ind.atr(highs, lows, closes, int(self.p["atr_period"]))
        mom = ind.momentum(closes, int(self.p["momentum_lookback"]))
        vol = ind.realized_vol(closes, 20)
        if None in (fast, slow, r, a, mom, vol):
            return None
        return {"close": closes[-1], "fast": fast, "slow": slow, "rsi": r, "atr": a, "mom": mom, "vol": vol}

    def _size_notional(self, cap: float, vol: float) -> float:
        target = float(self.p["vol_target_annual"])
        scale = 1.0 if vol <= 0 else min(1.0, target / vol)
        return max(0.0, cap * scale)

    def _edge_bps(self, mom: float) -> float:
        return max(0.0, mom * 1e4 * float(self.p["edge_capture"]))

    # ------------------------------------------------------------ signals
    def generate(self, history: Dict[str, List[Bar]], positions: Dict[str, Position],
                 equity: float, position_cap: float) -> List[Signal]:
        signals: List[Signal] = []
        candidates: List[Signal] = []
        for symbol, bars in history.items():
            f = self.features(bars)
            if f is None:
                continue
            held = positions.get(symbol)
            close = f["close"]
            if held is not None:
                stop = self.trailing_stops.get(symbol)
                new_stop = close - float(self.p["atr_stop_mult"]) * f["atr"]
                if stop is None or new_stop > stop:
                    self.trailing_stops[symbol] = new_stop
                    stop = new_stop
                if close <= stop:
                    signals.append(Signal(symbol, "EXIT", 0.0, 0.0, stop, "trailing stop"))
                elif close < f["slow"]:
                    signals.append(Signal(symbol, "EXIT", 0.0, 0.0, stop, "trend break (close < slow SMA)"))
                elif f["rsi"] > float(self.p["rsi_exit"]):
                    signals.append(Signal(symbol, "EXIT", 0.0, 0.0, stop, f"overbought RSI {f['rsi']:.0f}"))
                continue
            # entry evaluation
            if close > f["slow"] and close > f["fast"] and f["mom"] > 0 \
                    and float(self.p["rsi_entry_min"]) <= f["rsi"] <= float(self.p["rsi_entry_max"]):
                notional = self._size_notional(position_cap, f["vol"])
                edge = self._edge_bps(f["mom"])
                stop = close - float(self.p["atr_stop_mult"]) * f["atr"]
                candidates.append(Signal(symbol, "ENTER_LONG", edge, notional, stop,
                                         f"trend+pullback mom={f['mom']:.2%} rsi={f['rsi']:.0f}", score=f["mom"] / max(f["vol"], 1e-6)))
        candidates.sort(key=lambda s: s.score, reverse=True)
        slots = max(0, self.limits.max_open_positions - len(positions))
        signals.extend(candidates[:slots])
        return signals

    def on_exit(self, symbol: str) -> None:
        self.trailing_stops.pop(symbol, None)

    def on_entry(self, symbol: str, stop_price: Optional[float]) -> None:
        if stop_price is not None:
            self.trailing_stops[symbol] = stop_price

    # ------------------------------------------------------------ regime
    def regime(self, history: Dict[str, List[Bar]]) -> RegimeReport:
        report = RegimeReport()
        z_limit = float(self.p["regime_vol_z"])
        for symbol, bars in history.items():
            closes = [b.close for b in bars]
            z = ind.rolling_vol_zscore(closes, 20, 100)
            if z is not None and z > z_limit:
                report.anomalies[symbol] = round(z, 2)
        return report
