"""Stdlib-only webhook dispatcher (Discord, Telegram, or a generic JSON endpoint).

A failed notification must never affect trading: every error is swallowed and logged to the ledger.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from . import config


class WebhookNotifier:
    def __init__(self, url: str = config.WEBHOOK_URL, telegram_chat_id: str = config.TELEGRAM_CHAT_ID,
                 ledger: Any = None, timeout: float = 5.0, opener: Optional[Callable] = None):
        self.url = (url or "").strip()
        self.chat_id = telegram_chat_id
        self.ledger = ledger
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def kind(self) -> str:
        if "discord.com/api/webhooks" in self.url or "discordapp.com/api/webhooks" in self.url:
            return "discord"
        if "api.telegram.org" in self.url:
            return "telegram"
        return "generic"

    # ------------------------------------------------------------- payloads
    def build_payload(self, title: str, text: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = f"**{title}**\n{text}" if self.kind == "discord" else f"{title}\n{text}"
        if self.kind == "discord":
            return {"content": body[:1900]}
        if self.kind == "telegram":
            return {"chat_id": self.chat_id, "text": body[:4000], "disable_web_page_preview": True}
        return {"title": title, "text": text, "data": data or {}, "ts": time.time(), "source": "trading_engine"}

    def send(self, title: str, text: str, data: Optional[Dict[str, Any]] = None) -> bool:
        if not self.enabled:
            return False
        payload = json.dumps(self.build_payload(title, text, data), default=str).encode()
        req = urllib.request.Request(self.url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "trading-engine/0.1"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
            ok = 200 <= int(status) < 300
        except (urllib.error.URLError, OSError, ValueError) as e:
            ok = False
            if self.ledger is not None:
                self.ledger.log_event("WARN", "webhook_failed", f"{self.kind}: {e}"[:300])
        self.sent += int(ok)
        self.failed += int(not ok)
        return ok

    # ---------------------------------------------------------- formatters
    def notify_sweep(self, *, realized_profit: float, distributable: float, owner: float, reserve: float,
                     owner_total: float, reserve_total: float, equity_after: float, credit_remaining: float,
                     withdrawn_at_broker: bool, cycle: int) -> bool:
        text = (f"Realized trade profit swept: ${distributable:.2f} CAD (cumulative realized ${realized_profit:.2f})\n"
                f"10% compute reserve retained: ${reserve:.2f}  (reserve total ${reserve_total:.2f}, "
                f"credit pool now ${credit_remaining:.2f})\n"
                f"90% owner profit segregated for bank withdrawal: ${owner:.2f}  (owner total ${owner_total:.2f})\n"
                f"Trading equity after sweep: ${equity_after:.2f} | "
                f"{'moved at broker' if withdrawn_at_broker else 'PENDING manual transfer from exchange'} | cycle {cycle}")
        return self.send("Profit sweep 90/10", text, {
            "distributable": distributable, "owner": owner, "reserve": reserve, "owner_total": owner_total,
            "reserve_total": reserve_total, "equity_after": equity_after, "credit_remaining": credit_remaining,
            "withdrawn_at_broker": withdrawn_at_broker, "cycle": cycle})

    def notify_alert(self, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> bool:
        return self.send(f"Alert: {kind}", message, data)
