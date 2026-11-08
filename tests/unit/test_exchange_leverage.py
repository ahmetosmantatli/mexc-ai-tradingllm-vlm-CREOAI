from types import SimpleNamespace
from copy import deepcopy
import pytest
from trading.config import Settings
from trading.risk.engine import RiskEngine,RiskContext
from trading.models.domain import TradeProposal,StopLossSpec
from trading.exchange.contracts import ContractRules
from .test_live_runner import prepare,candidate,result
from .test_live_telegram import setup as telegram_setup,ready


def proposed(leverage):
    return TradeProposal(symbol='BTC_USDT',side='long',entry_price=100,notional=100,
        leverage=leverage,stop_loss=StopLossSpec(price=99.8),take_profit_price=102,max_loss_if_stopped=.3)


def test_live_exchange_mode_is_explicit_and_paper_profile_limit_is_not_bypassed():
    cfg=Settings(_env_file=None,broker_mode='live',live_leverage_mode='exchange',risk_profile='balanced',risk_per_trade_pct=.01)
    engine=RiskEngine(settings=cfg)
    context=RiskContext(equity=500,starting_capital=500,capital_floor=0,exchange_max_leverage=500)
    assert engine.evaluate(proposed(300),context).accepted
    context.exchange_max_leverage=200
    assert 'LEVERAGE_CAP' in engine.evaluate(proposed(300),context).reason_codes
    context.exchange_max_leverage=None
    assert 'UNVERIFIED_EXCHANGE_LEVERAGE_CAP' in engine.evaluate(proposed(300),context).reason_codes
    context.exchange_max_leverage=500
    engine.settings=cfg.model_copy(update={'broker_mode':'paper'})
    assert 'LEVERAGE_CAP' in engine.evaluate(proposed(300),context).reason_codes


@pytest.mark.parametrize('cap',[None,True,float('nan'),0])
def test_exchange_risk_limit_must_be_verified_numeric(cap):
    cfg=Settings(_env_file=None,broker_mode='live',live_leverage_mode='exchange')
    context=RiskContext(equity=500,starting_capital=500,capital_floor=0,exchange_max_leverage=cap)
    assert not RiskEngine(settings=cfg).evaluate(proposed(20),context).accepted


def test_runtime_uses_minimum_public_private_and_side_limit_not_saved_ten_x(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    runner.cfg.live_leverage_mode='exchange'
    runner.public.get_contract_rules=lambda s:ContractRules(.001,1,1,1e8,500,.005,.01,1e8)
    snapshot=runner._refresh(['BTC_USDT'])
    snapshot['risk_limits']['BTC_USDT'][0]['max_leverage']=400
    snapshot['leverage']['BTC_USDT'][0]['max_leverage']=300
    cap=runner._symbol_leverage_cap(candidate(),snapshot)
    control['max_leverage']=10
    assert cap==300 and runner._leverage_cap(control,cap)==300
    with pytest.raises(ValueError):runner._leverage_cap(control)
    value=result(runner,candidate())
    value.metadata['sizing_plan']={'leverage':20,'risk_per_trade_pct':.002}
    proposal=runner._final_proposal(candidate(),value,snapshot,control)
    assert proposal.leverage==20
    value.metadata['sizing_plan']=None
    with pytest.raises(ValueError,match='explicitly'):runner._final_proposal(candidate(),value,snapshot,control)


def test_model_receives_actual_account_limit_and_safety_context(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    runner.cfg.live_leverage_mode='exchange'
    captured={}
    runner.graph=SimpleNamespace(evaluate_candidates=lambda *a,**kw:captured.update(kw))
    runner._evaluate(candidate(),runner._refresh(['BTC_USDT']),control)
    assert captured['target_leverage']==20
    policy=captured['live_account_context']['leverage_policy']
    assert policy['account_exchange_max']==20 and policy['minimum_stop_liquidation_buffer_fraction']==.02


def test_exchange_mode_ui_does_not_claim_ten_x_cap(tmp_path):
    ctl,calls=telegram_setup(tmp_path,{'a':{'leverage_mode':'exchange'}})
    ready(ctl)
    text=ctl.settings_text('a')
    assert 'güncel borsa üst sınırı' in text
    assert 'Profil kaldıraç üst sınırı' not in text


def test_private_exchange_limit_does_not_bypass_stop_liquidation_protection(tmp_path):
    from .test_live_execution import setup,proposal,approve
    executor,private,store=setup(tmp_path)
    executor.risk=RiskEngine(settings=Settings(_env_file=None,broker_mode='live',live_leverage_mode='exchange',risk_profile='aggressive_growth',risk_per_trade_pct=.03))
    executor.public.get_contract_rules=lambda s:ContractRules(.001,1,1,1e8,500,.005,.01,1e8)
    oldrisk=private.risk_limits;oldlev=private.leverage
    def risks(symbol=None):
        data=oldrisk(symbol)
        for row in data[symbol]:row.update(maxLeverage=500,imr=.002)
        return data
    def levels(symbol):
        data=oldlev(symbol)
        for row in data:row.update(leverage=500,maxLeverageView=500)
        return data
    private.risk_limits=risks;private.leverage=levels
    intent=approve(executor,proposal(leverage=500))
    with pytest.raises(Exception,match='liquidation'):executor.execute(intent['intent_id'])
    assert not private.submits


def test_twenty_x_can_pass_full_executor_without_profile_cap_bypass_of_other_rules(tmp_path):
    from .test_live_execution import setup,proposal,approve
    executor,private,store=setup(tmp_path)
    executor.risk=RiskEngine(settings=Settings(_env_file=None,broker_mode='live',live_leverage_mode='exchange',risk_profile='aggressive_growth',risk_per_trade_pct=.03))
    executor.public.get_contract_rules=lambda s:ContractRules(.001,1,1,1e8,500,.005,.01,1e8)
    private.leverage_value=20
    risk_method=private.risk_limits;level_method=private.leverage
    def risks(symbol=None):
        data=risk_method(symbol)
        for row in data[symbol]:row['maxLeverage']=500
        return data
    def levels(symbol):
        data=level_method(symbol)
        for row in data:row['maxLeverageView']=500
        return data
    private.risk_limits=risks;private.leverage=levels
    intent=approve(executor,proposal(leverage=20))
    state=executor.execute(intent['intent_id'])
    assert state['status']=='protected' and state['proposal']['leverage']==20
    assert len(private.submits)==1
