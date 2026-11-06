"""Persistence for equity snapshots (Postgres via SQLAlchemy; in-memory for tests).

Optional Redis cache sits in front when a client is provided — SQL remains source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import create_engine, desc, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading.equity.state import EquitySource, EquityState
from trading.models.persistence import Base, EquitySnapshot


class EquityStore(Protocol):
    def load_latest(self) -> EquityState | None: ...

    def save(self, state: EquityState) -> EquityState: ...

    def clear(self) -> None: ...


def _row_to_state(row: EquitySnapshot) -> EquityState:
    extra = dict(row.extra or {})
    return EquityState(
        equity=float(row.equity),
        starting_capital=float(row.starting_capital),
        capital_floor=float(row.capital_floor),
        currency=row.currency or "TRY",
        source=EquitySource(extra.get("source", EquitySource.PAPER_DB.value)),
        equity_try=extra.get("equity_try"),
        equity_usdt=extra.get("equity_usdt"),
        usdt_try_rate=extra.get("usdt_try_rate"),
        note=str(extra.get("note") or ""),
        extra={k: v for k, v in extra.items() if k not in {"source", "note", "equity_try", "equity_usdt", "usdt_try_rate"}},
    )


def _state_extra(state: EquityState) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": state.source.value,
        "note": state.note,
        **state.extra,
    }
    if state.equity_try is not None:
        payload["equity_try"] = state.equity_try
    if state.equity_usdt is not None:
        payload["equity_usdt"] = state.equity_usdt
    if state.usdt_try_rate is not None:
        payload["usdt_try_rate"] = state.usdt_try_rate
    return payload


@dataclass
class InMemoryEquityStore:
    """Test / process-local store. Survives within one process until cleared."""

    _latest: EquityState | None = None
    _history: list[EquityState] = field(default_factory=list)

    def load_latest(self) -> EquityState | None:
        return self._latest

    def save(self, state: EquityState) -> EquityState:
        self._latest = state
        self._history.append(state)
        return state

    def clear(self) -> None:
        self._latest = None
        self._history.clear()


@dataclass
class SqlAlchemyEquityStore:
    """Persists to ``equity_snapshots``. Engine URL typically from DATABASE_URL."""

    engine: Engine
    _session_factory: sessionmaker[Session] = field(init=False)

    def __post_init__(self) -> None:
        Base.metadata.create_all(self.engine, tables=[EquitySnapshot.__table__])
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, database_url: str) -> SqlAlchemyEquityStore:
        # Tests may pass sqlite+pysqlite:///:memory:
        connect_args: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        elif "postgresql" in database_url or database_url.startswith("postgres"):
            # Fail fast when Postgres is down (unit tests / offline hosts).
            connect_args["connect_timeout"] = 2
        engine = create_engine(database_url, future=True, connect_args=connect_args)
        return cls(engine=engine)

    def load_latest(self) -> EquityState | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(EquitySnapshot).order_by(desc(EquitySnapshot.id)).limit(1)
            )
            if row is None:
                return None
            return _row_to_state(row)

    def save(self, state: EquityState) -> EquityState:
        with self._session_factory() as session:
            row = EquitySnapshot(
                equity=float(state.equity),
                currency=state.currency,
                capital_floor=float(state.capital_floor),
                starting_capital=float(state.starting_capital),
                broker="paper",
                extra=_state_extra(state),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_state(row)

    def clear(self) -> None:
        with self._session_factory() as session:
            for row in session.scalars(select(EquitySnapshot)).all():
                session.delete(row)
            session.commit()


@dataclass
class RedisCachedEquityStore:
    """Optional Redis mirror of the latest snapshot; SQL (or memory) is authoritative."""

    inner: EquityStore
    redis_client: Any
    key: str = "mexc-ai:equity:latest"
    ttl_seconds: int = 86_400

    def load_latest(self) -> EquityState | None:
        try:
            raw = self.redis_client.get(self.key)
            if raw:
                data = json.loads(raw)
                return EquityState(
                    equity=float(data["equity"]),
                    starting_capital=float(data["starting_capital"]),
                    capital_floor=float(data["capital_floor"]),
                    currency=str(data.get("currency") or "TRY"),
                    source=EquitySource(data.get("source", EquitySource.PAPER_DB.value)),
                    equity_try=data.get("equity_try"),
                    equity_usdt=data.get("equity_usdt"),
                    usdt_try_rate=data.get("usdt_try_rate"),
                    note=str(data.get("note") or ""),
                    extra=dict(data.get("extra") or {}),
                )
        except Exception:  # noqa: BLE001 — cache miss / redis down → fall through
            pass
        state = self.inner.load_latest()
        if state is not None:
            self._cache(state)
        return state

    def save(self, state: EquityState) -> EquityState:
        saved = self.inner.save(state)
        self._cache(saved)
        return saved

    def clear(self) -> None:
        self.inner.clear()
        try:
            self.redis_client.delete(self.key)
        except Exception:  # noqa: BLE001
            pass

    def _cache(self, state: EquityState) -> None:
        try:
            payload = state.to_public_dict()
            payload.pop("seed_only_env_note", None)
            self.redis_client.set(self.key, json.dumps(payload), ex=self.ttl_seconds)
        except Exception:  # noqa: BLE001
            pass
