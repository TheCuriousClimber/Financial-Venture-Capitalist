"""Kraken pair-name translation for the CAD crypto universe.

Kraken's REST API accepts several spellings for the same market and *answers with a different one*:
    query XBTCAD  -> result key "XXBTZCAD"   (legacy X/Z-prefixed name)
    query ETHCAD  -> result key "XETHZCAD"
    query SOLCAD  -> result key "SOLCAD"
A batched query (``pair=A,B,C``) fails entirely with ``EQuery:Unknown asset pair`` if any one name is wrong.

``KrakenPairResolver`` resolves each internal symbol (``BTC/CAD``) to one verified REST name via a per-symbol
``AssetPairs`` lookup, records every spelling Kraken may use in responses (key, altname, wsname, our aliases),
and marks pairs the venue does not know as unavailable so they are excluded from later batch requests
instead of breaking them.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from .. import config

# internal symbol -> candidate REST pair names, most likely first. Kraken's wsname (XBT/CAD) is derived
# from the altname; legacy keys are what appear in Ticker/OHLC results.
PAIR_ALIASES: Dict[str, List[str]] = {
    "BTC/CAD": ["XBTCAD", "XXBTZCAD", "XBT/CAD"],
    "ETH/CAD": ["ETHCAD", "XETHZCAD", "ETH/CAD"],
    "SOL/CAD": ["SOLCAD", "SOL/CAD"],
    "XRP/CAD": ["XRPCAD", "XXRPZCAD", "XRP/CAD"],
    "ADA/CAD": ["ADACAD", "ADA/CAD"],
    "DOGE/CAD": ["DOGECAD", "XDGCAD", "XXDGZCAD", "XDG/CAD", "DOGE/CAD"],
}

UNKNOWN_PAIR_MARKER = "Unknown asset pair"


def aliases_for(symbol: str) -> List[str]:
    out = list(PAIR_ALIASES.get(symbol, []))
    a = config.ASSETS.get(symbol)
    if a is not None:
        for cand in (a.exchange_pair, f"{a.base_asset}ZCAD", f"{a.base_asset}CAD"):
            if cand and cand not in out:
                out.append(cand)
    return out


class KrakenPairResolver:
    """Maps internal symbols <-> Kraken REST pair names. ``getter(method, params) -> result dict`` performs
    the public request and must raise an exception whose text contains the Kraken error array on API
    errors (KrakenFeed._get raises RuntimeError, KrakenBroker._public raises BrokerError)."""

    def __init__(self, getter: Callable[[str, Dict[str, str]], Dict[str, Any]], symbols: Iterable[str]):
        self._get = getter
        self.symbols: List[str] = list(symbols)
        self.symbol_to_pair: Dict[str, str] = {}          # verified REST name to send in queries
        self.key_to_symbol: Dict[str, str] = {}           # any spelling Kraken may return -> internal symbol
        self.unavailable: Dict[str, str] = {}             # symbol -> reason (venue does not list it)
        self.rules: Dict[str, Dict[str, Any]] = {}        # symbol -> ordermin / lot_decimals / costmin / source
        self._resolved = False
        for s in self.symbols:                            # aliases are usable for response mapping before resolution
            for alias in aliases_for(s):
                self.key_to_symbol.setdefault(alias, s)

    # ------------------------------------------------------------- resolution
    def resolve(self, force: bool = False) -> "KrakenPairResolver":
        """Per-symbol AssetPairs lookup. Network errors propagate (caller decides on fallback); an
        ``Unknown asset pair`` answer marks just that symbol unavailable."""
        if self._resolved and not force:
            return self
        for s in self.symbols:
            asset = config.ASSETS.get(s)
            fallback_rules = {"ordermin": asset.ordermin_fallback if asset else 0.0,
                              "lot_decimals": asset.lot_decimals if asset else 8, "pair_decimals": 2,
                              "costmin": 1.0, "source": "fallback"}
            last_err: Optional[str] = None
            hit = False
            for alias in aliases_for(s):
                if "/" in alias:
                    continue                              # wsnames are not accepted as query values
                try:
                    res = self._get("AssetPairs", {"pair": alias})
                except Exception as e:  # noqa: BLE001 - feed raises RuntimeError, broker raises BrokerError
                    if UNKNOWN_PAIR_MARKER in str(e):
                        last_err = str(e)
                        continue
                    raise
                want = _normalise(alias)
                for key, info in res.items():
                    spellings = [key, info.get("altname"), info.get("wsname"), alias]
                    if not any(sp and _normalise(sp) == want for sp in spellings[:3]):
                        continue                          # a different pair in the same response
                    self.symbol_to_pair[s] = info.get("altname") or alias
                    for spelling in spellings:
                        if spelling:
                            self.key_to_symbol[spelling] = s
                    self.rules[s] = {"ordermin": float(info.get("ordermin", fallback_rules["ordermin"])),
                                     "lot_decimals": int(info.get("lot_decimals", fallback_rules["lot_decimals"])),
                                     "pair_decimals": int(info.get("pair_decimals", 2)),
                                     "costmin": float(info.get("costmin") or 1.0), "source": "exchange"}
                    hit = True
                    break
                if hit:
                    self.unavailable.pop(s, None)
                    break
                # request succeeded but returned nothing matching: keep the alias, unverified
                self.symbol_to_pair[s] = alias
                self.rules[s] = fallback_rules
                hit = True
                break
            if not hit:
                self.unavailable[s] = last_err or "no alias accepted"
                self.symbol_to_pair.pop(s, None)
                self.rules[s] = fallback_rules
        self._resolved = True
        return self

    @property
    def resolved(self) -> bool:
        return self._resolved

    # ---------------------------------------------------------------- lookups
    def available(self) -> List[str]:
        return [s for s in self.symbols if s in self.symbol_to_pair]

    def pair(self, symbol: str) -> Optional[str]:
        return self.symbol_to_pair.get(symbol)

    def query_value(self, symbols: Iterable[str]) -> str:
        return ",".join(self.symbol_to_pair[s] for s in symbols if s in self.symbol_to_pair)

    def symbol_for(self, key: str) -> Optional[str]:
        """Map a response key (legacy, altname or wsname) back to the internal symbol."""
        if key in self.key_to_symbol:
            return self.key_to_symbol[key]
        for s in self.symbols:                            # tolerant match: X/Z prefixes stripped
            for alias in aliases_for(s):
                if _normalise(alias) == _normalise(key):
                    self.key_to_symbol[key] = s
                    return s
        return None

    def map_response(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Re-key a Ticker/OHLC/AssetPairs result dict by internal symbol; non-pair keys (``last``) dropped."""
        out: Dict[str, Any] = {}
        for key, value in result.items():
            s = self.symbol_for(key)
            if s is not None:
                out[s] = value
        return out


_BASE_CODES = [("XXBT", "BTC"), ("XBT", "BTC"), ("XETH", "ETH"), ("XXRP", "XRP"), ("XXDG", "DOGE"), ("XDG", "DOGE")]


def _normalise(name: str) -> str:
    """XXBTZCAD / XBTCAD / XBT/CAD -> BTCCAD; SOLCAD -> SOLCAD; unknown names pass through upper-cased."""
    n = name.replace("/", "").upper()
    if n.endswith("ZCAD"):
        n = n[:-4] + "CAD"
    for code, base in _BASE_CODES:                      # longest codes first
        if n.startswith(code):
            return base + n[len(code):]
    return n
