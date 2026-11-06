"""Equity seed vs live: env used only when no snapshot; sizing uses live E."""

from __future__ import annotations

import pytest

from trading.agents.graph import size_proposal_from_signal
from trading.config import Settings
from trading.equity.mexc_balance import parse_mexc_assets
from trading.equity.service import EquityService
from trading.equity.sizing import max_loss_budget, size_from_equity
from trading.equity.state import EquitySource, EquityState
from trading.equity.store import InMemoryEquityStore, SqlAlchemyEquityStore
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal, StrategyName


def _settings(**overrides: object) -> Settings:
    base = dict(
        _env_file=None,
        starting_capital_try=700.0,
        capital_floor_try=700.0,
        equity_reset=False,
        currency="TRY",
        mexc_api_key="",
        mexc_api_secret="",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _signal() -> CandidateSignal:
    return CandidateSignal(
        symbol="BTC_USDT",
        side=Side.LONG,
        strategy=StrategyName.TREND,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_price=104.0,
        confidence=0.8,
    )


def test_env_seed_used_only_when_store_empty() -> None:
    store = InMemoryEquityStore()
    svc = EquityService(settings=_settings(starting_capital_try=700.0), store=store)
    first = svc.resolve()
    assert first.equity == 700.0
    assert first.source == EquitySource.SEED
    assert first.capital_floor == 700.0

    # Simulate paper PnL away from the env example.
    svc.persist_paper_equity(540.0)
    # Env still says 700 — resolve must restore 540, not re-seed.
    svc2 = EquityService(
        settings=_settings(starting_capital_try=700.0, capital_floor_try=700.0),
        store=store,
    )
    restored = svc2.resolve()
    assert restored.equity == 540.0
    assert restored.source == EquitySource.PAPER_DB
    assert restored.equity != svc2.settings.starting_capital_try


def test_explicit_reset_reseeds_from_env() -> None:
    store = InMemoryEquityStore()
    svc = EquityService(settings=_settings(), store=store)
    svc.resolve()
    svc.persist_paper_equity(420.0)
    reset = svc.resolve(force_reset=True)
    assert reset.equity == 700.0
    assert reset.source == EquitySource.RESET
    assert store.load_latest() is not None


def test_deposit_updates_e0_and_floor() -> None:
    store = InMemoryEquityStore()
    svc = EquityService(
        settings=_settings(starting_capital_try=500.0, capital_floor_try=500.0),
        store=store,
    )
    svc.resolve()
    after = svc.apply_deposit(300.0)
    assert after.starting_capital == 800.0
    assert after.capital_floor == 800.0
    assert after.equity == 800.0
    assert after.source == EquitySource.DEPOSIT_API


def test_sizing_uses_live_equity_object_not_env_constant() -> None:
    settings = _settings(starting_capital_try=700.0, risk_per_trade_pct=0.03)
    live = EquityState(
        equity=500.0,
        starting_capital=500.0,
        capital_floor=500.0,
        source=EquitySource.PAPER_DB,
    )
    sized = size_from_equity(
        live,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        stop_distance_pct=0.02,
    )
    assert sized["equity"] == 500.0
    assert sized["max_loss_budget"] == pytest.approx(15.0)
    # Env seed 700 would have given 21 — prove we did not use it.
    assert max_loss_budget(700.0, 0.03) == pytest.approx(21.0)
    assert sized["max_loss_budget"] != max_loss_budget(
        settings.starting_capital_try, 0.03
    )

    proposal = size_proposal_from_signal(
        _signal(),
        equity=live,
        risk_per_trade_pct=0.03,
        target_leverage=10.0,
    )
    assert proposal.metadata["sized_from_equity"] == 500.0
    assert proposal.max_loss_if_stopped == pytest.approx(15.0)


def test_mexc_readonly_balance_prefers_available() -> None:
    store = InMemoryEquityStore()
    settings = _settings(
        starting_capital_try=700.0,
        mexc_api_key="readonly-key",
        mexc_api_secret="readonly-secret",
        usdt_try_rate=34.0,
        currency="TRY",
    )
    svc = EquityService(settings=settings, store=store)
    payload = {
        "assets": [
            {"currency": "USDT", "availableBalance": 20.0},
            {"currency": "TRY", "availableBalance": 100.0},
        ]
    }
    state = svc.resolve(mexc_payload=payload, prefer_mexc=True)
    assert state.source == EquitySource.MEXC_READONLY
    # 100 TRY + 20*34 USDT = 780 TRY display
    assert state.equity == pytest.approx(780.0)
    assert state.equity_usdt == pytest.approx(20.0)
    assert "paper" in state.note.lower()


def test_parse_mexc_assets_try_usdt_display() -> None:
    view = parse_mexc_assets(
        {"assets": [{"coin": "USDT", "available": 10}]},
        display_currency="TRY",
        usdt_try_rate=30.0,
    )
    assert view.available_usdt == 10.0
    assert view.equity_display == pytest.approx(300.0)
    assert view.display_currency == "TRY"


def test_sqlalchemy_sqlite_roundtrip_ignores_env_on_restore() -> None:
    store = SqlAlchemyEquityStore.from_url("sqlite+pysqlite:///:memory:")
    svc = EquityService(settings=_settings(starting_capital_try=700.0), store=store)
    svc.resolve()
    svc.persist_paper_equity(655.5, starting_capital=700.0, capital_floor=700.0)

    svc_b = EquityService(
        settings=_settings(starting_capital_try=700.0),
        store=store,
    )
    restored = svc_b.resolve()
    assert restored.equity == 655.5
    assert restored.source == EquitySource.PAPER_DB
