"""Rule-based slow-trend + pullback strategy on daily bars with ATR trailing stops and a cash gate.

Cash gate (evaluated first, on the equal-weight basket of the universe):
    macro bear   basket < SMA(macro_sma) AND basket momentum(macro_mom_lookback) < 0
                 -> liquidate everything, hold 100% CAD, zero turnover until it clears
    no entries   basket < SMA(macro_sma) OR basket momentum <= 0 OR basket 20d vol z-score > vol_z_max
Per-asset entry filters: 20d vol z-score <= vol_z_max, momentum >= momentum_min, efficiency ratio >= er_min,
and (breakout_confirm) close above the prior donchian_period-day high so entries never start mid-range.
Entry (long only):  close > SMA(slow) and close > SMA(fast), momentum > 0, rsi_entry_min <= RSI <= rsi_entry_max
Exit:               close < SMA(slow)  or  RSI > rsi_exit  or  close <= trailing stop  or  macro bear
Sizing:             vol-targeted fraction of the position cap, never above the cap.

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
class GateState:
    entries_allowed: bool = True
    force_exit: bool = False
    reasons: List[str] = field(default_factory=list)
    basket_close: Optional[float] = None
    basket_sma: Optional[float] = None
    basket_mom: Optional[float] = None
    basket_vol_z: Optional[float] = None

    @property
    def active(self) -> bool:
        return not self.entries_allowed

    def key(self) -> str:
        return f"{int(self.entries_allowed)}{int(self.force_exit)}:" + ",".join(self.reasons)


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
        self.gate = GateState()
        self.asset_filters: Dict[str, str] = {}      # symbol -> why its entry was filtered this bar

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
                       self.p["rsi_period"] + 1, self.p["er_period"] + 1, self.p.get("donchian_period", 20) + 1)) + 1

    # ------------------------------------------------------------ cash gate
    def evaluate_gate(self, history: Dict[str, List[Bar]]) -> GateState:
        """Macro regime + volatility gate on the equal-weight basket. Cheap: a few hundred floats."""
        g = GateState()
        if int(self.p.get("regime_gate", 1)) == 0:
            self.gate = g
            return g
        macro_sma = int(self.p["macro_sma"])
        mom_lb = int(self.p["macro_mom_lookback"])
        need = max(macro_sma, mom_lb) + 1
        usable = [[b.close for b in bars] for bars in history.values() if len(bars) >= need]
        if len(usable) < max(1, len(history) // 2):
            g.entries_allowed = False
            g.reasons.append("insufficient history for macro gate")
            self.gate = g
            return g
        basket = ind.basket_index(usable)
        g.basket_close = basket[-1]
        g.basket_sma = ind.sma(basket, macro_sma)
        g.basket_mom = ind.momentum(basket, mom_lb)
        g.basket_vol_z = ind.rolling_vol_zscore(basket, 20, 100)
        below = g.basket_sma is not None and g.basket_close < g.basket_sma
        neg_mom = g.basket_mom is not None and g.basket_mom <= 0
        if below:
            g.reasons.append(f"basket below {macro_sma}d SMA")
        if neg_mom:
            g.reasons.append(f"basket {mom_lb}d momentum {g.basket_mom:+.1%}")
        if g.basket_vol_z is not None and g.basket_vol_z > float(self.p["vol_z_max"]):
            g.reasons.append(f"basket vol z {g.basket_vol_z:.1f} > {float(self.p['vol_z_max']):.1f}")
        g.entries_allowed = not g.reasons
        g.force_exit = below and neg_mom              # both conditions: hysteresis against SMA flip-flop
        self.gate = g
        return g

    def _asset_filter(self, bars: Sequence[Bar], f: Dict[str, float]) -> Optional[str]:
        if int(self.p.get("regime_gate", 1)) == 0:
            return None
        closes = [b.close for b in bars]
        z = ind.rolling_vol_zscore(closes, 20, 100)
        if z is not None and z > float(self.p["vol_z_max"]):
            return f"vol z {z:.1f}"
        if f["mom"] < float(self.p["momentum_min"]):
            return f"momentum {f['mom']:+.1%} < floor"
        er = ind.efficiency_ratio(closes, int(self.p["er_period"]))
        if er is not None and er < float(self.p["er_min"]):
            return f"chop ER {er:.2f}"
        if int(self.p.get("breakout_confirm", 0)):
            upper = ind.donchian_upper([b.high for b in bars], int(self.p["donchian_period"]))
            if upper is not None and f["close"] <= upper:
                return f"no breakout: close {f['close']:.2f} <= {int(self.p['donchian_period'])}d high {upper:.2f}"
        return None

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
        gate = self.evaluate_gate(history)
        self.asset_filters = {}
        for symbol, bars in history.items():
            f = self.features(bars)
            if f is None:
                continue
            held = positions.get(symbol)
            close = f["close"]
            if held is not None and gate.force_exit:
                signals.append(Signal(symbol, "EXIT", 0.0, 0.0, self.trailing_stops.get(symbol), "regime gate: macro bear -> cash"))
                continue
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
            if not gate.entries_allowed:
                continue
            if close > f["slow"] and close > f["fast"] and f["mom"] > 0 \
                    and float(self.p["rsi_entry_min"]) <= f["rsi"] <= float(self.p["rsi_entry_max"]):
                why = self._asset_filter(bars, f)
                if why:
                    self.asset_filters[symbol] = why
                    continue
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
