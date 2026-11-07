from types import SimpleNamespace

import pytest

from trading.agents.anthropic_client import AnthropicClient
from trading.config import Settings
from trading.runtime.budget import Budget
from trading.runtime.store import RuntimeStore


class Rejected(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        self.body = {'error': {'message': 'rejected'}}


def client(tmp_path, *, error=None, raw='{"ok":true}', can_call=None):
    cfg = Settings(_env_file=None, anthropic_api_key='test')
    store = RuntimeStore(str(tmp_path/'budget.sqlite3'))
    budget = Budget(store, cfg)
    requests = []
    def count(**kwargs):
        requests.append('count')
        return SimpleNamespace(input_tokens=100)
    def create(**kwargs):
        requests.append('create')
        if error is not None:
            raise error
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=10),
                               content=[SimpleNamespace(text=raw)])
    llm = AnthropicClient(settings=cfg, budget=budget, can_call=can_call)
    llm._client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count, create=create))
    return llm, store, requests


@pytest.mark.parametrize('status', [400, 401, 403, 429])
def test_definitive_rejections_release_pending_spend(tmp_path, status):
    llm, store, calls = client(tmp_path, error=Rejected(status))
    assert llm.complete_json(system='JSON', user='test') is None
    state = store.get('ai_budget')
    assert state['total'] == pytest.approx(0)
    assert state['reservations'] == {}
    assert state['successful_calls'] == 0


@pytest.mark.parametrize('error', [TimeoutError(), Rejected(408), Rejected(499), Rejected(500)])
def test_uncertain_failures_retain_separate_pending_reservation(tmp_path, error):
    llm, store, calls = client(tmp_path, error=error)
    assert llm.complete_json(system='JSON', user='test') is None
    state = store.get('ai_budget')
    assert len(state['reservations']) == 1
    assert state['successful_calls'] == 0


def test_invalid_json_does_not_erase_completed_usage(tmp_path):
    llm, store, calls = client(tmp_path, raw='not json')
    assert llm.complete_json(system='JSON', user='test') is None
    state = store.get('ai_budget')
    assert state['reservations'] == {}
    assert state['successful_calls'] == 1
    assert state['input_tokens'] == 100
    assert state['total'] > 0


def test_stop_before_count_makes_no_provider_calls(tmp_path):
    llm, store, calls = client(tmp_path, can_call=lambda:False)
    assert llm.complete_json(system='JSON', user='test') is None
    assert calls == []
    assert llm.last_status == 'stopped'
    assert store.get('ai_budget') is None


def test_stop_after_count_releases_reservation_before_generation(tmp_path):
    checks = iter([True, True, False])
    llm, store, calls = client(tmp_path, can_call=lambda:next(checks))
    assert llm.complete_json(system='JSON', user='test') is None
    assert calls == ['count']
    assert store.get('ai_budget')['total'] == pytest.approx(0)
    assert store.get('ai_budget')['reservations'] == {}
    assert llm.last_status == 'stopped'


def test_release_is_idempotent_and_preserves_settled_usage(tmp_path):
    llm, store, _ = client(tmp_path)
    settled = llm.budget.reserve(.01)
    llm.budget.settle(settled, 100, 10)
    pending = llm.budget.reserve(.02)
    assert llm.budget.release(pending)
    assert not llm.budget.release(pending)
    assert not llm.budget.release(settled)
    state = store.get('ai_budget')
    assert state['total'] == pytest.approx(.00045)
    assert sum(state['days'].values()) == pytest.approx(.00045)


def test_success_telemetry_records_usage_and_both_request_types(tmp_path):
    llm, store, calls = client(tmp_path)
    assert llm.complete_json(system='JSON', user='private context') == {'ok': True}
    attempt = llm.last_attempt
    assert attempt['provider'] == 'anthropic'
    assert attempt['model'] == llm.settings.anthropic_model
    assert attempt['status'] == 'success' and attempt['phase'] == 'generation'
    assert attempt['http_status'] == 200
    assert attempt['count_requests'] == attempt['generation_requests'] == 1
    assert attempt['input_tokens'] == 100 and attempt['output_tokens'] == 10
    assert attempt['estimated_cost_usd'] == pytest.approx(.00045)
    assert attempt['pending_usd'] == 0 and attempt['latency_ms'] >= 0
    assert 'private context' not in str(attempt)


def test_rejected_generation_telemetry_has_no_invented_cost_or_raw_error(tmp_path):
    error = Rejected(400)
    error.body = {'error': {'message': 'secret-key-sample private account'}}
    llm, store, calls = client(tmp_path, error=error)
    assert llm.complete_json(system='JSON', user='test') is None
    attempt = llm.last_attempt
    assert attempt['http_status'] == 400
    assert attempt['error_category'] == 'request_rejected'
    assert attempt['phase'] == 'generation'
    assert attempt['count_requests'] == attempt['generation_requests'] == 1
    assert attempt['estimated_cost_usd'] is None
    assert attempt['input_tokens'] is None and attempt['output_tokens'] is None
    assert attempt['pending_usd'] == 0
    assert 'secret-key-sample' not in str(attempt)
    assert 'private account' not in str(attempt)


def test_count_failure_is_distinguished_from_generation_failure(tmp_path):
    llm, store, calls = client(tmp_path)
    def reject_count(**kwargs):
        raise Rejected(429)
    llm._client.messages.count_tokens = reject_count
    assert llm.complete_json(system='JSON', user='test') is None
    assert llm.last_attempt['phase'] == 'count_tokens'
    assert llm.last_attempt['count_requests'] == 1
    assert llm.last_attempt['generation_requests'] == 0
    assert llm.last_attempt['error_category'] == 'rate_limit'
    assert llm.last_attempt['pending_usd'] == 0
    assert store.get('ai_budget') is None


def test_ambiguous_failure_telemetry_keeps_unknown_cost_and_pending_reserve(tmp_path):
    llm, store, calls = client(tmp_path, error=TimeoutError())
    assert llm.complete_json(system='JSON', user='test') is None
    assert llm.last_attempt['estimated_cost_usd'] is None
    assert llm.last_attempt['pending_usd'] > 0
    assert llm.last_attempt['http_status'] is None


def test_malformed_response_telemetry_retains_completed_cost(tmp_path):
    llm, store, calls = client(tmp_path, raw='not JSON')
    assert llm.complete_json(system='JSON', user='test') is None
    assert llm.last_attempt['error_category'] == 'invalid_output'
    assert llm.last_attempt['estimated_cost_usd'] == pytest.approx(.00045)
    assert llm.last_attempt['pending_usd'] == 0


def test_early_stop_and_missing_key_replace_previous_attempt(tmp_path):
    llm, store, calls = client(tmp_path)
    llm.can_call = lambda: False
    assert llm.complete_json(system='JSON', user='test') is None
    assert llm.last_attempt['status'] == 'stopped'
    assert llm.last_attempt['count_requests'] == llm.last_attempt['generation_requests'] == 0
    llm.can_call = None
    llm.settings.anthropic_api_key = ''
    assert llm.complete_json(system='JSON', user='test') is None
    assert llm.last_attempt['status'] == 'missing_key'
    assert llm.last_attempt['phase'] == 'prepare'
    assert llm.last_attempt['estimated_cost_usd'] is None


DECISION_SCHEMA = dict(type='object', properties={
    'action':dict(type='string', enum=['hold','watch','trade']),
    'signal_id':dict(type=['string','null']),
}, required=['action','signal_id'], additionalProperties=False)


def structured_client(tmp_path, *, blocks=None, stop_reason='tool_use'):
    llm,store,_=client(tmp_path)
    llm.decision_schema=DECISION_SCHEMA
    count_args=[]; generation_args=[]
    def count(**kwargs):
        count_args.append(kwargs)
        return SimpleNamespace(input_tokens=200)
    def create(**kwargs):
        generation_args.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=200,output_tokens=40),
            stop_reason=stop_reason, content=blocks if blocks is not None else [
                SimpleNamespace(type='tool_use',name='submit_trade_decision',input={'action':'hold','signal_id':None})])
    llm._client=SimpleNamespace(messages=SimpleNamespace(count_tokens=count,create=create))
    return llm,store,count_args,generation_args


def test_forced_tool_decision_uses_counted_payload_and_larger_output_budget(tmp_path):
    llm,store,counts,generations=structured_client(tmp_path)
    result=llm.complete_json(system='private instructions',user='private context',max_tokens=700)
    assert result=={'action':'hold','signal_id':None}
    assert len(counts)==len(generations)==1
    arguments=dict(generations[0]); output_limit=arguments.pop('max_tokens')
    assert output_limit==getattr(llm.settings,'anthropic_decision_max_tokens',1200)
    assert counts[0]==arguments
    assert arguments['tool_choice']==dict(type='tool',name='submit_trade_decision',disable_parallel_tool_use=True)
    assert arguments['tools'][0]['input_schema']==DECISION_SCHEMA
    assert arguments['tools'][0]['strict'] is True
    assert llm.last_attempt['output_stop_reason']=='tool_use'
    assert llm.last_attempt['validation_issues']==[]
    assert store.get('ai_budget')['successful_calls']==1
    assert 'private context' not in str(llm.last_attempt)


@pytest.mark.parametrize('blocks,reason,category', [
    ([SimpleNamespace(type='text',text='private provider explanation')],'end_turn','missing_decision_tool'),
    ([SimpleNamespace(type='tool_use',name='unknown_tool',input={'private':'secret'})],'tool_use','missing_decision_tool'),
    ([SimpleNamespace(type='tool_use',name='submit_trade_decision',input='private malformed input')],'tool_use','invalid_tool_input'),
    ([], 'refusal','model_refusal'),
    ([SimpleNamespace(type='tool_use',name='submit_trade_decision',input={'action':'hold','signal_id':None})],'max_tokens','output_truncated'),
    ([SimpleNamespace(type='tool_use',name='submit_trade_decision',input={'action':'hold','signal_id':None})],'model_context_window_exceeded','output_truncated'),
])
def test_structured_failures_hold_without_retry_or_erasing_usage(tmp_path,blocks,reason,category):
    llm,store,counts,generations=structured_client(tmp_path,blocks=blocks,stop_reason=reason)
    assert llm.complete_json(system='private',user='private') is None
    assert len(counts)==len(generations)==1
    assert llm.last_attempt['error_category']==category
    assert llm.last_attempt['output_stop_reason']==reason
    assert llm.last_attempt['validation_issues']
    assert llm.last_attempt['estimated_cost_usd']>0
    assert store.get('ai_budget')['successful_calls']==1
    assert store.get('ai_budget')['reservations']=={}
    assert 'private' not in str(llm.last_attempt)
    assert 'secret' not in str(llm.last_attempt)


def test_legacy_json_truncation_also_rejected_even_when_parseable(tmp_path):
    llm,store,calls=client(tmp_path)
    llm._client.messages.create=lambda **kw:SimpleNamespace(
        usage=SimpleNamespace(input_tokens=100,output_tokens=10),stop_reason='max_tokens',
        content=[SimpleNamespace(type='text',text='{"ok":true}')])
    assert llm.complete_json(system='JSON',user='test') is None
    assert llm.last_status=='output_truncated'
    assert store.get('ai_budget')['successful_calls']==1


def test_router_forwards_schema_and_checks_unknown_signal_without_retry(tmp_path):
    from trading.agents.provider_router import ProviderRouter
    cfg=Settings(_env_file=None,anthropic_api_key='test',ai_primary_provider='anthropic')
    store=RuntimeStore(str(tmp_path/'router.sqlite3'))
    router=ProviderRouter(cfg,Budget(store,cfg),store)
    router.decision_schema=DECISION_SCHEMA
    counts=[];generations=[]
    def count(**kwargs):
        counts.append(kwargs)
        return SimpleNamespace(input_tokens=200)
    def create(**kwargs):
        generations.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=200,output_tokens=40),stop_reason='tool_use',
            content=[SimpleNamespace(type='tool_use',name='submit_trade_decision',input={'action':'trade','signal_id':'private-unknown-signal'})])
    router.claude._client=SimpleNamespace(messages=SimpleNamespace(count_tokens=count,create=create))
    def validate(payload):
        if payload['signal_id']!='allowed':
            raise ValueError('private-unknown-signal')
    router.response_validator=validate
    assert router.complete_json(system='private instructions',user='private context') is None
    assert len(counts)==len(generations)==1
    attempt=router.last_trace['attempts'][0]
    assert attempt['status']=='invalid_schema'
    assert attempt['validation_issues']==[dict(field='response',type='custom_validation')]
    assert 'private-unknown-signal' not in str(attempt)
    assert store.get('ai_budget')['successful_calls']==1
    router.close()


def test_router_validation_issues_use_only_known_fields_and_categories(tmp_path):
    from pydantic import BaseModel,ConfigDict,Field
    from trading.agents.provider_router import ProviderRouter
    class Decision(BaseModel):
        model_config=ConfigDict(extra='forbid')
        action:str=Field(pattern='^(hold|watch|trade)$')
    router=ProviderRouter(Settings(_env_file=None),None,RuntimeStore(str(tmp_path/'router.sqlite3')))
    router.decision_schema=Decision.model_json_schema()
    try:
        Decision.model_validate({'action':'private-value','private-secret-key':'private-secret-value'})
    except Exception as exc:
        issues=router._validation_issues(exc)
    assert issues==[dict(field='action',type='string_pattern_mismatch'),dict(field='field',type='extra_forbidden')]
    assert 'private' not in str(issues)
    router.close()
