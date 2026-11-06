"""Resolve live equity E: seed env only on empty DB / explicit reset."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from trading.config import Settings, get_settings
from trading.equity.mexc_balance import MexcBalanceView, parse_mexc_assets
from trading.equity.state import EquitySource, EquityState
from trading.equity.store import (
    EquityStore,
    InMemoryEquityStore,
    RedisCachedEquityStore,
    SqlAlchemyEquityStore,
)
from trading.exchange.mexc_readonly import MexcReadOnlyClient

logger = logging.getLogger(__name__)


@dataclass
class EquityService:
    """Bankroll authority for risk, sizing, and the equity API view.

    Env ``STARTING_CAPITAL_TRY`` / ``CAPITAL_FLOOR_TRY`` seed first boot only.
    After that, paper equity is restored from the store. When MEXC read-only
    keys are present, available balance is preferred for the equity *view*
    (orders stay paper while ``LIVE_TRADING_ENABLED=false``).
    """

    settings: Settings
    store: EquityStore
    mexc: MexcReadOnlyClient | None = None
    _current: EquityState | None = field(default=None, init=False, repr=False)

    @property
    def current(self) -> EquityState | None:
        return self._current

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        store: EquityStore | None = None,
        mexc: MexcReadOnlyClient | None = None,
        use_redis: bool = False,
        redis_client: Any | None = None,
    ) -> EquityService:
        cfg = settings or get_settings()
        if store is None:
            try:
                store = SqlAlchemyEquityStore.from_url(cfg.database_url)
            except Exception as exc:  # noqa: BLE001 — fall back for unit tests / offline
                logger.warning("equity SQL store unavailable (%s); using in-memory", exc)
                store = InMemoryEquityStore()
        if use_redis and redis_client is not None:
            store = RedisCachedEquityStore(inner=store, redis_client=redis_client)
        if mexc is None and cfg.mexc_api_key and cfg.mexc_api_secret:
            mexc = MexcReadOnlyClient(
                api_key=cfg.mexc_api_key,
                api_secret=cfg.mexc_api_secret,
                base_url=cfg.mexc_base_url,
                recv_window_ms=cfg.mexc_recv_window_ms,
            )
        return cls(settings=cfg, store=store, mexc=mexc)

    def seed_state(self, *, source: EquitySource = EquitySource.SEED) -> EquityState:
        e0 = float(self.settings.starting_capital_try)
        floor = float(self.settings.capital_floor_try)
        if floor <= 0:
            floor = e0
        return EquityState(
            equity=e0,
            starting_capital=e0,
            capital_floor=floor,
            currency=self.settings.currency,
            source=source,
            equity_try=e0 if self.settings.currency.upper() == "TRY" else None,
            note=(
                "Seeded from STARTING_CAPITAL_TRY / CAPITAL_FLOOR_TRY "
                "(örnek / seed — gerçek kasa değil; example seed, not live cash)."
            ),
        )

    def resolve(
        self,
        *,
        force_reset: bool | None = None,
        mexc_payload: dict[str, Any] | list[Any] | None = None,
        prefer_mexc: bool | None = None,
    ) -> EquityState:
        reset = self.settings.equity_reset if force_reset is None else force_reset
        if reset:
            state = self.seed_state(source=EquitySource.RESET)
            state.note = "Explicit equity reset — re-seeded from env defaults."
            self.store.clear()
            self._current = self.store.save(state)
            return self._current

        existing = self.store.load_latest()
        use_mexc = self.settings.mexc_api_key_configured if prefer_mexc is None else prefer_mexc
        if use_mexc:
            synced = self._sync_from_mexc(existing, payload=mexc_payload)
            if synced is not None:
                self._current = self.store.save(synced)
                return self._current

        if existing is not None:
            # Mark restored paper snapshots clearly when source was seed historically.
            if existing.source == EquitySource.SEED:
                existing = EquityState(
                    equity=existing.equity,
                    starting_capital=existing.starting_capital,
                    capital_floor=existing.capital_floor,
                    currency=existing.currency,
                    source=EquitySource.PAPER_DB,
                    equity_try=existing.equity_try,
                    equity_usdt=existing.equity_usdt,
                    usdt_try_rate=existing.usdt_try_rate,
                    note="Restored last paper equity snapshot (env seed not reapplied).",
                    extra=existing.extra,
                )
            self._current = existing
            return existing

        state = self.seed_state()
        self._current = self.store.save(state)
        return self._current

    def persist_paper_equity(
        self,
        equity: float,
        *,
        starting_capital: float | None = None,
        capital_floor: float | None = None,
        note: str = "Persisted paper broker equity / positions PnL basis.",
    ) -> EquityState:
        base = self._current or self.store.load_latest() or self.seed_state()
        state = EquityState(
            equity=float(equity),
            starting_capital=float(
                starting_capital if starting_capital is not None else base.starting_capital
            ),
            capital_floor=float(
                capital_floor if capital_floor is not None else base.capital_floor
            ),
            currency=base.currency,
            source=EquitySource.PAPER_DB,
            equity_try=float(equity) if base.currency.upper() == "TRY" else base.equity_try,
            equity_usdt=base.equity_usdt,
            usdt_try_rate=base.usdt_try_rate,
            note=note,
            extra=dict(base.extra),
        )
        self._current = self.store.save(state)
        return self._current

    def apply_deposit(self, amount: float, *, raise_floor: bool = True) -> EquityState:
        """Operator adds funds → update deposit basis E0 (and floor F by default)."""
        if amount <= 0:
            raise ValueError("deposit amount must be positive")
        current = self.resolve(prefer_mexc=False)
        new_e0 = current.starting_capital + float(amount)
        new_floor = (current.capital_floor + float(amount)) if raise_floor else current.capital_floor
        state = EquityState(
            equity=current.equity + float(amount),
            starting_capital=new_e0,
            capital_floor=new_floor,
            currency=current.currency,
            source=EquitySource.DEPOSIT_API,
            equity_try=(current.equity + float(amount))
            if current.currency.upper() == "TRY"
            else current.equity_try,
            equity_usdt=current.equity_usdt,
            usdt_try_rate=current.usdt_try_rate,
            note=f"Operator deposit +{amount:g}; E0→{new_e0:g} F→{new_floor:g}.",
            extra={**current.extra, "last_deposit": float(amount)},
        )
        self._current = self.store.save(state)
        return self._current

    def set_floor(self, capital_floor: float) -> EquityState:
        if capital_floor < 0:
            raise ValueError("capital_floor must be >= 0")
        current = self.resolve(prefer_mexc=False)
        if capital_floor < current.capital_floor:
            # Soften only with explicit API call (conscious); still audited via note.
            note = (
                f"Floor lowered {current.capital_floor:g}→{capital_floor:g} "
                "(conscious settings update; audit retained in snapshot)."
            )
        else:
            note = f"Floor set to {capital_floor:g}."
        state = EquityState(
            equity=current.equity,
            starting_capital=current.starting_capital,
            capital_floor=float(capital_floor),
            currency=current.currency,
            source=EquitySource.DEPOSIT_API,
            equity_try=current.equity_try,
            equity_usdt=current.equity_usdt,
            usdt_try_rate=current.usdt_try_rate,
            note=note,
            extra=dict(current.extra),
        )
        self._current = self.store.save(state)
        return self._current

    def _sync_from_mexc(
        self,
        existing: EquityState | None,
        *,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> EquityState | None:
        view = self._fetch_mexc_view(payload=payload)
        if view is None or not view.has_balance:
            return None
        e0 = existing.starting_capital if existing else float(self.settings.starting_capital_try)
        floor = existing.capital_floor if existing else float(self.settings.capital_floor_try)
        # First observation of MEXC equity initializes deposit basis / floor when empty.
        if existing is None:
            e0 = view.equity_display
            floor = view.equity_display
        return EquityState(
            equity=view.equity_display,
            starting_capital=e0,
            capital_floor=floor if floor > 0 else e0,
            currency=view.display_currency,
            source=EquitySource.MEXC_READONLY,
            equity_try=view.available_try
            if view.available_try > 0
            else (view.equity_display if view.display_currency == "TRY" else None),
            equity_usdt=view.available_usdt if view.available_usdt > 0 else None,
            usdt_try_rate=view.usdt_try_rate,
            note=(
                "Equity view synced from MEXC read-only available balance. "
                "Orders remain PAPER while LIVE_TRADING_ENABLED=false."
            ),
            extra={
                "available_usdt": view.available_usdt,
                "available_try": view.available_try,
                "display_currency": view.display_currency,
            },
        )

    def _fetch_mexc_view(
        self,
        *,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> MexcBalanceView | None:
        data = payload
        if data is None and self.mexc is not None:
            data = self.mexc.get_account_assets()
        if data is None:
            return None
        # Stub with empty assets → no sync
        if isinstance(data, dict) and data.get("source") == "stub" and not data.get("assets"):
            return None
        return parse_mexc_assets(
            data,
            display_currency=self.settings.currency,
            usdt_try_rate=self.settings.usdt_try_rate,
        )
