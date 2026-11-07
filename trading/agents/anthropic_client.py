"""Anthropic client — API key only from env / Settings. Never hardcode secrets."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import base64
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable
import httpx
import copy

from trading.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class AnthropicClient:
    """Thin wrapper. Missing key → stub/fallback mode (deterministic, test-safe)."""

    settings: Settings = field(default_factory=get_settings)
    model: str | None = None
    budget: Any = field(default=None, repr=False)
    last_status: str = "not_called"
    successful_calls: int = 0
    last_attempt: dict[str, Any] = field(default_factory=dict, repr=False)
    decision_schema: dict[str, Any] | None = field(default=None, repr=False)
    can_call: Callable[[], bool] | None = field(default=None, repr=False)
    _client: Any = field(default=None, repr=False, init=False)

    @property
    def api_key(self) -> str:
        # Env wins; Settings also reads ANTHROPIC_API_KEY via pydantic-settings.
        return (
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or (self.settings.anthropic_api_key or "").strip()
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _ensure_sdk(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.available:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dep edge
            raise RuntimeError("anthropic package not installed") from exc
        self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.settings.ai_provider_timeout_seconds, max_retries=0)
        return self._client

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 800,
        images: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Ask for JSON and retain safe request/usage telemetry, never prompt content."""
        started = time.monotonic()
        reservation = None
        reserved_cost = 0.
        model = self.model or self.settings.anthropic_model
        schema = copy.deepcopy(self.decision_schema)
        if schema is not None:
            max_tokens = max(max_tokens, getattr(self.settings, 'anthropic_decision_max_tokens', 1200))
        attempt = dict(provider='anthropic', model=model, status='preparing', phase='prepare',
                       http_status=None, error_category='', latency_ms=0,
                       count_requests=0, generation_requests=0,
                       input_tokens=None, output_tokens=None, estimated_cost_usd=None, pending_usd=0.,
                       output_stop_reason=None, validation_issues=[])
        self.last_attempt = attempt
        try:
            if self.can_call is not None and not self.can_call():
                self.last_status = "stopped"
                return None
            if not self.available:
                self.last_status = "missing_key"
                return None
            client = self._ensure_sdk()
            content = [{"type": "text", "text": user}]
            for path in images or []:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(Path(path).read_bytes()).decode("ascii")}})
            arguments = dict(model=model, system=system, messages=[{"role": "user", "content": content}])
            if schema is not None:
                arguments.update(
                    tools=[dict(name='submit_trade_decision',
                        description='Submit the single structured decision for the supplied candidate. This does not execute an order.',
                        input_schema=schema, strict=True)],
                    tool_choice=dict(type='tool', name='submit_trade_decision', disable_parallel_tool_use=True))
            if self.budget is not None:
                if not self.budget.can_spend():
                    self.last_status = "budget_exhausted"
                    return None
                if self.can_call is not None and not self.can_call():
                    self.last_status = "stopped"
                    return None
                attempt['phase'] = 'count_tokens'
                attempt['count_requests'] += 1
                counted = client.messages.count_tokens(**arguments).input_tokens + 1024
                if counted > 50000:
                    self.last_status = "context_too_large"
                    return None
                reserved_cost = (counted*self.settings.anthropic_input_usd_per_million + max_tokens*self.settings.anthropic_output_usd_per_million)/1e6
                reservation = self.budget.reserve(reserved_cost)
                if reservation is None:
                    self.last_status = "budget_or_interval_hold"
                    return None
            if self.can_call is not None and not self.can_call():
                if reservation is not None:
                    self.budget.release(reservation)
                    reservation = None
                self.last_status = "stopped"
                return None
            attempt['phase'] = 'generation'
            attempt['generation_requests'] += 1
            message = client.messages.create(max_tokens=max_tokens, **arguments)
            attempt['http_status'] = 200
            attempt['input_tokens'] = message.usage.input_tokens
            attempt['output_tokens'] = message.usage.output_tokens
            attempt['estimated_cost_usd'] = (message.usage.input_tokens*self.settings.anthropic_input_usd_per_million + message.usage.output_tokens*self.settings.anthropic_output_usd_per_million)/1e6
            if self.budget is not None:
                self.budget.settle(reservation, message.usage.input_tokens, message.usage.output_tokens)
                reservation = None
            self.successful_calls += 1
            self.last_status = "success"
            stop_reason = getattr(message, 'stop_reason', None)
            allowed_reasons = {'end_turn','max_tokens','stop_sequence','tool_use','pause_turn','refusal','model_context_window_exceeded'}
            attempt['output_stop_reason'] = stop_reason if stop_reason in allowed_reasons else 'unknown' if stop_reason is not None else None
            if stop_reason == 'max_tokens' or stop_reason == 'model_context_window_exceeded':
                self.last_status = 'output_truncated'
                attempt['error_category'] = 'output_truncated'
                attempt['validation_issues'] = [dict(field='response', type='output_truncated')]
                return None
            if stop_reason == 'refusal':
                self.last_status = 'model_refusal'
                attempt['error_category'] = 'model_refusal'
                attempt['validation_issues'] = [dict(field='response', type='model_refusal')]
                return None
            if schema is not None:
                def attr(block, name, default=None):
                    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)
                tools = [block for block in message.content if attr(block, 'type') == 'tool_use']
                if stop_reason != 'tool_use' or len(tools) != 1 or attr(tools[0], 'name') != 'submit_trade_decision':
                    self.last_status = 'error_or_invalid_output'
                    attempt['error_category'] = 'missing_decision_tool'
                    attempt['validation_issues'] = [dict(field='response', type='missing_decision_tool')]
                    return None
                result = attr(tools[0], 'input')
                if not isinstance(result, dict):
                    self.last_status = 'error_or_invalid_output'
                    attempt['error_category'] = 'invalid_tool_input'
                    attempt['validation_issues'] = [dict(field='response', type='object_required')]
                    return None
                return result
            text_parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if text:
                    text_parts.append(text)
            raw = "\n".join(text_parts).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("expected JSON object")
            return result
        except sqlite3.Error:
            self.last_status = 'persistence_error'
            attempt['error_category'] = 'persistence_error'
            raise
        except Exception as exc:  # noqa: BLE001 — raw response content is never retained
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int) and 100 <= status_code <= 599:
                attempt['http_status'] = status_code
            if reservation is not None and isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in (408, 499):
                self.budget.release(reservation)
                reservation = None
            self.last_status = "error_or_invalid_output"
            body = getattr(exc, "body", {})
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
            if "credit balance" in message or "insufficient credit" in message:
                self.last_status = "provider_credit_exhausted"
                attempt['error_category'] = 'credit_exhausted'
            elif status_code == 429:
                attempt['error_category'] = 'rate_limit'
            elif status_code in (401, 403):
                attempt['error_category'] = 'authentication_or_permission'
            elif status_code in (408, 499) or isinstance(exc,httpx.TimeoutException) or type(exc).__name__=="APITimeoutError":
                attempt['error_category'] = 'ambiguous_timeout'
            elif isinstance(status_code, int) and 400 <= status_code < 500:
                attempt['error_category'] = 'request_rejected'
            elif isinstance(status_code, int) and status_code >= 500:
                attempt['error_category'] = 'provider_server_error'
            elif attempt['input_tokens'] is not None:
                attempt['error_category'] = 'invalid_output'
                attempt['validation_issues'] = [dict(field='response', type='invalid_json' if isinstance(exc, json.JSONDecodeError) else 'invalid_output')]
            else:
                attempt['error_category'] = 'transport_or_local_error'
            logger.warning("Anthropic call failed; using stub fallback: %s", type(exc).__name__)
            return None
        finally:
            attempt['status'] = self.last_status
            attempt['latency_ms'] = round((time.monotonic()-started)*1000)
            attempt['pending_usd'] = reserved_cost if reservation is not None else 0.
            if not attempt['error_category'] and self.last_status != 'success':
                attempt['error_category'] = self.last_status
