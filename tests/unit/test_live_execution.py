"""No real keys/network; validate exact intent, exchange confirmation and stops."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import threading
from concurrent.futures import ThreadPoolExecutor
from time import time

import pytest

from trading.brokers.live.execution import ApprovedLiveExecutor,LiveExecutionGates,LiveExecutionError
from trading.brokers.live.automatic import AutomaticLiveExecutor,AutomaticLivePolicy
from trading.exchange.contracts import ContractRules
from trading.models.domain import TradeProposal,StopLossSpec,Side
from trading.runtime.store import RuntimeStore


class Client:
    writes_enabled=True
    def __init__(self):
        self.assets=[dict(currency='USDT',equity=500.,availableBalance=500.,positionMargin=0.,unrealized=0.)]
        self.orders=[];self.positions=[];self.order_row=None;self.stops=[];self.history=[];self.close_order=None
        self.submits=[];self.protects=[];self.closes=[];self.cancels=[]
        self.submit_unknown=False;self.protection_fails=False;self.closed=False;self.fill_fraction=1.
        self.mmr=.005;self.leverage_value=5;self.protection_page2=False
        self.on_submit=None;self.on_assets=None
    def account_assets(self):
        if self.on_assets:self.on_assets()
        return deepcopy(self.assets)
    def open_positions(self,symbol=None):return deepcopy(self.positions)
    def current_orders(self,symbol=None,page_num=1,page_size=100):return deepcopy(self.orders)
    def position_mode(self):return 1
    def risk_limits(self,symbol=None):
        return {symbol:[dict(symbol=symbol,positionType=side,level=1,maxVol=1000,maxLeverage=10,mmr=self.mmr,imr=.1) for side in (1,2)]}
    def leverage(self,symbol):
        return [dict(positionType=side,openType=1,leverage=self.leverage_value,maxVol=1000,currentMmr=self.mmr,maxLeverageView=10) for side in (1,2)]
    def protection_orders(self,symbol=None,page_num=1,page_size=100):
        if self.protection_page2 and page_num==1:return [dict(positionId='unrelated')]*100
        return deepcopy(self.stops)
    def order_by_external_id(self,symbol,external_id):
        if external_id.startswith('close-'):return deepcopy(self.close_order)
        return deepcopy(self.order_row)
    def submit_market(self,*,symbol,side,contracts,leverage,external_id,stop_loss,take_profit,entry_price,position_mode=1):
        self.submits.append(external_id)
        if self.on_submit:self.on_submit()
        self.order_row=dict(symbol=symbol,side=1 if side=='long' else 3,openType=1,positionMode=position_mode,
            externalOid=external_id,positionId=11,orderId=22,vol=contracts,dealVol=contracts*self.fill_fraction,
            dealAvgPrice=100.,orderType=5,state=3 if self.fill_fraction==1 else 2)
        self.positions=[dict(symbol=symbol,positionType=1 if side=='long' else 2,positionId=11,holdVol=contracts*self.fill_fraction,
            state=1,openType=1,autoAddIm=0,leverage=leverage,holdAvgPrice=100.,im=20.,unRealizedPnl=0.,liquidatePrice=80.)]
        if self.submit_unknown:raise SimpleError(ambiguous=True)
        return {'orderId':22,'ts':time()*1000}
    def place_protection(self,*,position_id,contracts,stop_loss,take_profit,request_id):
        self.protects.append(request_id)
        if self.protection_fails:raise SimpleError(ambiguous=True)
        self.stops=[dict(positionId=position_id,state=1,isFinished=0,lossTrend=2,profitTrend=2,
            stopLossType=0,takeProfitType=0,stopLossReverse=2,takeProfitReverse=2,
            profitLossVolType='SAME',vol=contracts,stopLossPrice=stop_loss,takeProfitPrice=take_profit)]
        return {'id':33}
    def close_position(self,*,symbol,side,contracts,position_id,external_id,position_mode=1):
        self.closes.append(external_id)
        self.close_order=dict(symbol=symbol,externalOid=external_id,side=4 if side=='long' else 2,state=3,dealVol=contracts)
        self.positions=[]
        self.make_history(contracts)
        return {'orderId':44}
    def cancel_order(self,order_id):
        self.cancels.append(order_id)
        self.order_row['state']=4
        return []
    def historical_positions(self,symbol=None,page_num=1,page_size=100):return deepcopy(self.history)
    def make_history(self,contracts):
        self.history=[dict(positionId=11,symbol='BTC_USDT',positionType=1,state=3,holdVol=0,closeVol=contracts,
            realised=4.3,updateTime=time()*1000+1,closeProfitLoss=5.,totalFee=.6,holdFee=-.1)]


class SimpleError(Exception):
    def __init__(self,ambiguous):self.ambiguous=ambiguous


class Public:
    def get_contract_rules(self,symbol):return ContractRules(.001,1,1,1e8,10,.005,.01,1e8)
    def get_ticker(self,symbol):return SimpleNamespace(bid1=99.99,ask1=100.,raw={'timestamp':time()*1000})


def proposal(**changes):
    data=dict(symbol='BTC_USDT',side=Side.LONG,leverage=5,notional=100.,entry_price=100.,
        stop_loss=StopLossSpec(price=98.),take_profit_price=104.,max_loss_if_stopped=3.)
    data.update(changes)
    return TradeProposal(**data)


def setup(tmp_path,*,enabled=True,automatic=False,control=None):
    store=RuntimeStore(str(tmp_path/'live'))
    private=Client();public=Public()
    gates=LiveExecutionGates(live_trading_enabled=enabled,broker_mode='live',reviewed=True,operator_ids=('7',))
    risk=SimpleNamespace(evaluate=lambda *a:SimpleNamespace(accepted=True))
    kwargs=dict(account_id='ahmet',binding_id='binding-1')
    if automatic:
        control=control or {'enabled':True,'updated_at':1.}
        executor=AutomaticLiveExecutor(AutomaticLivePolicy(enabled=enabled,allowed_accounts=('ahmet',),operator_ids=('7',)),gates,store,private,public,risk,
            control_provider=lambda:dict(control),**kwargs)
        executor.grant_start('7',control['updated_at'])
    else:executor=ApprovedLiveExecutor(gates,store,private,public,risk,**kwargs)
    return executor,private,store


def approve(executor,p=None):
    intent=executor.stage(p or proposal())
    return executor.approve(intent['intent_id'],intent['proposal_fingerprint'],'7',allow_emergency_close=True)


def test_definite_entry_rejection_preserves_safe_exchange_diagnostics(tmp_path):
    from trading.exchange.mexc_private import MexcPrivateError
    executor,private,store=setup(tmp_path)
    intent=approve(executor)
    def reject(**kwargs):
        raise MexcPrivateError('submit_market',code=2001,status_code=200,ambiguous=False)
    private.submit_market=reject
    row=executor.execute(intent['intent_id'])
    assert row['status']=='entry_rejected'
    assert row['entry_rejection']['exchange_code']==2001
    assert row['entry_rejection']['http_status']==200
    assert row['entry_rejection']['stage']=='submit_market'
    assert executor.state(intent['intent_id'])['entry_rejection']==row['entry_rejection']
    assert not private.protects


def test_defaults_refuse_entry_and_no_paper_or_seed_fallback(tmp_path):
    executor,private,store=setup(tmp_path,enabled=False)
    intent=executor.stage(proposal())
    with pytest.raises(LiveExecutionError):executor.approve(intent['intent_id'],intent['proposal_fingerprint'],'7',allow_emergency_close=True)
    assert not private.submits and store.get('paper') is None


def test_exact_manual_approval_and_intent_durable_before_one_submit(tmp_path):
    executor,private,store=setup(tmp_path)
    intent=executor.stage(proposal())
    with pytest.raises(LiveExecutionError,match='approval_proposal_mismatch'):
        executor.approve(intent['intent_id'],'changed','7',allow_emergency_close=True)
    intent=approve(executor,TradeProposal.model_validate(intent['proposal']))
    def during_submit():
        persisted=executor.state(intent['intent_id'])
        assert persisted['entry_write_attempted'] and persisted['status']=='entry_write_unknown'
    private.on_submit=during_submit
    opened=executor.execute(intent['intent_id'])
    assert opened['status']=='protected' and opened['is_simulated'] is False
    executor.execute(intent['intent_id'])
    assert len(private.submits)==1 and len(private.protects)==1


def test_ambiguous_entry_reconciles_without_resubmit_across_restart(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    private.submit_unknown=True
    uncertain=executor.execute(intent['intent_id'])
    assert uncertain['status']=='entry_write_unknown'
    restarted=ApprovedLiveExecutor(executor.gates,store,private,executor.public,executor.risk,account_id='ahmet',binding_id='binding-1')
    result=restarted.reconcile(intent['intent_id'])
    assert result['status']=='protected' and len(private.submits)==1
    assert not store.get('live:halt')


def test_failed_native_protection_closes_identified_exposure_once(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor);private.protection_fails=True
    result=executor.execute(intent['intent_id'])
    assert result['status']=='close_write_unknown'
    assert len(private.protects)==1 and len(private.closes)==1
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='closed' and result['net_pnl']==4.3
    assert store.get('live:halt')['requires_review'] is True
    assert store.get('live:halt')['reason']=='emergency_close_requires_review'
    executor.reconcile(intent['intent_id'])
    assert len(private.submits)==1 and len(private.closes)==1
    next_intent=approve(executor,proposal())
    with pytest.raises(LiveExecutionError,match='live_halt_latched'):
        executor.execute(next_intent['intent_id'])
    assert len(private.submits)==1


def test_pause_blocks_new_entry_but_does_not_block_normal_close_reconciliation(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    private.positions=[];private.make_history(1000.)
    executor.can_execute=lambda:False
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='closed' and result['close_reason']=='EXCHANGE_CLOSE' and result['net_pnl']==4.3
    assert result['net_pnl_source']=='MEXC historical_positions.realised'


def test_partial_fill_cancel_is_not_entry_retry_and_then_native_protects_actual_volume(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor);private.fill_fraction=.5
    first=executor.execute(intent['intent_id'])
    assert first['status']=='partial_entry_cancel_unknown' and len(private.cancels)==1
    second=executor.reconcile(intent['intent_id'])
    assert second['status']=='protected' and second['effective_contracts']==500
    assert private.stops[0]['vol']==500
    assert len(private.submits)==1 and len(private.cancels)==1


def test_protection_reverse_or_missing_volume_semantics_are_not_verified(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    private.stops[0]['stopLossReverse']=1
    private.protection_fails=True
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='close_write_unknown' and len(private.closes)==1


def test_all_protection_pages_read_before_duplicate_protection_write(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id']);private.protection_page2=True
    assert executor.reconcile(intent['intent_id'])['status']=='protected'
    assert len(private.protects)==1


def test_binding_generation_rotation_cannot_execute_old_approval(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    rotated=ApprovedLiveExecutor(executor.gates,store,private,executor.public,executor.risk,account_id='ahmet',binding_id='binding-2')
    with pytest.raises(LiveExecutionError,match='credential_binding_changed'):rotated.execute(intent['intent_id'])
    assert not private.submits


def test_preflight_leverage_mismatch_or_private_mmr_halts_before_write(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor);private.leverage_value=4
    with pytest.raises(LiveExecutionError,match='private_preflight_rejected'):executor.execute(intent['intent_id'])
    assert not private.submits
    private.leverage_value=5;private.mmr=.19
    with pytest.raises(LiveExecutionError,match='insufficient_liquidation_buffer'):executor.execute(intent['intent_id'])
    assert not private.submits


def test_no_store_lock_held_during_exchange_network(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    def probe():
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(lambda:store.get('anything','readable')).result(timeout=1)=='readable'
    private.on_assets=probe
    assert executor.execute(intent['intent_id'])['status']=='protected'


def test_automatic_policy_grant_distinct_from_manual_approval_and_stale_control_rejected(tmp_path):
    control=dict(enabled=True,updated_at=1.)
    executor,private,store=setup(tmp_path,automatic=True,control=control)
    row=executor.authorize(proposal(),control_revision=1.)
    assert row['status']=='policy_authorized' and 'approved_by' not in row
    with pytest.raises(LiveExecutionError,match='never_fabricates'):executor.approve(row['intent_id'],'x','7')
    control['updated_at']=2.
    with pytest.raises(LiveExecutionError,match='grant_not_current'):executor.execute(row['intent_id'])
    assert not private.submits


def test_automatic_policy_executes_only_under_current_start_grant(tmp_path):
    executor,private,store=setup(tmp_path,automatic=True)
    row=executor.authorize(proposal(),control_revision=1.)
    assert executor.execute(row['intent_id'])['status']=='protected'
    executor.revoke_start()
    executor.can_execute=lambda:False
    assert executor.reconcile(row['intent_id'])['status']=='protected'


def test_unknown_protection_volume_type_does_not_duplicate_order(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    private.stops[0].pop('profitLossVolType')
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='close_write_unknown'
    assert len(private.protects)==1 and len(private.closes)==1


def test_attached_market_protection_without_stop_type_matches_documented_shape(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    def attached():
        private.stops=[dict(positionId=11,orderId=22,positionType=1,errorCode=0,
            lossTrend=2,profitTrend=2,stopLossPrice=98.,takeProfitPrice=104.,
            state=1,isFinished=0,profitLossVolType='SEPARATE',
            stopLossVol=1000.,takeProfitVol=1000.,vol=1000.,
            takeProfitType=0,stopLossReverse=2,takeProfitReverse=2)]
    private.on_submit=attached
    result=executor.execute(intent['intent_id'])
    assert result['status']=='protected'
    assert result['protection_verification']=='attached_market_entry_schema'
    assert result['entry_exchange_type']==5 and result['entry_submitted_type']==5
    assert 'type' not in private.order_row and private.order_row['orderType']==5
    assert not private.protects and not private.closes
    restarted=ApprovedLiveExecutor(executor.gates,store,private,executor.public,executor.risk,
        account_id='ahmet',binding_id='binding-1')
    assert restarted.reconcile(intent['intent_id'])['status']=='protected'
    assert len(private.submits)==1


@pytest.mark.parametrize('order,expected',[
    ({'orderType':5},5),({'type':5},5),({'orderType':5,'type':5},5),
    ({'orderType':5,'type':1},None),({'orderType':None},None),
    ({'orderType':'5'},None),({'orderType':True},None),
    ({'category':1},None),({},None)])
def test_entry_type_reads_exchange_response_without_guessing(order,expected):
    assert ApprovedLiveExecutor._entry_order_type(order)==expected


@pytest.mark.parametrize('fields',[{}, {'orderType':1}, {'orderType':5,'type':1}])
def test_local_submitted_market_type_does_not_override_exchange_evidence(tmp_path,fields):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    private.order_row.pop('orderType')
    private.order_row.update(fields)
    native=private.stops[0]
    native.pop('stopLossType')
    native.update(orderId=22,positionType=1,errorCode=0)
    result=executor.reconcile(intent['intent_id'])
    assert result['entry_submitted_type']==5
    assert result['status']=='close_write_unknown'
    assert len(private.closes)==1


@pytest.mark.parametrize('changes',[
    {'stopLossType':1},{'stopLossType':None},{'stopLossType':False},
    {'stopLossOrderPrice':97.},{'orderId':99},{'positionType':2},
    {'errorCode':8912},{'takeProfitType':1},{'takeProfitReverse':1},
    {'stopLossVol':999.},{'state':2},{'isFinished':1}])
def test_attached_missing_stop_type_never_hides_conflicting_protection(tmp_path,changes):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    native=private.stops[0]
    native.pop('stopLossType')
    native.update(orderId=22,positionType=1,errorCode=0,
        profitLossVolType='SEPARATE',stopLossVol=1000.,takeProfitVol=1000.)
    native.update(changes)
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='close_write_unknown'
    assert len(private.closes)==1


def test_unknown_stop_type_not_inferred_for_old_or_unconfirmed_entry(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    result=executor.execute(intent['intent_id'])
    private.stops[0].pop('stopLossType')
    private.stops[0].update(orderId=22,positionType=1,errorCode=0)
    result.pop('entry_protection_schema')
    executor._save(result)
    assert executor.reconcile(intent['intent_id'])['status']=='close_write_unknown'


def test_actual_filled_liquidation_risk_can_trigger_one_reduce_close(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    private.positions[0]['liquidatePrice']=97.5
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='close_write_unknown'
    assert len(private.submits)==1 and len(private.closes)==1


def test_real_private_client_contract_end_to_end_uses_no_network(tmp_path):
    import httpx,json
    from trading.exchange.mexc_private import MexcPrivateClient
    executor,fake,store=setup(tmp_path)
    paths=[]
    def handle(request):
        path=request.url.path;paths.append(path)
        if path.endswith('/account/assets'):data=fake.account_assets()
        elif path.endswith('/position/open_positions'):data=fake.open_positions(request.url.params.get('symbol'))
        elif path.endswith('/order/list/open_orders'):data=fake.current_orders()
        elif path.endswith('/position/position_mode'):data=fake.position_mode()
        elif path.endswith('/account/risk_limit'):data=fake.risk_limits(symbol=request.url.params['symbol'])
        elif path.endswith('/position/leverage'):data=fake.leverage(request.url.params['symbol'])
        elif '/order/external/' in path:data=fake.order_by_external_id('BTC_USDT',path.rsplit('/',1)[-1])
        elif path.endswith('/stoporder/list/orders'):data=fake.protection_orders('BTC_USDT',page_num=int(request.url.params['page_num']))
        elif path.endswith('/order/create'):
            body=json.loads(request.content)
            data=fake.submit_market(symbol=body['symbol'],side='long',contracts=body['vol'],leverage=body['leverage'],external_id=body['externalOid'],stop_loss=body['stopLossPrice'],take_profit=body['takeProfitPrice'],entry_price=body['price'],position_mode=body['positionMode'])
        elif path.endswith('/stoporder/place'):
            body=json.loads(request.content)
            data=fake.place_protection(position_id=body['positionId'],contracts=body['vol'],stop_loss=body['stopLossPrice'],take_profit=body['takeProfitPrice'],request_id='test')
        else:pytest.fail(path)
        return httpx.Response(200,json={'success':True,'code':0,'data':data})
    executor.private=MexcPrivateClient('fake-key','fake-secret',writes_enabled=True,client=httpx.Client(transport=httpx.MockTransport(handle)))
    intent=approve(executor)
    assert executor.execute(intent['intent_id'])['status']=='protected'
    assert paths.count('/api/v1/private/order/create')==1
    assert '/api/v1/private/position/leverage' in paths


def test_matching_protection_does_not_hide_a_second_reversing_order(tmp_path):
    executor,private,store=setup(tmp_path);intent=approve(executor)
    executor.execute(intent['intent_id'])
    reverse=deepcopy(private.stops[0]);reverse['takeProfitReverse']=1
    private.stops.append(reverse)
    result=executor.reconcile(intent['intent_id'])
    assert result['status']=='close_write_unknown'
    assert len(private.closes)==1 and len(private.protects)==1
