from dataclasses import replace
import pytest
from trading.exchange.contracts import ContractRules
from trading.risk.adaptive import choose_plan, InfeasiblePlan


@pytest.fixture
def rules():
    return ContractRules(.001,1,1,10000000,100,.005,.01,10000000)


def test_exchange_max_is_not_selected_by_default(rules):
    plan=choose_plan(500,500,100,98,104,.01,.2,rules)
    assert plan.leverage==3
    assert plan.leverage<rules.max_leverage
    assert plan.margin+plan.entry_fee<=100
    assert plan.estimated_stop_loss<=5
    assert plan.research_only


def test_higher_exchange_ceiling_does_not_increase_risk(rules):
    a=choose_plan(500,500,100,98,104,.01,.2,rules)
    b=choose_plan(500,500,100,98,104,.01,.2,replace(rules,max_leverage=500))
    assert a.leverage==b.leverage
    assert a.notional==b.notional
    assert a.estimated_stop_loss==b.estimated_stop_loss


def test_current_equity_decline_scales_quantity(rules):
    a=choose_plan(500,500,100,98,104,.01,.2,rules)
    b=choose_plan(300,300,100,98,104,.01,.2,rules)
    assert b.contracts<a.contracts
    assert b.risk_budget==3
    assert b.estimated_stop_loss<=3


def test_margin_preference_changes_leverage_not_stop_sized_notional(rules):
    a=choose_plan(500,500,100,98,104,.01,.2,rules)
    b=choose_plan(500,500,100,98,104,.01,.5,rules)
    assert a.notional==b.notional
    assert b.leverage<a.leverage


def test_available_margin_is_respected(rules):
    plan=choose_plan(500,50,100,98,104,.01,.5,rules)
    assert plan.margin+plan.entry_fee<=50
    assert plan.leverage==5


def test_no_feasible_leverage_rejects_instead_of_using_max(rules):
    with pytest.raises(InfeasiblePlan,match='no leverage'):
        choose_plan(500,1,100,98,104,.01,.5,rules)
    with pytest.raises(InfeasiblePlan,match='no leverage'):
        choose_plan(500,500,100,98,104,.01,.1,replace(rules,max_leverage=2))


def test_short_setup_and_adverse_grid_rounding(rules):
    plan=choose_plan(500,500,100,102.001,96.001,.01,.2,rules)
    assert plan.side=='short'
    assert plan.stop==102.01
    assert plan.target==96.01
    assert plan.estimated_stop_fill>plan.stop
    assert plan.estimated_stop_loss<=5
    assert plan.stop_liquidation_buffer_pct>=.02


def test_base_risk_tier_and_minimum_contract_limits_fail_closed(rules):
    with pytest.raises(InfeasiblePlan,match='base-tier'):
        choose_plan(500,500,100,98,104,.01,.2,replace(rules,base_tier_max=1))
    with pytest.raises(InfeasiblePlan,match='base-tier'):
        choose_plan(.01,.01,100,98,104,.01,.2,rules)


@pytest.mark.parametrize('updates',[{'eq':float('nan')},{'risk_pct':0},{'risk_pct':1},{'requested_margin_fraction':1.1},{'stop':104},{'available':0}])
def test_bad_input_never_produces_plan(rules,updates):
    args=dict(eq=500,available=500,entry=100,stop=98,target=104,risk_pct=.01,requested_margin_fraction=.2,rules=rules)
    args.update(updates)
    with pytest.raises(InfeasiblePlan):choose_plan(**args)
