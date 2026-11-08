"""AI -> fresh executor -> native read-back, event dedup, stop and fallback context."""
from copy import deepcopy
from types import SimpleNamespace
from time import time
import json
import pytest
from pydantic import ValidationError

from trading.config import Settings
from trading.runtime.live_runner import LiveRunner
from trading.runtime.live_management import ManagementDecision
from .test_live_execution import setup,proposal
from .test_live_management_execution import ManagementClient,ManagementPublic


class Model:
    def __init__(self,root=None):
        self.root=root or self
        if root is None:self.calls=[];self.action='hold';self.on_call=None;self.bad_reference=False;self.fail=False
        self.last_trace={};self.last_provider='gemini';self.last_model='fake';self.last_status='success'
    def fork(self):return Model(self.root)
    def close(self):pass
    def complete_json(self,**kw):
        root=self.root;data=json.loads(kw['user']);root.calls.append((data,kw))
        if root.on_call:root.on_call()
        self.last_trace={'attempts':[dict(provider='gemini',model='fake',status='success',generation_requests=1,input_tokens=100,output_tokens=80)]}
        if root.fail:return None
        answer=dict(action=root.action,position_id=data['position']['position_id'],reason='Yapı korunuyor; destek teyidi gelene kadar pozisyonu koru.',review_timeframe='15m')
        if root.action=='move_stop':
            answer.update(stop_price=99.,stop_reference=dict(timeframe='15m',open_time=root.stamp+(1 if root.bad_reference else 0),field='low'))
        if root.action in ('reduce','close'):answer['close_fraction']=.5 if root.action=='reduce' else 1.
        return answer


def prepare(tmp_path,monkeypatch):
    control=dict(enabled=True,stopped=False,updated_at=1,version='text',risk_profile='balanced',
        risk_per_trade_pct=.01,allocation_mode='full')
    executor,_,store=setup(tmp_path,automatic=True,control=control)
    private=ManagementClient();public=ManagementPublic()
    executor.private=private;executor.public=public
    row=executor.authorize(proposal(),control_revision=1);row=executor.execute(row['intent_id'])
    assert row['status']=='protected'
    cfg=Settings(_env_file=None,broker_mode='live',live_position_management_enabled=True,
        runtime_db_path=str(tmp_path/'runtime'),kill_switch_path=str(tmp_path/'kill'),analysis_chart_dir=str(tmp_path/'charts'))
    llm=Model();llm.stamp=int(time())-1000
    rows=[[llm.stamp-900,100,104,98.5,100,10],[llm.stamp,100,103,99.1,100,10]]
    market=dict(timeframes=[dict(timeframe=tf,closed_at=int(time())-1000,features={},recent_ohlcv=deepcopy(rows)) for tf in ('4h','15m','5m')])
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(to_context=lambda:deepcopy(market),fingerprint='evidence'))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[SimpleNamespace(path=tmp_path/'chart.png')])
    runner=LiveRunner(cfg,store=store,account_id='ahmet',binding_id='binding-1',private=private,public=public,llm=llm,
        executor=executor,external_control=lambda:dict(control))
    return runner,private,executor,control,llm,market,row


def allow_next(runner,row):
    key=runner.position_manager.key(row);card=runner.store.get(key);card['last_attempt_at']=0;runner.store.put(key,card)


def test_hold_deduplicates_closed_event_and_records_token_usage(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch)
    for _ in range(3):runner.manage_positions()
    allow_next(runner,row);runner.manage_positions()
    assert len(llm.calls)==1 and not private.modifies and not private.closes
    market['timeframes'][1]['closed_at']+=900
    runner.manage_positions()
    assert len(llm.calls)==2
    data=llm.calls[-1][0]
    assert data['position']['previous_decisions'][0]['action']=='hold'
    assert data['account']['current_equity']==500
    assert data['position']['original_thesis']==row['proposal']['rationale']
    assert runner.store.db.execute('SELECT count(*) FROM ai_usage_events').fetchone()[0]==2


def test_stop_model_action_is_written_and_ui_uses_current_stop(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch);llm.action='move_stop'
    runner.manage_positions()
    assert len(private.modifies)==1
    changed=executor.state(row['intent_id'])
    assert changed['current_protection']['stop']==99
    assert runner.monitor()['positions']['BTC_USDT']['stop']==99
    assert runner._positions(runner._refresh())['BTC_USDT']['initial_stop']==98
    assert runner.store.get('management_status')['ai']['provider']=='gemini'


def test_ai_partial_then_next_review_has_remaining_size_and_confirmed_action(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch);llm.action='reduce'
    runner.manage_positions();llm.action='hold'
    allow_next(runner,row);market['timeframes'][1]['closed_at']+=900
    runner.manage_positions()
    data=llm.calls[-1][0]
    assert data['position']['remaining_contracts']==500
    assert data['position']['confirmed_actions'][0]['remaining_contracts']==500
    assert runner.monitor()['positions']['BTC_USDT']['remaining_contracts']==500


def test_stop_or_control_revision_change_during_ai_blocks_write(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch);llm.action='reduce'
    llm.on_call=lambda:control.update(enabled=False,stopped=True,updated_at=2)
    runner.manage_positions()
    assert not private.closes
    for _ in range(3):runner.manage_positions()
    assert len(llm.calls)==1
    assert runner.monitor()['reason']=='LIVE_RECONCILED'


def test_bad_model_reference_rejected_and_same_event_failure_not_spammed(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch)
    llm.action='move_stop';llm.bad_reference=True
    runner.manage_positions();allow_next(runner,row);runner.manage_positions()
    assert not private.modifies and len(llm.calls)==1
    assert runner.store.get('management_status')['status']=='review_failed'


def test_disabled_or_revoked_grant_never_calls_model(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch)
    runner.cfg.live_position_management_enabled=False;runner.manage_positions()
    runner.cfg.live_position_management_enabled=True;executor.revoke_start();runner.manage_positions()
    assert not llm.calls


def test_ai_failure_preserves_protection_and_vlm_uses_request_local_schema(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch)
    control['version']='vlm';llm.fail=True
    runner.manage_positions()
    assert len(llm.calls)==1 and llm.calls[0][1]['images']==[str(tmp_path/'chart.png')]
    assert not private.modifies and not private.closes
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    assert runner.store.get('management_status')['status']=='ai_unavailable'


@pytest.mark.parametrize('changes',[
    {'action':'open'}, {'action':'reduce','close_fraction':1.}, {'action':'close','close_fraction':.5},
    {'action':'hold','stop_price':99.}, {'action':'move_stop'}, {'review_timeframe':'1m'},
    {'action':'reduce','close_fraction':True}, {'stop_price':float('nan')}, {'other':'unexpected'},
])
def test_decision_schema_rejects_invalid_or_conflicting_actions(changes):
    with pytest.raises(ValidationError):ManagementDecision.model_validate(dict(
        {'action':'hold','position_id':'11','reason':'Yapısal teyit henüz oluşmadı.','review_timeframe':'15m'},**changes))


def test_new_r_milestone_can_trigger_between_review_candle_closes(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch)
    for view in market['timeframes']:view['closed_at']=int(time())-1
    runner.manage_positions();allow_next(runner,row)
    original=runner.public.get_ticker
    def moved(symbol):
        q=original(symbol);q.bid1=102.1;q.ask1=102.11;q.last_price=102.1;q.raw['fairPrice']=102.1;return q
    runner.public.get_ticker=moved
    runner.manage_positions()
    assert len(llm.calls)==2
    assert llm.calls[-1][0]['event']['favorable_r_milestone']==1
    allow_next(runner,row);runner.manage_positions()
    assert len(llm.calls)==2 # Same R milestone and same closed bar are not re-sent.


def test_temporary_entry_candidate_guard_does_not_replace_management_permission(tmp_path,monkeypatch):
    runner,private,executor,control,llm,market,row=prepare(tmp_path,monkeypatch)
    executor.can_execute=lambda:False # Simulate a concurrent entry-specific stale-candidate guard.
    runner.manage_positions()
    assert len(llm.calls)==1
    executor.revoke_start();allow_next(runner,row);market['timeframes'][1]['closed_at']+=900
    runner.manage_positions()
    assert len(llm.calls)==1 # Actual revoked operator grant still blocks management.
