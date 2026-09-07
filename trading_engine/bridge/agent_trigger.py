"""Cost-gated bridge to Claude. The ONLY module allowed to spend compute credits.

Invocation reasons (and nothing else):
  * monthly_review   - scheduled strategy evaluation (30 days; ~$0.18/call => ~$2.16/yr)
  * circuit_breaker  - the 3% daily drawdown kill-switch tripped (emergency, at most daily)
  * feed_outage      - N consecutive market-data failures (emergency, at most daily)
  * self_heal        - N consecutive non-network exceptions in the daemon loop
Volatility regime shifts are handled locally by the strategy's cash gate and never call the model.

Gates, all evaluated locally before any network call:
  1. bridge enabled + API key present (else the call is logged as skipped at $0)
  2. credit remaining above CLAUDE_CREDIT_FLOOR_CAD
  3. estimated call cost below CLAUDE_MAX_COST_PER_CALL_CAD and below 25% of remaining credit
  4. per-reason minimum interval since the last call of that reason
The model may propose strategy parameter changes only; they are clamped to STRATEGY_PARAM_BOUNDS
and can never touch RiskLimits (Second Law). Self-heal patches are written to disk for review.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .. import config
from ..ledger import Ledger

MIN_INTERVAL: Dict[str, int] = {
    "monthly_review": config.SCHEDULED_REVIEW_INTERVAL_SECONDS - 3600,
    "circuit_breaker": config.EMERGENCY_TRIGGER_MIN_INTERVAL_SECONDS,
    "feed_outage": config.EMERGENCY_TRIGGER_MIN_INTERVAL_SECONDS,
    "self_heal": config.SELF_HEAL_MIN_INTERVAL_SECONDS,
}
SCHEDULED_REASONS = frozenset({"monthly_review"})

SYSTEM_PROMPT = (
    "You are the strategy reviewer for a fully automated micro-account trading daemon "
    "(CAD, long-only TSX ETFs, max 5% per position, 3% daily drawdown halt). "
    "Risk limits are owner-defined and immutable; you may only tune strategy parameters within the "
    "bounds provided. Be concise. Respond with a short assessment followed by a JSON object on its own "
    "line of the form {\"param_overrides\": {...}, \"halt_new_entries\": false, \"notes\": \"...\"}. "
    "For self_heal requests, additionally include a unified diff in a ```diff block if a code fix is warranted."
)


@dataclass
class AgentResult:
    invoked: bool
    reason: str
    skipped_because: str = ""
    text: str = ""
    param_overrides: Dict[str, float] = field(default_factory=dict)
    halt_new_entries: bool = False
    patch: Optional[str] = None
    cost_cad: float = 0.0
    model: str = ""


def estimate_cost_cad(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
                      cache_write: int = 0, usd_cad: float = config.USD_CAD_RATE) -> float:
    price = config.CLAUDE_PRICING_USD_PER_M.get(model) or config.CLAUDE_PRICING_USD_PER_M[config.CLAUDE_MODEL]
    usd = (input_tokens * price["input"] + output_tokens * price["output"]
           + cache_read * price["cache_read"] + cache_write * price["cache_write"]) / 1e6
    return usd * usd_cad


class AgentTrigger:
    def __init__(self, ledger: Ledger, enabled: bool = config.CLAUDE_BRIDGE_ENABLED,
                 api_key: Optional[str] = None, model: str = config.CLAUDE_MODEL, client: Any = None):
        self.ledger = ledger
        self.enabled = enabled
        self.api_key = api_key if api_key is not None else config.env("ANTHROPIC_API_KEY")
        self.model = model
        self._client = client            # injectable for tests
        self.invocations = 0

    # ---------------------------------------------------------------- gating
    def can_invoke(self, reason: str, prompt_chars: int) -> tuple:
        if reason not in MIN_INTERVAL:
            return False, f"unknown reason {reason}"
        if not self.enabled:
            return False, "bridge disabled (CLAUDE_BRIDGE_ENABLED=false)"
        if not self.api_key and self._client is None:
            return False, "no ANTHROPIC_API_KEY"
        remaining = self.ledger.credit_remaining_cad()
        if remaining <= config.CLAUDE_CREDIT_FLOOR_CAD:
            return False, f"credit {remaining:.2f} CAD at/below floor {config.CLAUDE_CREDIT_FLOOR_CAD:.2f}"
        est_in = min(prompt_chars, config.CLAUDE_MAX_INPUT_CHARS) // 4 + 400
        est = estimate_cost_cad(self.model, est_in, config.CLAUDE_MAX_OUTPUT_TOKENS)
        if est > config.CLAUDE_MAX_COST_PER_CALL_CAD:
            return False, f"estimated cost {est:.2f} CAD exceeds per-call cap"
        if est > 0.25 * remaining:
            return False, f"estimated cost {est:.2f} CAD exceeds 25% of remaining credit"
        last = self.ledger.last_token_call_ts(reason)
        if last is not None and time.time() - last < MIN_INTERVAL[reason]:
            return False, f"{reason} called {int(time.time() - last)}s ago (< {MIN_INTERVAL[reason]}s)"
        if reason in SCHEDULED_REASONS:
            spent = self.ledger.credit_spent_since(time.time() - 365 * 86400)
            if spent >= config.CLAUDE_SCHEDULED_ANNUAL_CAP_CAD:
                return False, f"trailing-365d spend {spent:.2f} CAD at annual cap {config.CLAUDE_SCHEDULED_ANNUAL_CAP_CAD:.2f}"
        return True, "ok"

    # ------------------------------------------------------------ invocation
    def maybe_invoke(self, reason: str, context: Dict[str, Any]) -> AgentResult:
        prompt = self._build_prompt(reason, context)
        ok, why = self.can_invoke(reason, len(prompt))
        if not ok:
            self.ledger.log_event("INFO", "bridge_skipped", f"{reason}: {why}", {"cost_cad": 0.0})
            return AgentResult(False, reason, skipped_because=why)
        try:
            text, usage, served_model = self._call(prompt, reason)
        except Exception as e:  # noqa: BLE001 - network/API failure must never kill the daemon
            self.ledger.record_tokens(purpose=reason, model=self.model, input_tokens=0, output_tokens=0,
                                      cost_usd=0.0, cost_cad=0.0, ok=False, note=f"error: {e}"[:500])
            self.ledger.log_event("ERROR", "bridge_error", f"{reason}: {e}")
            return AgentResult(False, reason, skipped_because=f"api error: {e}")
        cost_cad = estimate_cost_cad(served_model, usage["input"], usage["output"], usage["cache_read"], usage["cache_write"])
        self.ledger.record_tokens(purpose=reason, model=served_model, input_tokens=usage["input"],
                                  output_tokens=usage["output"], cache_read_tokens=usage["cache_read"],
                                  cache_write_tokens=usage["cache_write"], cost_usd=cost_cad / config.USD_CAD_RATE,
                                  cost_cad=cost_cad, ok=True)
        self.invocations += 1
        result = self._parse(text, reason)
        result.cost_cad = cost_cad
        result.model = served_model
        self.ledger.log_event("INFO", "bridge_invoked", f"{reason} via {served_model} cost={cost_cad:.4f} CAD",
                              {"overrides": result.param_overrides, "halt": result.halt_new_entries})
        if result.patch:
            self._write_patch(reason, result.patch)
        return result

    def _call(self, prompt: str, reason: str):
        client = self._client
        if client is None:
            import anthropic  # imported lazily: the daemon must run without the SDK installed
            client = anthropic.Anthropic(api_key=self.api_key, max_retries=1, timeout=300.0)
            self._client = client
        effort = "medium" if reason in ("self_heal", "feed_outage") else "low"
        response = client.beta.messages.create(
            model=self.model,
            max_tokens=config.CLAUDE_MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": config.CLAUDE_FALLBACK_MODEL}],
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model refused the request")
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        u = response.usage
        usage = {
            "input": int(getattr(u, "input_tokens", 0) or 0),
            "output": int(getattr(u, "output_tokens", 0) or 0),
            "cache_read": int(getattr(u, "cache_read_input_tokens", 0) or 0),
            "cache_write": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        }
        return text, usage, getattr(response, "model", self.model) or self.model

    # -------------------------------------------------------------- helpers
    def _build_prompt(self, reason: str, context: Dict[str, Any]) -> str:
        ctx = json.dumps(context, default=str, indent=None, sort_keys=True)
        if len(ctx) > config.CLAUDE_MAX_INPUT_CHARS:
            ctx = ctx[: config.CLAUDE_MAX_INPUT_CHARS] + "...[truncated]"
        bounds = json.dumps(config.STRATEGY_PARAM_BOUNDS)
        return (f"Trigger reason: {reason}\nParameter bounds: {bounds}\n"
                f"Current state (JSON): {ctx}\n"
                "Assess whether the strategy should keep running unchanged, adjust parameters within bounds, "
                "or halt new entries. Explain in <=120 words, then the JSON line.")

    @staticmethod
    def _parse(text: str, reason: str) -> AgentResult:
        result = AgentResult(True, reason, text=text)
        m = re.search(r"\{[^{}]*\"param_overrides\"[\s\S]*\}", text)
        if m:
            try:
                payload = json.loads(m.group(0))
                overrides = payload.get("param_overrides") or {}
                result.param_overrides = {k: float(v) for k, v in overrides.items()
                                          if k in config.STRATEGY_PARAM_BOUNDS and isinstance(v, (int, float))}
                result.halt_new_entries = bool(payload.get("halt_new_entries", False))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        d = re.search(r"```diff\n([\s\S]*?)```", text)
        if d:
            result.patch = d.group(1)
        return result

    def _write_patch(self, reason: str, patch: str) -> None:
        config.PATCH_DIR.mkdir(parents=True, exist_ok=True)
        path = config.PATCH_DIR / f"{int(time.time())}_{reason}.diff"
        path.write_text(patch)
        self.ledger.log_event("WARN", "patch_proposed", f"self-heal patch written to {path} (AUTO_APPLY_PATCHES=False)")
