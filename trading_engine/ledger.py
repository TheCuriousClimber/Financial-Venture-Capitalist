"""SQLite ledger: trades, equity snapshots, token expenditures, realized PnL and 90/10 split.

The ledger is the single source of truth for both finite pools:
  * investment capital  -> trades / equity_snapshots / profit_splits
  * compute credits     -> token_expenditures

All money is CAD, rounded to cents at the boundaries where it is booked.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cycle INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    slippage_bps REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    equity_at_order REAL,
    reason TEXT,
    broker TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cycle INTEGER,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity REAL NOT NULL,
    swept_total REAL NOT NULL,
    day_start_equity REAL,
    drawdown_pct REAL,
    open_positions INTEGER
);
CREATE TABLE IF NOT EXISTS token_expenditures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL,
    cost_cad REAL NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE TABLE IF NOT EXISTS profit_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cycle INTEGER,
    realized_cum REAL NOT NULL,
    distributable REAL NOT NULL,
    owner_disbursement REAL NOT NULL,
    operational_reserve REAL NOT NULL,
    equity_after REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


def cents(x: float) -> float:
    """Round half-up to cents without float drift surprises."""
    return round(x + 1e-12, 2) if x >= 0 else -round(-x + 1e-12, 2)


@dataclass
class SplitResult:
    distributable: float
    owner_disbursement: float
    operational_reserve: float


def compute_split(distributable: float) -> SplitResult:
    """Partition realized profit into 10% operational reserve and 90% owner disbursement.

    The two legs always sum exactly to ``distributable`` (in cents).
    """
    d = cents(max(0.0, distributable))
    reserve = cents(d * config.OPERATIONAL_SURPLUS_PCT)
    owner = cents(d - reserve)
    return SplitResult(d, owner, reserve)


class Ledger:
    def __init__(self, path: str = config.LEDGER_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._init_state()

    # ------------------------------------------------------------------ state kv
    def _init_state(self) -> None:
        defaults = {
            "principal_cad": f"{config.CAPITAL_BASE_CAD:.2f}",
            "credit_budget_cad": f"{config.CREDIT_BUDGET_CAD:.2f}",
            "created_ts": str(time.time()),
        }
        for k, v in defaults.items():
            self.conn.execute("INSERT OR IGNORE INTO state(key, value) VALUES (?, ?)", (k, v))

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def get_state_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_state(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_state_json(self, key: str, value: Any) -> None:
        self.set_state(key, json.dumps(value, sort_keys=True))

    @property
    def principal(self) -> float:
        return float(self.get_state("principal_cad") or config.CAPITAL_BASE_CAD)

    # --------------------------------------------------------------------- trades
    def record_trade(self, *, ts: float, cycle: Optional[int], symbol: str, side: str, qty: float,
                     price: float, commission: float, fees: float, slippage_bps: float,
                     realized_pnl: float, equity_at_order: float, reason: str, broker: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO trades(ts, cycle, symbol, side, qty, price, notional, commission, fees,
                   slippage_bps, realized_pnl, equity_at_order, reason, broker)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, cycle, symbol, side, qty, price, qty * price, commission, fees, slippage_bps,
                 realized_pnl, equity_at_order, reason, broker),
            )
            return int(cur.lastrowid)

    def trades(self, limit: int = 1000) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def trade_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def trades_today(self, day_start_ts: float) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trades WHERE ts >= ?", (day_start_ts,)).fetchone()[0])

    def realized_pnl_total(self) -> float:
        """Cumulative realized PnL net of all commissions and fees (both legs)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) AS pnl, COALESCE(SUM(commission+fees),0) AS cost FROM trades"
        ).fetchone()
        return float(row["pnl"]) - float(row["cost"])

    def last_exit_cycle(self, symbol: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT cycle FROM trades WHERE symbol=? AND side='SELL' ORDER BY id DESC LIMIT 1", (symbol,)
        ).fetchone()
        return int(row["cycle"]) if row and row["cycle"] is not None else None

    # ------------------------------------------------------------ equity snapshots
    def record_equity(self, *, ts: float, cycle: int, cash: float, positions_value: float, equity: float,
                      swept_total: float, day_start_equity: Optional[float], drawdown_pct: Optional[float],
                      open_positions: int) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO equity_snapshots(ts, cycle, cash, positions_value, equity, swept_total,
                   day_start_equity, drawdown_pct, open_positions) VALUES (?,?,?,?,?,?,?,?,?)""",
                (ts, cycle, cash, positions_value, equity, swept_total, day_start_equity, drawdown_pct, open_positions),
            )

    def equity_curve(self, limit: int = 10_000) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM (SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?) ORDER BY id ASC", (limit,)
        ).fetchall()

    # ------------------------------------------------------------- profit split
    def swept_total(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(distributable),0) FROM profit_splits").fetchone()
        return float(row[0])

    def reserve_total(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(operational_reserve),0) FROM profit_splits").fetchone()
        return float(row[0])

    def owner_total(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(owner_disbursement),0) FROM profit_splits").fetchone()
        return float(row[0])

    def pending_distribution(self, equity: float) -> float:
        """Realized profit not yet swept, capped so trading equity never drops below principal.

        d = max(0, min(R - S, E - P)) where R = cumulative realized PnL (net of fees),
        S = already swept, E = current trading equity, P = principal.
        """
        realized = self.realized_pnl_total()
        swept = self.swept_total()
        headroom = equity - self.principal
        return cents(max(0.0, min(realized - swept, headroom)))

    def record_split(self, *, ts: float, cycle: int, split: SplitResult, equity_after: float) -> None:
        if split.distributable <= 0:
            return
        with self._lock:
            self.conn.execute(
                """INSERT INTO profit_splits(ts, cycle, realized_cum, distributable, owner_disbursement,
                   operational_reserve, equity_after) VALUES (?,?,?,?,?,?,?)""",
                (ts, cycle, self.realized_pnl_total(), split.distributable, split.owner_disbursement,
                 split.operational_reserve, equity_after),
            )

    # ----------------------------------------------------------- token spending
    def record_tokens(self, *, purpose: str, model: str, input_tokens: int, output_tokens: int,
                      cache_read_tokens: int = 0, cache_write_tokens: int = 0, cost_usd: float = 0.0,
                      cost_cad: float = 0.0, ok: bool = True, note: str = "") -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO token_expenditures(ts, purpose, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, cost_cad, ok, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), purpose, model, input_tokens, output_tokens, cache_read_tokens,
                 cache_write_tokens, cost_usd, cost_cad, 1 if ok else 0, note),
            )

    def credit_spent_cad(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cost_cad),0) FROM token_expenditures").fetchone()
        return float(row[0])

    def credit_spent_for(self, purposes) -> float:
        purposes = list(purposes)
        if not purposes:
            return 0.0
        q = f"SELECT COALESCE(SUM(cost_cad),0) FROM token_expenditures WHERE purpose IN ({','.join('?' * len(purposes))})"
        return float(self.conn.execute(q, purposes).fetchone()[0])

    def credit_spent_since(self, ts: float) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cost_cad),0) FROM token_expenditures WHERE ts >= ?", (ts,)).fetchone()
        return float(row[0])

    def credit_budget_cad(self) -> float:
        return float(self.get_state("credit_budget_cad") or config.CREDIT_BUDGET_CAD)

    def credit_remaining_cad(self) -> float:
        """Initial credit pool + operational reserve earned from profits - spent."""
        return self.credit_budget_cad() + self.reserve_total() - self.credit_spent_cad()

    def total_tokens(self) -> Dict[str, int]:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,
               COALESCE(SUM(cache_read_tokens),0) cr, COUNT(*) n FROM token_expenditures"""
        ).fetchone()
        return {"input": int(row["i"]), "output": int(row["o"]), "cache_read": int(row["cr"]), "calls": int(row["n"])}

    def last_token_call_ts(self, purpose: Optional[str] = None) -> Optional[float]:
        if purpose:
            row = self.conn.execute("SELECT MAX(ts) FROM token_expenditures WHERE purpose=?", (purpose,)).fetchone()
        else:
            row = self.conn.execute("SELECT MAX(ts) FROM token_expenditures").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------- events
    def log_event(self, level: str, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(ts, level, kind, message, data) VALUES (?,?,?,?,?)",
                (time.time(), level, kind, message, json.dumps(data, default=str) if data else None),
            )

    def events(self, kind: Optional[str] = None, limit: int = 100) -> List[sqlite3.Row]:
        if kind:
            return self.conn.execute(
                "SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
        return self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------------------ summary
    def summary(self, equity: Optional[float] = None) -> Dict[str, Any]:
        tokens = self.total_tokens()
        return {
            "principal_cad": self.principal,
            "trades": self.trade_count(),
            "realized_pnl_net_cad": cents(self.realized_pnl_total()),
            "swept_total_cad": cents(self.swept_total()),
            "owner_disbursement_cad": cents(self.owner_total()),
            "operational_reserve_cad": cents(self.reserve_total()),
            "trading_equity_cad": cents(equity) if equity is not None else None,
            "credit_budget_cad": self.credit_budget_cad(),
            "credit_spent_cad": round(self.credit_spent_cad(), 4),
            "credit_remaining_cad": round(self.credit_remaining_cad(), 4),
            "compute_reserve_balance_cad": round(self.reserve_total() - self.credit_spent_for(["monthly_review"]), 4),
            "tokens": tokens,
        }

    def close(self) -> None:
        self.conn.close()
