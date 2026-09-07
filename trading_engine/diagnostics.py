"""Pre-flight diagnostics for the Kraken paper soak / live cut-over. stdlib only.

Checks
  a. public REST connectivity to api.kraken.com
  b. live bid / ask / spread for BTC/CAD, ETH/CAD, SOL/CAD (plus any other watchlist pair)
  c. Kraken order minimums vs. the position cap (MAX_POSITION_PCT x principal) and fee-gate math
  d. SQLite ledger write / read on LEDGER_PATH

Run: python3 -m trading_engine.diagnostics [--ledger PATH] [--json]
Exit code 0 = all hard checks passed, 1 = a hard check failed. Network failure downgrades (b) and (c)
to estimates from config fallbacks and is reported as FAIL for (a).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import config
from .ledger import Ledger

KRAKEN_PUBLIC = "https://api.kraken.com/0/public"
FOCUS_PAIRS = ["BTC/CAD", "ETH/CAD", "SOL/CAD"]


@dataclass
class Check:
    name: str
    status: str                     # PASS | FAIL | WARN | SKIP
    detail: str
    data: Dict[str, Any] = field(default_factory=dict)


def _get(method: str, params: Optional[Dict[str, str]] = None, timeout: float = config.HTTP_TIMEOUT_SECONDS) -> Dict[str, Any]:
    url = f"{KRAKEN_PUBLIC}/{method}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError("; ".join(payload["error"]))
    return payload["result"]


def _match(key: str) -> Optional[config.Asset]:
    for a in config.CRYPTO_WATCHLIST:
        if key in (a.exchange_pair, f"{a.base_asset}ZCAD", f"{a.base_asset}CAD"):
            return a
    return None


# ------------------------------------------------------------------ a. connectivity
def check_connectivity() -> Check:
    t0 = time.time()
    try:
        res = _get("SystemStatus")
        srv = _get("Time")
        latency_ms = (time.time() - t0) * 1000 / 2
        skew = abs(time.time() - float(srv["unixtime"]))
        status = res.get("status", "unknown")
        ok = status == "online"
        detail = f"api.kraken.com status={status} latency~{latency_ms:.0f}ms clock_skew={skew:.1f}s"
        if skew > 30:
            detail += " (clock skew > 30s will break nonces; sync NTP)"
        return Check("kraken_connectivity", "PASS" if ok and skew <= 30 else "FAIL", detail,
                     {"status": status, "latency_ms": round(latency_ms), "clock_skew_s": round(skew, 1)})
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
        return Check("kraken_connectivity", "FAIL", f"cannot reach api.kraken.com: {e}", {"error": str(e)})


# ------------------------------------------------------------------ b. tickers
def check_tickers(symbols: List[str]) -> Check:
    pairs = ",".join(config.ASSETS[s].exchange_pair for s in symbols)
    try:
        res = _get("Ticker", {"pair": pairs})
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
        yahoo = _yahoo_fallback_quotes(symbols)
        if yahoo:
            return Check("live_tickers", "WARN", f"Kraken ticker unavailable ({e}); Yahoo last price with config spread estimate", yahoo)
        est = {s: {"bid": None, "ask": None, "spread_bps": config.ASSETS[s].typical_spread_bps, "source": "config_estimate"}
               for s in symbols}
        return Check("live_tickers", "FAIL", f"ticker unavailable ({e}); Yahoo fallback also unreachable; using config spread estimates", est)
    rows: Dict[str, Any] = {}
    for key, t in res.items():
        a = _match(key)
        if a is None:
            continue
        bid, ask, last = float(t["b"][0]), float(t["a"][0]), float(t["c"][0])
        mid = (bid + ask) / 2
        rows[a.symbol] = {"bid": bid, "ask": ask, "last": last, "spread_bps": round((ask - bid) / mid * 1e4, 2),
                          "spread_pct": round((ask - bid) / mid * 100, 4), "vol_24h_base": float(t["v"][1]) if "v" in t else None,
                          "source": "live"}
    missing = [s for s in symbols if s not in rows]
    status = "PASS" if not missing else "WARN"
    return Check("live_tickers", status, f"{len(rows)}/{len(symbols)} pairs quoted" + (f"; missing {missing}" if missing else ""), rows)


def _yahoo_fallback_quotes(symbols: List[str]) -> Dict[str, Any]:
    from .data.feed import YahooChartFeed
    feed = YahooChartFeed(symbols=symbols, symbol_map={s: s.replace("/", "-") for s in symbols}, lookback="5d")
    out: Dict[str, Any] = {}
    for s in symbols:
        try:
            q = feed.quote(s)
        except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError, IndexError):
            continue
        mid = q.mid
        out[s] = {"bid": q.bid, "ask": q.ask, "last": q.last, "spread_bps": round((q.ask - q.bid) / mid * 1e4, 2),
                  "spread_pct": round((q.ask - q.bid) / mid * 100, 4), "source": "yahoo_last+config_spread"}
    return out


# ------------------------------------------------------------------ c. minimums math
def check_minimums(symbols: List[str], tickers: Dict[str, Any]) -> Check:
    cap = config.RISK.max_position_pct * config.CAPITAL_BASE_CAD
    sized = cap * 0.995                              # RiskManager.SIZING_HAIRCUT
    fee_side_bps = config.PAPER_FEE_BPS if config.PAPER_LIVE_FEED else config.KRAKEN_TAKER_BPS
    try:
        res = _get("AssetPairs", {"pair": ",".join(config.ASSETS[s].exchange_pair for s in symbols)})
        live_rules = {}
        for _k, info in res.items():
            a = _match(info.get("altname", ""))
            if a:
                live_rules[a.symbol] = {"ordermin": float(info["ordermin"]), "costmin": float(info.get("costmin") or 0),
                                        "lot_decimals": int(info["lot_decimals"]), "source": "exchange"}
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError):
        live_rules = {}
    rows: Dict[str, Any] = {}
    all_ok = True
    any_estimate = False
    for s in symbols:
        a = config.ASSETS[s]
        rules = live_rules.get(s) or {"ordermin": a.ordermin_fallback, "costmin": 1.0, "lot_decimals": a.lot_decimals, "source": "fallback"}
        t = tickers.get(s) or {}
        ask = t.get("ask") or a.ref_price
        price_src = "live" if t.get("ask") else "config_ref_price"
        any_estimate |= price_src != "live" or rules["source"] != "exchange"
        min_notional = rules["ordermin"] * ask
        qty_at_cap = sized / ask
        ok = qty_at_cap >= rules["ordermin"] and sized >= rules["costmin"]
        headroom_pct = (sized / min_notional - 1) * 100 if min_notional > 0 else float("inf")
        spread = t.get("spread_bps", a.typical_spread_bps)
        rt_cost_bps = 2 * fee_side_bps + spread + 2 * config.FEE_TABLES["kraken"].slippage_bps_mean
        rt_ok = rt_cost_bps <= config.RISK.cost_cap_bps("crypto")
        min_edge_bps = rt_cost_bps / config.RISK.max_cost_to_edge_ratio
        all_ok &= ok and rt_ok
        rows[s] = {"ask": ask, "price_source": price_src, "ordermin": rules["ordermin"], "min_notional_cad": round(min_notional, 2),
                   "cap_cad": round(sized, 2), "qty_at_cap": round(qty_at_cap, 8), "headroom_pct": round(headroom_pct, 1),
                   "fits_cap": ok, "rules_source": rules["source"], "round_trip_cost_bps": round(rt_cost_bps, 1),
                   "cost_cap_bps": config.RISK.cost_cap_bps("crypto"), "fee_gate_ok": rt_ok,
                   "min_edge_bps_for_entry": round(min_edge_bps, 1),
                   "fee_per_side_cad_at_cap": round(sized * fee_side_bps / 1e4, 4)}
    status = "PASS" if all_ok else "FAIL"
    if all_ok and any_estimate:
        status = "WARN"
    fails = [s for s, r in rows.items() if not r["fits_cap"]]
    detail = (f"cap ${sized:.2f} (={config.RISK.max_position_pct:.0%} x ${config.CAPITAL_BASE_CAD:.2f} x 0.995); "
              + (f"below-minimum: {fails}" if fails else "all pairs clear Kraken minimums")
              + ("; based on estimates (exchange unreachable)" if any_estimate else ""))
    return Check("order_minimums_vs_cap", status, detail, rows)


# ------------------------------------------------------------------ d. ledger
def check_ledger(path: str) -> Check:
    try:
        ledger = Ledger(path)
        marker = f"diag-{int(time.time() * 1000)}"
        ledger.set_state("diagnostics_last_run", marker)
        ledger.log_event("INFO", "diagnostics", "ledger write/read test", {"marker": marker})
        read_back = ledger.get_state("diagnostics_last_run")
        ev = ledger.events("diagnostics", limit=1)
        journal = ledger.conn.execute("PRAGMA journal_mode").fetchone()[0]
        integrity = ledger.conn.execute("PRAGMA integrity_check").fetchone()[0]
        summary = ledger.summary()
        ok = read_back == marker and bool(ev) and integrity == "ok"
        size = os.path.getsize(path) if path != ":memory:" and os.path.exists(path) else 0
        ledger.close()
        return Check("sqlite_ledger", "PASS" if ok else "FAIL",
                     f"{path}: journal={journal} integrity={integrity} size={size}B trades={summary['trades']} "
                     f"credit_remaining=${summary['credit_remaining_cad']:.2f}",
                     {"path": path, "journal_mode": journal, "integrity": integrity, "round_trip_ok": read_back == marker,
                      "trades": summary["trades"], "credit_remaining_cad": summary["credit_remaining_cad"]})
    except (sqlite3.Error, OSError) as e:
        return Check("sqlite_ledger", "FAIL", f"{path}: {e}", {"error": str(e)})


# ------------------------------------------------------------------ runner
def run(ledger_path: str = config.LEDGER_PATH, symbols: Optional[List[str]] = None) -> List[Check]:
    symbols = symbols or [s for s in FOCUS_PAIRS if s in config.ASSETS]
    checks = [check_connectivity()]
    tick = check_tickers(symbols)
    checks.append(tick)
    checks.append(check_minimums(symbols, tick.data if tick.status != "FAIL" else {}))
    checks.append(check_ledger(ledger_path))
    checks.append(Check("config", "PASS" if not config.validate() else "FAIL",
                        "; ".join(config.validate()) or
                        f"universe={config.ASSET_UNIVERSE} feed={config.DATA_FEED} broker={config.BROKER} "
                        f"paper={config.PAPER_LIVE_FEED} live={config.LIVE_TRADING_ENABLED} bridge={config.CLAUDE_BRIDGE_ENABLED}"))
    return checks


def _fmt_money(v) -> str:
    return "n/a" if v is None else f"{v:,.2f}"


def render(checks: List[Check]) -> str:
    out: List[str] = ["=== trading_engine diagnostics ===", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), ""]
    for c in checks:
        out.append(f"[{c.status:<4}] {c.name}: {c.detail}")
        if c.name == "live_tickers":
            out.append(f"       {'pair':<9} {'bid':>12} {'ask':>12} {'spread bps':>11} {'spread %':>9}  source")
            for s, r in c.data.items():
                out.append(f"       {s:<9} {_fmt_money(r.get('bid')):>12} {_fmt_money(r.get('ask')):>12} "
                           f"{r.get('spread_bps', 0):>11.2f} {r.get('spread_pct', r.get('spread_bps', 0) / 100):>9.4f}  {r.get('source')}")
        if c.name == "order_minimums_vs_cap":
            out.append(f"       {'pair':<9} {'ordermin':>10} {'min $':>8} {'cap $':>7} {'headroom':>9} {'fits':>5} {'rt bps':>7} {'min edge':>9} {'fee/side $':>10}  src")
            for s, r in c.data.items():
                out.append(f"       {s:<9} {r['ordermin']:>10.5f} {r['min_notional_cad']:>8.2f} {r['cap_cad']:>7.2f} "
                           f"{r['headroom_pct']:>8.1f}% {'yes' if r['fits_cap'] else 'NO':>5} {r['round_trip_cost_bps']:>7.1f} "
                           f"{r['min_edge_bps_for_entry']:>9.1f} {r['fee_per_side_cad_at_cap']:>10.4f}  {r['rules_source']}/{r['price_source']}")
    hard_fail = any(c.status == "FAIL" for c in checks)
    out.append("")
    out.append("RESULT: " + ("FAIL (see above)" if hard_fail else "PASS"))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=config.LEDGER_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    checks = run(args.ledger)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2, default=str))
    else:
        print(render(checks))
    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
