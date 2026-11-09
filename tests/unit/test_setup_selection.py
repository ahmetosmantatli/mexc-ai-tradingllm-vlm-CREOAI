from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from trading.config import Settings
from trading.evaluation.service import ContextEvaluator
from trading.models.domain import Side
from trading.quant.signals import StrategyName
from trading.runtime.store import RuntimeStore
from .test_watch_service import signal
from .test_structure_setups import NOW, context, htf_zone_context, view


def prepare(tmp_path,monkeypatch,*,counter=False):
    market=context();market['symbol']='BTC_USDT'
    micro=view(market,'5m')
    if counter:
        micro['recent_ohlcv'][-1][1:5]=[99.,102.,97.,99.]
        micro['features']['sweeps']=[dict(direction='high_sweep',at=micro['recent_ohlcv'][-1][0],level=100.)]
    else:
        micro['features']['breaks']=[dict(direction='bullish',at=micro['recent_ohlcv'][-1][0],level=100.)]
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='closed',to_context=lambda:deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    cfg=Settings(_env_file=None,live_structure_setups_enabled=True,live_price_plan_enabled=True)
    store=RuntimeStore(str(tmp_path/'state'))
    calls=[];override={}
    family='countertrend_sweep_reclaim' if counter else 'trend_continuation'
    def complete(**kw):
        data=json.loads(kw['user']);calls.append(data)
        option=next(o for o in data['account']['setup_selection']['options'] if o['family']==family)
        answer=dict(action='trade',signal_id=data['candidate']['signal_id'],setup_id=option['setup_id'],
            rationale='Kapanmış yapısal kanıtlara göre bu kurulum seçildi.',invalidation='Yapısal dip/tepe bozulması.',watch_note='',
            sizing_plan=dict(leverage=5,risk_per_trade_pct=.005),
            price_plan=dict(entry_type='market',entry_price=data['candidate']['entry_price'],entry_timeframe='5m',
                stop_loss_price=option['stop_loss_price'],take_profit_price=option['take_profit_price'],
                stop_reference=option['stop_reference'],target_reference=option['target_reference'],reason='Kapanmış mum fitili ve karşı likidite referansı.'))
        answer.update(override)
        return answer
    evaluator=ContextEvaluator(SimpleNamespace(budget=None,complete_json=complete),store,
        SimpleNamespace(get_contract_rules=lambda s:SimpleNamespace(max_leverage=100,price_step=.01)),cfg)
    source=signal(Side.LONG if counter else Side.SHORT);source.entry_price=99. if counter else 101.
    def run(candidate=None):
        return evaluator.evaluate_candidates([candidate or source],equity=11.58,risk_per_trade_pct=.03,target_leverage=20,
            live_account_context=dict(mode='LIVE',currency='USDT',leverage_policy={'mode':'exchange'}),live_positions={})
    return run,source,calls,override,store


def test_model_selects_grounded_long_even_when_legacy_seed_was_short(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch)
    result=run()
    assert result.action.value=='trade',store.get('analysis_trace')
    assert result.selected_signal.side==Side.LONG and source.side==Side.SHORT
    assert result.selected_signal.signal_id==source.signal_id
    assert result.proposal.side==Side.LONG
    assert store.get('analysis_trace')['setup_family']=='trend_continuation'


def test_same_symbol_same_evidence_not_recalled_for_legacy_strategy_or_quote_change(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch)
    override.update(action='hold',setup_id=None,signal_id=None,price_plan=None,sizing_plan=None)
    run()
    sibling=source.model_copy(deep=True);sibling.strategy=StrategyName.MOMENTUM;sibling.side=Side.LONG;sibling.entry_price=101.01
    assert run(sibling).metadata['provider_called'] is False
    assert len(calls)==1


def test_countertrend_selected_within_half_policy_risk(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch,counter=True)
    result=run()
    assert result.action.value=='trade',store.get('analysis_trace')
    assert result.selected_signal.side==Side.SHORT
    assert result.proposal.metadata['setup_family']=='countertrend_sweep_reclaim'


def test_countertrend_cannot_use_full_policy_risk(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch,counter=True)
    override['sizing_plan']=dict(leverage=5,risk_per_trade_pct=.03)
    assert run().hold_reason=='invalid_or_unavailable_model'


def test_unknown_setup_never_changes_candidate_direction(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch)
    override['setup_id']='not-supplied'
    assert run().hold_reason=='invalid_or_unavailable_model'
    assert source.side==Side.SHORT


def test_execution_uses_selected_direction_but_original_queue_lease(tmp_path):
    from .test_live_runner import prepare as runner_setup,candidate,result as trade_result
    runner,private,executor,control=runner_setup(tmp_path)
    seed=candidate();runner.candidate_queue().refresh([seed])
    selected=seed.model_copy(deep=True);selected.side=Side.SHORT;selected.stop_loss_price=102.;selected.take_profit_price=96.
    result=trade_result(runner,selected)
    result.metadata['setup_selection']=dict(setup_id='grounded',side='short',family='pullback')
    status=runner._apply(seed,result,control)
    assert status['reason']=='LIVE_PROTECTED'
    assert executor.proposal.side==Side.SHORT
    assert private.changed[-1]['side']=='short'


def test_queued_structural_seed_cannot_fall_back_to_legacy_when_feature_disabled():
    from trading.evaluation.setup_selection import validate_selection
    from trading.evaluation.service import ModelDecision
    source=signal();source.features['structure_seed']=True
    decision=ModelDecision(action='trade',signal_id=source.signal_id,rationale='x',invalidation='',watch_note='')
    with pytest.raises(ValueError,match='structure seed'):
        validate_selection(decision,source,[],False)


def test_shadow_baseline_remains_original_when_model_selects_opposite_direction(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch)
    assert run().action.value=='trade'
    row=json.loads(store.db.execute('SELECT payload FROM edge_reviews').fetchone()[0])
    assert row['baseline']['side']=='short' and row['model_plan']['side']=='long'
    assert row['setup_kind']=='trend_continuation'


def test_legacy_trade_cannot_bypass_explicit_setup_risk_rules(tmp_path,monkeypatch):
    run,source,calls,override,store=prepare(tmp_path,monkeypatch,counter=True)
    source.side=Side.SHORT;source.strategy=StrategyName.MEAN_REVERSION
    override.update(setup_id=None,sizing_plan=dict(leverage=5,risk_per_trade_pct=.03))
    assert run().hold_reason=='invalid_or_unavailable_model'


def test_structural_scanner_can_emit_seed_without_legacy_strategy_signal(monkeypatch):
    from trading.quant.scanner import MarketScanner,LiquidityFilter
    from trading.exchange.mexc_public import TickerSnapshot
    from .test_structure_setups import NOW
    rows=[[NOW-900*(30-i),98.,99.,97.,98.,1.] for i in range(30)]
    rows[-10][2]=110.;rows[-7][3]=90.;rows[-6][2]=100.
    rows[-1][1:5]=[99.,102.,98.,101.]
    monkeypatch.setattr('trading.quant.scanner.time.time',lambda:NOW)
    client=SimpleNamespace(use_fixtures=False,list_fee_free_candidates=lambda:[],list_contract_symbols=lambda:['BTC_USDT'],
        get_ticker=lambda s:TickerSnapshot(s,101.,100.99,101.01,1e9),get_klines=lambda *a,**kw:{'candles':deepcopy(rows)})
    monkeypatch.setattr('trading.quant.scanner.run_strategies',lambda *a:[])
    scanner=MarketScanner(client,strategies=[],liquidity=LiquidityFilter(),structure_setups_enabled=True)
    result=scanner.scan()
    assert result.errors=={} and result.candidates
    assert all(c.strategy==StrategyName.STRUCTURE_SETUP and c.features['requires_full_context'] for c in result.candidates)


@pytest.mark.parametrize('runner_id',['baseline-v1','runner3'])
def test_opposite_direction_selection_reaches_native_exchange_protection(tmp_path,monkeypatch,runner_id):
    from time import time
    from trading.runtime.live_runner import LiveRunner
    from trading.brokers.live.automatic import AutomaticLiveExecutor,AutomaticLivePolicy
    from trading.brokers.live.execution import LiveExecutionGates
    from trading.risk.engine import RiskEngine
    from .test_live_execution import Client
    from .test_live_pipeline import QuotePublic
    market=context();market['symbol']='BTC_USDT'
    micro=view(market,'5m');micro['features']['breaks']=[dict(direction='bullish',at=micro['recent_ohlcv'][-1][0],level=100.)]
    if runner_id=='runner3':
        offset=int(time())-NOW-1

        def rebase(value):
            if isinstance(value, dict):
                return {key: rebase(item) for key,item in value.items()}
            if isinstance(value, list):
                return [rebase(item) for item in value]
            return value + offset if isinstance(value, (int,float)) and value > 1000000 else value

        market=rebase(htf_zone_context('4h','15m'))
        market['symbol']='BTC_USDT'
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='native-choice',to_context=lambda **kw:deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:
                        [SimpleNamespace(path=tmp_path/'chart.png')] if runner_id=='runner3' else [])
    class Model:
        budget=None
        last_status='success';last_provider='mock';last_model='no-network'
        last_trace={'attempts':[{'generation_requests':1,'status':'success'}]}
        def complete_json(self,**kw):
            data=json.loads(kw['user']);assert data['candidate']['side']=='short'
            assert bool(kw['images']) == (runner_id=='runner3')
            family='pullback' if runner_id=='runner3' else 'trend_continuation'
            option=next(o for o in data['account']['setup_selection']['options'] if o['family']==family and o['state']=='ready')
            return dict(action='trade',signal_id=data['candidate']['signal_id'],setup_id=option['setup_id'],
                rationale='Kapanmış yapı ters ön elemeye rağmen long kurulum gösteriyor.',invalidation='Yapısal dip altı.',watch_note='',
                sizing_plan={'leverage':5,'risk_per_trade_pct':.001},
                price_plan=dict(entry_type='market',entry_price=100.,entry_timeframe=option.get('trigger_timeframe','5m'),stop_loss_price=option['stop_loss_price'],
                    take_profit_price=option['take_profit_price'],stop_reference=option['stop_reference'],
                    target_reference=option['target_reference'],reason='Teyitli fitil altı stop ve gözlenen üst likidite.'))
    cfg=Settings(_env_file=None,broker_mode='live',live_trading_enabled=True,live_automatic_enabled=True,
        live_structure_setups_enabled=True,live_price_plan_enabled=True,live_allowed_accounts=('ahmet',),
        risk_profile='balanced',risk_per_trade_pct=.01,default_target_leverage=5,kill_switch_path=str(tmp_path/'STOP'),analysis_chart_dir=str(tmp_path/'charts'))
    store=RuntimeStore(str(tmp_path/'runtime.sqlite3'));private,public=Client(),QuotePublic()
    if runner_id=='runner3':
        private.assets=[dict(currency='USDT',equity=11.,availableBalance=11.,positionMargin=0.,unrealized=0.)]
        def submit_ioc(*,limit_price,**kwargs):
            private.submit_market(entry_price=limit_price,**kwargs)
            private.order_row['orderType']=3
            private.stops=[dict(positionId=11,positionType=1,orderId=22,errorCode=0,state=1,isFinished=0,
                lossTrend=2,profitTrend=2,stopLossType=0,takeProfitType=0,stopLossReverse=2,takeProfitReverse=2,
                profitLossVolType='SAME',vol=kwargs['contracts'],
                stopLossPrice=kwargs['stop_loss'],takeProfitPrice=kwargs['take_profit'])]
        private.submit_ioc=submit_ioc
    control=dict(runner_id=runner_id,enabled=True,stopped=False,updated_at=1,version='text',risk_profile='balanced',risk_per_trade_pct=.01,max_leverage=5,max_positions=2,allocation_mode='full')
    executor=AutomaticLiveExecutor(AutomaticLivePolicy(True,('ahmet',),('7',),2),LiveExecutionGates(True,'live',('7',),True),
        store,private,public,RiskEngine(settings=cfg),control_provider=lambda:dict(control),account_id='ahmet',binding_id='binding')
    runner=LiveRunner(cfg,store=store,account_id='ahmet',binding_id='binding',private=private,public=public,llm=Model(),external_control=lambda:dict(control),executor=executor)
    executor.grant_start('7',1)
    seed=signal(Side.SHORT);seed.features.update(source_closed_at=time(),dedup_key='short-screen',htf_zone_seed=runner_id=='runner3')
    runner.candidate_queue().refresh([seed])
    status=runner.cycle()
    assert status['reason']=='LIVE_PROTECTED',(status,store.get('analysis_trace'),store.get('live_candidate_error:BTC_USDT'))
    assert len(private.submits)==1 and private.order_row['side']==1
    assert private.stops[0]['stopLossPrice']<100<private.stops[0]['takeProfitPrice']
    if runner_id=='runner3':
        row=executor.intents()[0]
        assert row['entry_submitted_type']==3
        assert row['proposal']['metadata']['runner3_allocation']['equity_usdt']==11
        assert row['proposal']['metadata']['strategy_cohort']=='runner3-htf-zone-v3'
        assert row['proposal']['metadata']['htf_setup']['context_timeframe']=='4h'
        assert row['proposal']['metadata']['price_plan']['stop_reference']['timeframe']=='15m'
        assert row['proposal']['metadata']['decision_ai']['review_mode']=='vlm'


@pytest.mark.parametrize('probe_result', ['valid', 'invalid', 'unavailable'])
def test_runner3_text_mode_prefers_valid_vision_and_keeps_single_decision(tmp_path, monkeypatch, probe_result):
    from .test_structure_setups import htf_zone_context

    market = htf_zone_context('4h','15m')
    market['symbol'] = 'BTC_USDT'
    monkeypatch.setattr('trading.analysis.build_bundle', lambda *args, **kwargs:
                        SimpleNamespace(fingerprint='lane', to_context=lambda **kw: deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle', lambda *args, **kwargs:
                        [SimpleNamespace(path=tmp_path/'chart.png')])

    class Model:
        budget = None
        last_status = 'success'
        last_provider = 'mock'
        last_model = 'vision-model'
        last_trace = {}
        deadline = None

        def __init__(self):
            self.calls = []

        def complete_vision_probe(self, **request):
            self.calls.append(request)
            self.last_trace = {'attempts':[{'generation_requests':int(probe_result != 'unavailable'),
                                           'status':'quota_skipped' if probe_result == 'unavailable' else 'success'}]}
            if probe_result == 'unavailable':
                return None
            return {'action':'hold', 'rationale':'Bekleme kararı.'} if probe_result == 'invalid' else self.hold()

        def complete_json(self, **request):
            self.calls.append(request)
            self.last_model = 'text-model'
            self.last_trace = {'attempts':[{'generation_requests':1,'status':'success'}]}
            return self.hold()

        @staticmethod
        def hold():
            return dict(action='hold',signal_id=None,rationale='Alt zaman dilimi teyidi bekleniyor.',
                        invalidation='Bölge geçersiz olursa takip biter.',watch_note='')

    model = Model()
    settings = Settings(_env_file=None,analysis_version='text',live_structure_setups_enabled=True,
                        live_price_plan_enabled=True)
    store = RuntimeStore(str(tmp_path/'state'))
    evaluator = ContextEvaluator(model,store,
        SimpleNamespace(get_contract_rules=lambda symbol:SimpleNamespace(max_leverage=20,price_step=.01)),settings)
    seed=signal(Side.LONG)
    seed.entry_price=101.
    result=evaluator.evaluate_candidates([seed],equity=11.58,risk_per_trade_pct=.03,target_leverage=20,
        live_account_context=dict(mode='LIVE',currency='USDT',runner_id='runner3',current_equity=11.58,available_usdt=11.58),
        live_positions={})
    assert result.hold_reason == 'hold'
    assert model.calls[0]['images'] and json.loads(model.calls[0]['user'])['version']=='vlm'
    expected='vlm' if probe_result=='valid' else 'text'
    assert result.metadata['decision_ai']['review_mode']==expected
    trace=store.get('analysis_trace')
    assert trace['decision_mode']==expected
    assert [attempt['review_mode'] for attempt in trace['attempts']]==(['vlm'] if expected=='vlm' else ['vlm','text'])
    if expected=='text':
        assert len(model.calls)==2 and model.calls[1]['images'] is None
        vision, text=(json.loads(call['user']) for call in model.calls)
        vision.pop('version')
        text.pop('version')
        assert vision==text
    else:
        assert len(model.calls)==1


@pytest.mark.parametrize('invalid_trigger',[False, True])
def test_runner3_watch_uses_supplied_zone_trigger_and_invalidation(tmp_path, monkeypatch, invalid_trigger):
    from .test_structure_setups import htf_zone_context

    market = htf_zone_context('4h', '15m')
    market['symbol'] = 'BTC_USDT'
    lower = view(market,'15m')
    lower['recent_ohlcv'][-3][3] = 98.
    monkeypatch.setattr('trading.analysis.build_bundle', lambda *args, **kwargs:
        SimpleNamespace(fingerprint='zone',to_context=lambda **kw:deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    cfg=Settings(_env_file=None,analysis_version='text',live_structure_setups_enabled=True,live_price_plan_enabled=True)
    store=RuntimeStore(str(tmp_path/'state'))
    def answer(**request):
        option,=json.loads(request['user'])['account']['setup_selection']['options']
        assert option['evidence']['stage']=='awaiting_touch'
        payload=dict(action='watch',signal_id=None,setup_id=option['setup_id'],
                     rationale='Bölgeye dönüş ve teyit gerekiyor.',invalidation='Bölgenin alt sınırı.',watch_note='')
        if invalid_trigger:
            from time import time
            payload['watch_plan']=dict(trigger_condition='price_below',trigger_price=98.,
                invalidation_condition='below',invalidation_price=94.,expires_at=time()+3600)
        return payload
    evaluator=ContextEvaluator(SimpleNamespace(budget=None,complete_json=answer),store,
        SimpleNamespace(get_contract_rules=lambda _:SimpleNamespace(max_leverage=20,price_step=.01)),cfg)
    seed=signal(Side.LONG)
    seed.entry_price=101.
    result=evaluator.evaluate_candidates([seed],equity=11.58,risk_per_trade_pct=.03,target_leverage=20,
        live_account_context=dict(mode='LIVE',currency='USDT',runner_id='runner3',current_equity=11.58,available_usdt=11.58),
        live_positions={})
    if invalid_trigger:
        assert result.hold_reason=='invalid_or_unavailable_model'
        return
    assert result.hold_reason=='watch'
    assert result.metadata['watch_plan']['trigger_price']==97.
    assert result.metadata['watch_plan']['invalidation_price']==94.
    assert store.get('analysis_trace')['htf_evidence']['evidence']['stage']=='awaiting_touch'
