"""Anthropic client — API key only from env / Settings. Never hardcode secrets."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from trading.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class AnthropicClient:
    """Thin wrapper. Missing key → stub/fallback mode (deterministic, test-safe)."""

    settings: Settings = field(default_factory=get_settings)
    model: str = "claude-sonnet-4-20250514"
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
        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 800,
    ) -> dict[str, Any] | None:
        """Ask for JSON. Returns None on missing key / errors (caller uses stub)."""
        if not self.available:
            logger.debug("Anthropic stub mode: no ANTHROPIC_API_KEY")
            return None
        try:
            client = self._ensure_sdk()
            message = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
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
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — fallback path is intentional
            logger.warning("Anthropic call failed; using stub fallback: %s", type(exc).__name__)
            return None
