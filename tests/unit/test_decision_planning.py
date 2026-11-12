import math

import pytest

from trading.evaluation.planning import PlanSizing, PlanSizingError, apply_decision_sizing
from trading.exchange.contracts import ContractRules
from trading.quant.signals import CandidateSignal
from trading.runtime.paper import PaperLedger
from trading.runtime.store import RuntimeStore


def candidate(**overrides):
    return CandidateSignal(**dict(dict(symbol='BTC_USDT', side='long', strategy='trend',
        entry_price=100., stop_loss_price=98., take_profit_price=108., confidence=.8,
        rationale='confirmed setup'), **overrides))


def size(plan=None, **overrides):
    return apply_decision_sizing(**dict(dict(candidate=candidate(), decisionplan=plan,
        equity=1000., policy_risk=.03, policy_leverage=10., exchange_cap=50.), **overrides))


def rules(**overrides):
    return ContractRules(**dict(dict(size=.001, step=1., minimum=1., maximum=1e9,
        max_leverage=100., maintenance=.005, price_step=.01), **overrides))


def test_model_selects_lower_risk_and_leverage_without_changing_setup():
    signal = candidate()
    result = size({'leverage': 4, 'risk_per_trade_pct': .005}, candidate=signal)
    assert result.notional == pytest.approx(250.)
    assert result.max_loss_if_stopped == 5
    assert result.leverage == 4
    assert (result.symbol, result.side, result.entry_price, result.stop_loss.price,
            result.take_profit_price, result.rationale) == (
        signal.symbol, signal.side, signal.entry_price, signal.stop_loss_price,
        signal.take_profit_price, signal.rationale)
    assert result.metadata['signal_id'] == signal.signal_id
    assert result.metadata['sizing_source'] == 'model'


def test_omitted_plan_keeps_policy_sizing_and_current_exchange_cap():
    result = size(exchange_cap=5.)
    assert result.leverage == 5
    assert result.max_loss_if_stopped == 30
    assert result.notional == 1500
    assert result.metadata['sizing_source'] == 'policy'


def test_sizing_reapplied_from_fresh_equity():
    plan = PlanSizing(leverage=5, risk_per_trade_pct=.01)
    before, after = size(plan), size(plan, equity=500.)
    assert before.notional == 2*after.notional
    assert after.max_loss_if_stopped == 5.


@pytest.mark.parametrize('plan', [
    {'leverage': 11, 'risk_per_trade_pct': .01},
    {'leverage': 5, 'risk_per_trade_pct': .031},
    {'leverage': 0, 'risk_per_trade_pct': .01},
    {'leverage': 5.5, 'risk_per_trade_pct': .01},
    {'leverage': True, 'risk_per_trade_pct': .01},
    {'leverage': '5', 'risk_per_trade_pct': .01},
    {'leverage': 5, 'risk_per_trade_pct': float('nan')},
    {'leverage': 5, 'risk_per_trade_pct': 0},
    {'leverage': 5, 'risk_per_trade_pct': True},
    {'leverage': 5, 'risk_per_trade_pct': .01, 'symbol': 'OTHER_USDT'},
    {'leverage': 5, 'risk_per_trade_pct': .01, 'entry_price': 95},
    {'leverage': 5, 'risk_per_trade_pct': .01, 'amount': 500},
])
def test_invalid_plan_rejected_never_clamped(plan):
    with pytest.raises(PlanSizingError):
        size(plan)


def test_exchange_cap_is_not_permission_to_bypass_policy():
    with pytest.raises(PlanSizingError, match='exceeds_limit'):
        size({'leverage': 50, 'risk_per_trade_pct': .01})
    with pytest.raises(PlanSizingError, match='exceeds_limit'):
        size({'leverage': 5, 'risk_per_trade_pct': .01}, exchange_cap=3)


@pytest.mark.parametrize('overrides', [dict(equity=0), dict(equity=-5), dict(equity=math.inf),
    dict(policy_risk=0), dict(policy_risk=math.nan), dict(policy_leverage=0), dict(exchange_cap=0),
    dict(candidate=candidate(stop_loss_price=102.)), dict(candidate=candidate(take_profit_price=99.))])
def test_invalid_account_limits_and_setup_fail_closed(overrides):
    with pytest.raises(PlanSizingError):
        size(**overrides)


def test_instance_revalidated_instead_of_trusting_model_construct():
    with pytest.raises(PlanSizingError):
        size(PlanSizing.model_construct(leverage=500, risk_per_trade_pct=-.01))


def test_short_keeps_levels_and_no_hidden_twenty_five_leverage_clamp():
    result = size({'leverage': 30, 'risk_per_trade_pct': .01}, policy_leverage=40,
        candidate=candidate(side='short', stop_loss_price=102, take_profit_price=94))
    assert result.leverage == 30
    assert result.stop_loss.price == 102
    assert result.notional == 500


def test_paper_gate_still_accounts_for_fees_slippage_and_rounds_down():
    ledger = PaperLedger(RuntimeStore(':memory:'), 1000.)
    proposal = size({'leverage': 4, 'risk_per_trade_pct': .01})
    position = ledger.open(proposal, rules(), 99.99, 100., 's1', .03, .01)
    assert position['notional'] < proposal.notional
    assert position['stop_loss_estimate'] <= proposal.max_loss_if_stopped + 1e-8
    assert position['leverage'] == 4


def test_paper_gate_rejects_liquidation_unsafe_plan_despite_allowed_exchange_leverage():
    proposal = size({'leverage': 50, 'risk_per_trade_pct': .01}, policy_leverage=50)
    ledger = PaperLedger(RuntimeStore(':memory:'), 1000.)
    with pytest.raises(ValueError, match='liquidation'):
        ledger.open(proposal, rules(), 99.99, 100., 's1', .03, .01)
    assert not ledger.positions


def test_omitted_risk_share_uses_equity_sizing_inside_the_cap_and_a_present_share_stays():
    omitted = size({'leverage': 5})
    assert omitted.metadata['risk_per_trade_pct'] == pytest.approx(.03)
    assert omitted.metadata['risk_per_trade_pct'] <= .15
    assert omitted.metadata['sizing_plan']['risk_per_trade_pct'] == pytest.approx(.03)
    assert omitted.notional == pytest.approx(1500.)
    assert omitted.leverage == 5
    capped = size({'leverage': 4}, policy_risk=.20)
    assert capped.metadata['risk_per_trade_pct'] == pytest.approx(.15)
    assert capped.max_loss_if_stopped == pytest.approx(150.)
    present = size({'leverage': 4, 'risk_per_trade_pct': .01}, policy_risk=.03)
    assert present.metadata['risk_per_trade_pct'] == pytest.approx(.01)
    assert present.notional == pytest.approx(500.)


def test_margin_share_does_not_change_size_and_an_unusable_share_stays_empty():
    kept = size({'leverage': 4, 'risk_per_trade_pct': .005, 'margin_share': .22})
    assert kept.notional == pytest.approx(250.)
    assert kept.stop_loss.price == 98
    assert kept.metadata['sizing_plan']['margin_share'] == pytest.approx(.22)
    for raw in ('high', 2, True, float('nan'), 0):
        result = size({'leverage': 4, 'risk_per_trade_pct': .005, 'margin_share': raw})
        assert result.notional == pytest.approx(250.)
        assert result.stop_loss.price == 98
        assert 'margin_share' not in result.metadata['sizing_plan']


def test_paper_gate_rejects_tiny_amount_never_increases_to_minimum():
    proposal = size({'leverage': 2, 'risk_per_trade_pct': .00000001})
    ledger = PaperLedger(RuntimeStore(':memory:'), 1000.)
    with pytest.raises(ValueError, match='contract quantity below exchange minimum'):
        ledger.open(proposal, rules(minimum=10), 99.99, 100., 's1', .03, .01)
    assert not ledger.positions
    assert ledger.equity == 1000.


def test_fee_aware_continuous_sizing_clears_entry_veto_at_fifteen_percent():
    """~8 USDT continuous size-to-ceiling must use fee+slip unit loss, not raw stop%."""
    from trading.evaluation.planning import FEE_AWARE_SIZE_HAIRCUT
    from trading.runtime.runner3_policy import (
        CONTINUOUS_STOP_RISK_CAP, entry_veto, ioc_target_economics)

    equity = 8.
    fee, slip = 0.0006, 0.0005
    signal = candidate(stop_loss_price=98., take_profit_price=108.)
    raw = size(equity=equity, policy_risk=CONTINUOUS_STOP_RISK_CAP)
    assert entry_veto(
        side='long', entry=100., stop=98., target=108., equity=equity, free_margin=equity,
        fee=fee, slippage=slip, notional=raw.notional, leverage=raw.leverage) == 'equity_stop'

    sized = size(equity=equity, policy_risk=CONTINUOUS_STOP_RISK_CAP, fee=fee, slippage=slip)
    assert sized.metadata['fee_aware_sizing'] is True
    _g, _n, unit = ioc_target_economics(
        side='long', entry=100., stop=98., target=108., fee=fee, slippage=slip)
    expected = equity * CONTINUOUS_STOP_RISK_CAP * 100. / unit * FEE_AWARE_SIZE_HAIRCUT
    assert sized.notional == pytest.approx(expected)
    assert sized.notional < raw.notional
    modeled = unit * sized.notional / 100.
    assert modeled <= equity * CONTINUOUS_STOP_RISK_CAP + 1e-8
    assert sized.max_loss_if_stopped == pytest.approx(modeled)
    assert entry_veto(
        side='long', entry=100., stop=98., target=108., equity=equity, free_margin=equity,
        fee=fee, slippage=slip, notional=sized.notional, leverage=sized.leverage) is None


def test_fee_aware_sizing_still_rejects_min_contract_over_equity_stop():
    """Min-contract notional that still exceeds the 15% fee+slip cap must REJECT."""
    from trading.runtime.runner3_policy import CONTINUOUS_STOP_RISK_CAP, entry_veto

    equity = 8.
    fee, slip = 0.0006, 0.0005
    # Wide structural stop (~5%) + min-contract style notional (~40 USDT) >> 15% of ~8.
    min_contract_notional = 40.
    assert entry_veto(
        side='long', entry=100., stop=95., target=115., equity=equity, free_margin=equity,
        fee=fee, slippage=slip, notional=min_contract_notional, leverage=10) == 'equity_stop'
    # Fee-aware size for the same levels stays under the cap when physics allows.
    sized = size(
        equity=equity, policy_risk=CONTINUOUS_STOP_RISK_CAP, fee=fee, slippage=slip,
        candidate=candidate(stop_loss_price=95., take_profit_price=115.))
    assert sized.notional < min_contract_notional
    assert entry_veto(
        side='long', entry=100., stop=95., target=115., equity=equity, free_margin=equity,
        fee=fee, slippage=slip, notional=sized.notional, leverage=sized.leverage) is None
    # Do not invent a fill by bumping to min contract past the equity stop.
    assert entry_veto(
        side='long', entry=100., stop=95., target=115., equity=equity, free_margin=equity,
        fee=fee, slippage=slip, notional=min_contract_notional, leverage=10) == 'equity_stop'
