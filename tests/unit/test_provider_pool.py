from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from trading.agents.provider_pool import ProviderPool
from trading.config import Settings
from trading.runtime.budget import Budget
from trading.runtime.store import RuntimeStore


def settings(**updates):
    defaults=dict(_env_file=None,groq_api_key='private-groq-key-1',gemini_api_key='',anthropic_api_key='',
        ai_primary_provider='groq',ai_vision_primary_provider='groq',ai_claude_fallback_enabled=False)
    defaults.update(updates)
    cfg=Settings(**defaults)
    # model_copy keeps tests usable while root adds these settings concurrently.
    pool_defaults=dict(groq_api_key_2='',gemini_api_key_2='',groq_quota_group='groq-main',groq_quota_group_2='groq-main',
        gemini_quota_group='gemini-main',gemini_quota_group_2='gemini-main',groq_rpm=30,groq_tpm=8000,groq_rpd=1000,groq_tpd=200000,
        gemini_rpm=15,gemini_tpm=250000,gemini_rpd=500,gemini_tpd=0,ai_pool_retry_seconds=0,ai_pool_max_retry_attempts=0)
    pool_defaults.update({key:value for key,value in updates.items() if key in pool_defaults})
    return cfg.model_copy(update=pool_defaults)


def response(action='hold'):
    return httpx.Response(200,json=dict(choices=[dict(message=dict(content=json.dumps(dict(action=action))))],
        usage=dict(prompt_tokens=100,completion_tokens=20)))


def pool(tmp_path,cfg=None,transport=None):
    cfg=cfg or settings()
    store=RuntimeStore(str(tmp_path/'state'))
    client=httpx.Client(transport=httpx.MockTransport(transport or (lambda request:response())))
    return ProviderPool(cfg,Budget(store,cfg),store,client),store


def test_valid_hold_never_uses_other_key_or_paid_fallback_and_records_no_secrets(tmp_path):
    calls=[]
    p,store=pool(tmp_path,settings(groq_api_key_2='private-groq-key-2',groq_quota_group_2='separate-project',anthropic_api_key='private-paid-key',ai_claude_fallback_enabled=True),
        lambda request:(calls.append(request) or response()))
    p.claude.complete_json=lambda **kw:pytest.fail('valid HOLD must never fallback')
    assert p.complete_json(system='instructions',user='same content')=={'action':'hold'}
    assert len(calls)==1 and p.last_credential_id=='groq-1'
    assert p.last_trace['attempts'][0]['credential_id']=='groq-1'
    for row in store.db.execute('SELECT key,value FROM state'):
        assert 'private-groq-key' not in str(row) and 'private-paid-key' not in str(row)
    assert 'private-groq-key' not in str(p.snapshot())
    snap=next(r for r in p.snapshot()['groups'] if r['model']==p.settings.groq_text_model and r['requests_minute'])
    assert snap['tokens_minute_estimate']==120 # known actual usage replaces estimate
    assert snap['in_flight'] is False


def test_duplicate_actual_key_is_not_a_second_route(tmp_path):
    p,_=pool(tmp_path,settings(groq_api_key_2='private-groq-key-1',groq_quota_group_2='different-name'))
    assert len(p._slots)==1


def test_same_group_keys_share_minute_and_daily_quota(tmp_path):
    cfg=settings(groq_api_key_2='private-groq-key-2',groq_rpm=1)
    calls=[]
    p,_=pool(tmp_path,cfg,lambda request:(calls.append(request) or response()))
    assert p.complete_json(system='s',user='u')=={'action':'hold'}
    assert p.fork().complete_json(system='s',user='u') is None
    assert len(calls)==1
    assert len({s.group for s in p._slots})==1


def test_daily_reservations_survive_restart(tmp_path):
    cfg=settings(groq_rpd=1)
    calls=[]
    p,store=pool(tmp_path,cfg,lambda request:(calls.append(request) or response()))
    assert p.complete_json(system='s',user='u')
    for key,value in list(store.db.execute("SELECT key,value FROM state WHERE key LIKE 'pool:quota:%'")):
        state=json.loads(value)
        state['rows'][0]['at']-=70 # no longer minute quota, still 24h quota
        store.put(key,state)
    second=ProviderPool(cfg,Budget(store,cfg),RuntimeStore(str(tmp_path/'state')),p.client)
    assert second.complete_json(system='s',user='u') is None
    assert len(calls)==1


def test_parallel_forks_choose_disjoint_group_leases(tmp_path):
    barrier=threading.Barrier(2)
    keys=[]
    def transport(request):
        keys.append(request.headers['authorization'])
        barrier.wait(timeout=3)
        return response()
    cfg=settings(groq_api_key_2='private-groq-key-2',groq_quota_group_2='second-org')
    p,_=pool(tmp_path,cfg,transport)
    children=[p.fork(),p.fork()]
    with ThreadPoolExecutor(2) as executor:
        results=list(executor.map(lambda c:c.complete_json(system='s',user='u'),children))
    assert all(r=={'action':'hold'} for r in results)
    assert len(set(keys))==2
    assert all(not row['in_flight'] for row in p.snapshot()['groups'])
    assert children[0].last_trace is not children[1].last_trace


def test_parallel_same_group_is_not_double_leased(tmp_path):
    entered=threading.Event();release=threading.Event();calls=[]
    def transport(request):
        calls.append(request)
        entered.set()
        release.wait(timeout=3)
        return response()
    cfg=settings(groq_api_key_2='private-groq-key-2')
    p,_=pool(tmp_path,cfg,transport)
    first,second=p.fork(),p.fork()
    with ThreadPoolExecutor(2) as executor:
        running=executor.submit(first.complete_json,system='s',user='u')
        assert entered.wait(timeout=2)
        assert second.complete_json(system='s',user='u') is None
        release.set()
        assert running.result(timeout=3)=={'action':'hold'}
    assert len(calls)==1


def test_429_shared_group_cooldown_does_not_use_other_nominal_key(tmp_path):
    calls=[]
    cfg=settings(groq_api_key_2='private-groq-key-2')
    p,_=pool(tmp_path,cfg,lambda request:(calls.append(request) or httpx.Response(429,headers={'retry-after':'60'},json=dict(error=dict(message='private-secret')))))
    assert p.complete_json(system='s',user='u') is None
    assert len(calls)==1
    assert any(row['cooldown_seconds']>0 for row in p.snapshot()['groups'])
    assert 'private-secret' not in str(p.last_trace)


def test_auth_failure_can_try_another_key_but_consumes_shared_requests(tmp_path):
    calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(401,json={'error':{'message':'private'}}) if len(calls)==1 else response()
    p,_=pool(tmp_path,settings(groq_api_key_2='private-groq-key-2'),transport)
    assert p.complete_json(system='s',user='u')=={'action':'hold'}
    assert len(calls)==2
    assert p.last_credential_id=='groq-2'
    snap=next(r for r in p.snapshot()['groups'] if r['model']==p.settings.groq_text_model)
    assert snap['requests_minute']==2


def test_full_unchanged_context_images_and_schema_reach_paid_once(tmp_path):
    images=[]
    for n in range(3):
        path=tmp_path/f'{n}.png';path.write_bytes(b'image-'+str(n).encode());images.append(str(path))
    calls=[]
    p,store=pool(tmp_path,settings(anthropic_api_key='paid',ai_claude_fallback_enabled=True),lambda request:(calls.append(request) or httpx.Response(400,json={'error':{'message':'not supported'}})))
    paid=[]
    def complete(**kwargs):
        paid.append(kwargs)
        p.claude.last_status='success'
        p.claude.last_attempt=dict(provider='anthropic',model='paid-model',status='success',generation_requests=1,count_requests=1)
        return {'action':'hold'}
    p.claude.complete_json=complete
    p.response_validator=lambda value:None
    p.decision_schema={'type':'object','properties':{'action':{'type':'string'}}}
    assert p.complete_json(system='full-system',user='full-user',images=images,max_tokens=700)=={'action':'hold'}
    assert len(paid)==1
    assert paid[0]==dict(system='full-system',user='full-user',images=images,max_tokens=700)
    assert p.claude.decision_schema==p.decision_schema
    assert p.last_credential_id=='anthropic-1'


def test_one_transient_retry_only_and_stop_during_wait_no_paid(tmp_path):
    cfg=settings(ai_pool_retry_seconds=5,ai_pool_max_retry_attempts=1,anthropic_api_key='paid',ai_claude_fallback_enabled=True)
    calls=[]
    p,_=pool(tmp_path,cfg,lambda request:(calls.append(request) or httpx.Response(500,json={'error':{'message':'down'}})))
    allowed=[True]
    p.can_call=lambda:allowed[0]
    p._sleep=lambda seconds:allowed.__setitem__(0,False)
    p.claude.complete_json=lambda **kwargs:pytest.fail('stop must block paid fallback')
    assert p.complete_json(system='s',user='u') is None
    assert p.last_status=='stopped' and len(calls)==1


def test_transient_retries_once_after_local_cooldown(tmp_path):
    calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(500,json={'error':{'message':'down'}}) if len(calls)==1 else response()
    cfg=settings(ai_pool_retry_seconds=0,ai_pool_max_retry_attempts=1)
    p,_=pool(tmp_path,cfg,transport)
    assert p.complete_json(system='s',user='u')=={'action':'hold'}
    assert len(calls)==2 and len(p.last_trace['attempts'])==2


def test_stop_and_expired_deadline_make_no_requests(tmp_path):
    p,_=pool(tmp_path,transport=lambda request:pytest.fail('unexpected request'))
    p.deadline=time.time()-1
    assert p.complete_json(system='s',user='u') is None
    assert p.last_status=='stopped'
    p.deadline=None;p.can_call=lambda:False
    assert p.complete_json(system='s',user='u') is None


def test_ambiguous_timeout_retains_upper_token_reservation(tmp_path):
    def transport(request):
        raise httpx.ReadTimeout('private secret',request=request)
    p,_=pool(tmp_path,transport=transport)
    assert p.complete_json(system='s',user='u') is None
    snap=next(r for r in p.snapshot()['groups'] if r['model']==p.settings.groq_text_model)
    assert snap['tokens_minute_estimate']>120
    assert snap['in_flight'] is False
    assert 'private secret' not in str(p.last_trace)


def test_parallel_groq_and_gemini_groups_share_context_without_collision(tmp_path):
    barrier=threading.Barrier(2)
    requests=[]
    def transport(request):
        requests.append(request)
        barrier.wait(timeout=3)
        return response()
    cfg=settings(gemini_api_key='private-gemini-key')
    p,store=pool(tmp_path,cfg,transport)
    children=[p.fork(),p.fork()]
    with ThreadPoolExecutor(2) as executor:
        results=list(executor.map(lambda child:child.complete_json(system='policy',user='context'),children))
    assert all(result=={'action':'hold'} for result in results)
    assert {child.last_provider for child in children}=={'groq','gemini'}
    assert all(json.loads(req.content)['messages'][1]['content'][0]['text']=='context' for req in requests)
    assert store.get('groq_usage')['input_tokens']==100
    assert store.get('gemini_usage')['input_tokens']==100


def test_invalid_schema_free_result_does_not_count_as_success(tmp_path):
    calls=[]
    p,store=pool(tmp_path,settings(groq_api_key_2='private-groq-key-2',groq_quota_group_2='second'),lambda request:(calls.append(request) or response('bad')))
    def valid(payload):
        if payload['action']!='hold':raise ValueError('bad decision')
    p.response_validator=valid
    assert p.complete_json(system='s',user='u') is None
    assert len(calls)==2 and p.successful_calls==0
    assert store.get('ai_request_stats')['groq']['successes']==0
    assert len(p.last_trace['attempts'])==2


def test_busy_free_group_waits_briefly_before_paid_fallback(tmp_path):
    p,store=pool(tmp_path,settings(ai_pool_retry_seconds=5,ai_pool_max_retry_attempts=1,
        anthropic_api_key='paid',ai_claude_fallback_enabled=True))
    slot=p._slots[0]
    reservation=p._reserve([slot],'s','u',800,None)
    waits=[]
    def wait(seconds):
        waits.append(seconds)
        p._release(reservation[1],reservation[2])
        return True
    p._wait=wait
    p.claude.complete_json=lambda **kw:pytest.fail('free slot became available')
    assert p.complete_json(system='s',user='u')=={'action':'hold'}
    assert waits==[5]
    assert p.last_provider=='groq'


def paid_pool(tmp_path):
    return pool(tmp_path,settings(groq_api_key='',anthropic_api_key='paid',
        ai_primary_provider='anthropic',ai_vision_primary_provider='anthropic'))


def paid_response(client, *, status='success', code=None, category='', retry=None):
    def complete(**kwargs):
        client.last_status=status
        client.last_attempt=dict(provider='anthropic',model='test',status=status,http_status=code,
            error_category=category,generation_requests=1,count_requests=1)
        if retry is not None:
            client.last_attempt['retry_after_seconds']=retry
        return {'action':'hold'} if status=='success' else None
    client.complete_json=complete


@pytest.mark.parametrize('status,code,category,retry,expected',[
    ('error_or_invalid_output',429,'rate_limit',123,123),
    ('error_or_invalid_output',429,'rate_limit',None,60),
    ('error_or_invalid_output',401,'authentication_or_permission',None,300),
    ('provider_credit_exhausted',400,'credit_exhausted',None,300),
    ('error_or_invalid_output',None,'ambiguous_timeout',None,30),
    ('error_or_invalid_output',503,'provider_server_error',None,30),
    ('output_truncated',200,'output_truncated',None,30),
])
def test_paid_failure_cooldown_persists_across_clients_without_http_retry(tmp_path,status,code,category,retry,expected):
    p,store=paid_pool(tmp_path)
    paid_response(p.claude,status=status,code=code,category=category,retry=retry)
    before=time.time()
    assert p.complete_json(system='s',user='u') is None
    state=store.get(p._paid_guard_key())
    assert state['lease'] is None
    assert before+expected<=state['retry_at']<=time.time()+expected
    # Another account/process using the same shared DB sees this cooldown too.
    restarted=ProviderPool(p.settings,p._paid_budget,RuntimeStore(str(tmp_path/'state')),p.client)
    restarted.claude.complete_json=lambda **kw:pytest.fail('no HTTP during shared cooldown')
    assert restarted.complete_json(system='s',user='u') is None
    assert restarted.last_status=='cooldown_skipped'
    attempt=restarted.last_trace['attempts'][-1]
    assert attempt['generation_requests']==attempt['count_requests']==0
    assert attempt['operational_guard'] is True
    assert restarted.snapshot()['paid_guard']['cooldown_seconds']>0


def test_paid_valid_hold_releases_lease_without_cooldown(tmp_path):
    p,store=paid_pool(tmp_path)
    paid_response(p.claude)
    assert p.complete_json(system='s',user='u')=={'action':'hold'}
    assert p.complete_json(system='s',user='u')=={'action':'hold'}
    state=store.get(p._paid_guard_key())
    assert state.get('retry_at',0)==0
    assert state['lease'] is None
    assert p.successful_calls==2


def test_paid_parallel_forks_single_inflight_does_not_block_free_routes(tmp_path):
    p,store=paid_pool(tmp_path)
    entered=threading.Event();release=threading.Event()
    child=p.fork()
    def complete(**kwargs):
        entered.set()
        assert release.wait(3)
        child.claude.last_status='success'
        child.claude.last_attempt=dict(provider='anthropic',model='test',status='success',generation_requests=1,count_requests=1)
        return {'action':'hold'}
    child.claude.complete_json=complete
    with ThreadPoolExecutor(1) as executor:
        future=executor.submit(child.complete_json,system='s',user='u')
        assert entered.wait(3)
        assert p.snapshot()['paid_guard']['in_flight'] is True
        p.claude.complete_json=lambda **kw:pytest.fail('second paid call must not run')
        assert p.complete_json(system='s',user='u') is None
        assert p.last_status=='paid_in_flight'
        assert p.last_trace['attempts'][-1]['generation_requests']==0
        free=ProviderPool(settings(),p._paid_budget,store,p.client)
        assert free.complete_json(system='s',user='u')=={'action':'hold'}
        assert free.last_provider=='groq'
        release.set()
        assert future.result(3)=={'action':'hold'}
    assert p.snapshot()['paid_guard']['in_flight'] is False


def test_paid_stopped_no_lease_or_count_http(tmp_path):
    p,store=paid_pool(tmp_path)
    p.can_call=lambda:False
    p.claude.complete_json=lambda **kw:pytest.fail('stopped request')
    assert p.complete_json(system='s',user='u') is None
    assert store.get(p._paid_guard_key()) is None


@pytest.mark.parametrize('header,expected',[('125',125),('garbage',None),('nan',None)])
def test_anthropic_safe_retry_after_parsed_without_raw_header_or_body(tmp_path,header,expected):
    p,_=paid_pool(tmp_path)
    class RateLimited(Exception):
        status_code=429
        body={'error':{'message':'private provider body'}}
        response=SimpleNamespace(headers={'retry-after':header,'authorization':'private-secret'})
    def reject(**kwargs):
        raise RateLimited()
    p.claude._client=SimpleNamespace(messages=SimpleNamespace(count_tokens=reject))
    assert p.complete_json(system='private prompt',user='private user') is None
    attempt=p.claude.last_attempt
    assert attempt.get('retry_after_seconds')==expected
    assert 'private' not in str(attempt)
    state=p.store.get(p._paid_guard_key())
    assert state['retry_at']>time.time()+ (expected or 60)-1


def test_dynamic_capacity_counts_quota_groups_not_credentials_and_includes_single_paid_guard(tmp_path):
    cfg=settings(groq_api_key_2='key-2',gemini_api_key='google',gemini_api_key_2='google-2',
        gemini_quota_group_2='separate',anthropic_api_key='paid',ai_claude_fallback_enabled=True)
    p,_=pool(tmp_path,cfg)
    capacity=p.analysis_capacity(prompt_bytes=100,max_tokens=700,ceiling=8)
    assert capacity['free_slots']==3 # Two Groq keys share ONE organization group.
    assert capacity['paid_slots']==1 and capacity['parallel_candidates']==4
    assert p.analysis_capacity(prompt_bytes=100,max_tokens=700,ceiling=2)['parallel_candidates']==2


def test_capacity_drops_for_busy_cooldown_and_oversize_requests_without_sending_http(tmp_path):
    calls=[];p,store=pool(tmp_path,settings(),lambda request:(calls.append(request) or response()))
    assert p.analysis_capacity(prompt_bytes=100)['parallel_candidates']==1
    assert p.analysis_capacity(prompt_bytes=100000)['parallel_candidates']==0
    slot=p._slots[0];key=p._quota_key(slot,False)
    store.put(key,dict(rows=[],lease={'until':time.time()+30}))
    assert p.analysis_capacity(prompt_bytes=100)['parallel_candidates']==0
    store.put(key,dict(rows=[],retry_at=time.time()+30))
    assert p.analysis_capacity(prompt_bytes=100)['parallel_candidates']==0
    assert not calls
