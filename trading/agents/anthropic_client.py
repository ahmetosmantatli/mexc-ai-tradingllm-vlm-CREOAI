"""Anthropic client — API key only from env / Settings. Never hardcode secrets."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import base64
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

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
        self._client = anthropic.Anthropic(api_key=self.api_key, timeout=15.0, max_retries=0)
        return self._client

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 800,
        images: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Ask for JSON. Returns None on missing key / errors (caller uses stub)."""
        if not self.available:
            logger.debug("Anthropic stub mode: no ANTHROPIC_API_KEY")
            self.last_status = "missing_key"
            return None
        try:
            client = self._ensure_sdk()
            model = self.model or self.settings.anthropic_model
            content = [{"type": "text", "text": user}]
            for path in images or []:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(Path(path).read_bytes()).decode("ascii")}})
            arguments = dict(model=model, system=system, messages=[{"role": "user", "content": content}])
            reservation = None
            if self.budget is not None:
                if not self.budget.can_spend():
                    self.last_status = "budget_exhausted"
                    return None
                # Count before generating; reserve headroom for provider token accounting.
                counted = client.messages.count_tokens(**arguments).input_tokens + 1024
                if counted > 50000:
                    self.last_status = "context_too_large"
                    return None
                reserved_cost = (counted*self.settings.anthropic_input_usd_per_million + max_tokens*self.settings.anthropic_output_usd_per_million)/1e6
                reservation = self.budget.reserve(reserved_cost)
                if reservation is None:
                    self.last_status = "budget_or_interval_hold"
                    return None
            message = client.messages.create(max_tokens=max_tokens, **arguments)
            if self.budget is not None:
                self.budget.settle(reservation, message.usage.input_tokens, message.usage.output_tokens)
            self.successful_calls += 1
            self.last_status = "success"
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
            raise
        except Exception as exc:  # noqa: BLE001 — fallback path is intentional
            self.last_status = "error_or_invalid_output"
            body = getattr(exc, "body", {})
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
            if "credit balance" in message or "insufficient credit" in message:
                self.last_status = "provider_credit_exhausted"
            logger.warning("Anthropic call failed; using stub fallback: %s", type(exc).__name__)
            return None
