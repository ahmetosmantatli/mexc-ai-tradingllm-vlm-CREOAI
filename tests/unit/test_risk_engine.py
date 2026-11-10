"""Risk engine: reject without SL; max-loss / residual / floor fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trading.config import RiskProfile, Settings
from trading.models.domain import DecisionAction, Side, StopLossSpec, TradeProposal
from trading.risk.engine import RiskContext, RiskEngine
from trading.risk.presets import get_preset


def _proposal(**overrides: object) -> TradeProposal:
    base = dict(
        symbol="BTC_USDT",
        side=Side.LONG,
        leverage=10.0,
        notional=100.0,
        entry_price=50_000.0,
        stop_loss=StopLossSpec(percent_from_entry=0.02),
        max_loss_if_stopped=21.0,  # ~3% of 700
    )
    base.update(overrides)
    return TradeProposal(**base)  # type: ignore[arg-type]


def _context(**overrides: object) -> RiskContext:
    base = dict(
        equity=700.0,
        starting_capital=700.0,
        capital_floor=700.0,
        open_risk=0.0,
        open_positions=0,
        always_on_scan=True,
        floor_activated=False,
    )
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


def test_reject_without_stop_loss_schema() -> None:
    with pytest.raises(ValidationError):
        TradeProposal(
            symbol="ETH_USDT",
            side=Side.LONG,
            leverage=5.0,
            notional=50.0,
            stop_loss=StopLossSpec(),  # neither price nor percent
            max_loss_if_stopped=10.0,
        )


def test_risk_rejects_missing_stop_via_engine_path() -> None:
    """If stop fields are somehow empty after construction helpers, engine rejects."""
    engine = RiskEngine(
        settings=Settings(_env_file=None),
        preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
    )
    # Construct a valid proposal then clear stop fields via model_copy bypassing validator
    proposal = _proposal()
    broken = proposal.model_copy(update={"stop_loss": StopLossSpec(price=1.0)})
    # Force invalid by replacing with object missing both — use object.__setattr__ path
    # via model_construct (skips validation)
    broken = TradeProposal.model_construct(
        proposal_id=proposal.proposal_id,
        symbol="BTC_USDT",
        side=Side.LONG,
        leverage=10.0,
        notional=100.0,
        entry_price=50_000.0,
        stop_loss=StopLossSpec.model_construct(price=None, percent_from_entry=None),
        max_loss_if_stopped=21.0,
        take_profit_price=None,
        expected_edge=None,
        fee_free_pair=False,
        rationale="",
        metadata={},
    )
    decision = engine.evaluate(broken, _context())
    assert decision.action == DecisionAction.REJECT
    assert "MISSING_STOP_LOSS" in decision.reason_codes


def test_peak_drawdown_does_not_reject_while_daily_loss_still_does() -> None:
    engine = RiskEngine(
        settings=Settings(_env_file=None),
        preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
    )
    opened = engine.evaluate(_proposal(), _context(drawdown_pct=0.90))
    assert opened.accepted
    assert opened.reason_codes == ["OK"]
    halted = engine.evaluate(_proposal(), _context(drawdown_pct=0.90, daily_loss_pct=0.055))
    assert "DAILY_LOSS_HALT" in halted.reason_codes
    assert "MAX_DRAWDOWN_KILL" not in halted.reason_codes


def test_accept_reasonable_ag_proposal() -> None:
    engine = RiskEngine(
        settings=Settings(_env_file=None),
        preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
    )
    decision = engine.evaluate(_proposal(), _context())
    assert decision.accepted
    assert decision.reason_codes == ["OK"]
    assert "always_on_scan=True" in decision.notes


def test_reject_single_trade_wipe() -> None:
    engine = RiskEngine(
        settings=Settings(_env_file=None),
        preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
    )
    decision = engine.evaluate(
        _proposal(max_loss_if_stopped=700.0),
        _context(equity=700.0),
    )
    assert decision.action == DecisionAction.REJECT
    assert "SINGLE_TRADE_WIPE_FORBIDDEN" in decision.reason_codes


def test_reject_residual_too_low() -> None:
    engine = RiskEngine(
        settings=Settings(_env_file=None),
        preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
    )
    # 40% residual min of 700 = 280; loss 500 leaves 200 → reject
    decision = engine.evaluate(
        _proposal(max_loss_if_stopped=500.0),
        _context(equity=700.0),
    )
    assert decision.action == DecisionAction.REJECT
    assert "RESIDUAL_CAPITAL_TOO_LOW" in decision.reason_codes


def test_capital_floor_threat_when_activated() -> None:
    engine = RiskEngine(
        settings=Settings(_env_file=None),
        preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
    )
    decision = engine.evaluate(
        _proposal(max_loss_if_stopped=21.0),
        _context(
            equity=720.0,
            capital_floor=700.0,
            floor_activated=True,
        ),
    )
    assert decision.action == DecisionAction.REJECT
    assert "CAPITAL_FLOOR_THREAT" in decision.reason_codes


def test_preset_always_on_scan_flag_for_ag() -> None:
    preset = get_preset(RiskProfile.AGGRESSIVE_GROWTH)
    assert preset.always_on_scan is True
    assert preset.stop_required is True
    assert preset.capital_floor_enabled is True


def test_aggressive_runner3_checks_single_stop_separately_from_existing_open_risk():
    engine = RiskEngine(settings=Settings(_env_file=None), preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH))
    context = _context(equity=80., open_risk=7., aggressive_model_sizing=True)
    assert engine.evaluate(_proposal(max_loss_if_stopped=8.), context).accepted
    assert 'MAX_LOSS_EXCEEDS_CAP' in engine.evaluate(_proposal(max_loss_if_stopped=8.01), context).reason_codes
    context.aggressive_model_sizing = False
    assert 'TOTAL_OPEN_RISK_CAP' in engine.evaluate(_proposal(max_loss_if_stopped=8.), context).reason_codes
