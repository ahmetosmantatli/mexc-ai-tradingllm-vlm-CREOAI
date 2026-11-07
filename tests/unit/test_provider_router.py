import base64
from types import SimpleNamespace

import httpx
import pytest

from trading.agents.provider_router import ProviderRouter
from trading.config import Settings
from trading.evaluation.service import ModelDecision
from trading.runtime.budget import Budget
from trading.runtime.store import RuntimeStore


def setup(tmp_path,handler,**overrides):
    overrides.setdefault('groq_request_token_budget',0)
    cfg=Settings(_env_file=None,groq_api_key='fake-groq',anthropic_api_key='fake-claude',**overrides)
    store=RuntimeStore(str(tmp_path/'state'))
    router=ProviderRouter(cfg,Budget(store,cfg),store,httpx.Client(transport=httpx.MockTransport(handler)))
    router.response_validator=ModelDecision.model_validate
    return router


def decision(action='hold'):
    return dict(action=action,signal_id=None,rationale='evidence',invalidation='level',watch_note='wait')


def response(payload):
    import json
    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload)}}], 'usage':{'prompt_tokens':100,'completion_tokens':20}})


def test_groq_success_does_not_call_claude_and_tracks_tokens(tmp_path,monkeypatch):
    router=setup(tmp_path,lambda request:response(decision()))
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:pytest.fail('Claude should not be called for HOLD'))
    assert router.complete_json(system='JSON rules',user='all context')==decision()
    assert router.last_provider=='groq' and router.last_status=='success'
    assert router.store.get('groq_usage')['input_tokens']==100
    assert router.store.get('ai_budget') is None


def test_rate_limit_preserves_complete_context_and_images_in_fallback(tmp_path,monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={'retry-after':'120'},json={'error':{'message':'limit'}})
    router=setup(tmp_path,handler)
    charts=[]
    for index in range(3):
        path=tmp_path/f'{index}.png';path.write_bytes(b'PNG'+bytes([index]));charts.append(str(path))
    forwarded=[]
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:forwarded.append(kw) or decision())
    assert router.complete_json(system='rules',user='memory and all candles',images=charts)==decision()
    assert forwarded[0]==dict(system='rules',user='memory and all candles',max_tokens=800,images=charts)
    assert router.last_fallback_reason=='groq_http_429'
    import json
    envelope=json.loads(calls[0].content)
    images=envelope['messages'][1]['content'][1:]
    assert len(images)==3
    assert base64.b64decode(images[2]['image_url']['url'].split(',')[1])==Path(charts[2]).read_bytes()
    router.complete_json(system='rules',user='next context',images=charts)
    assert len(calls)==1 # shared cooldown avoids hammering Groq


from pathlib import Path


def test_stop_during_primary_prevents_new_paid_fallback(tmp_path,monkeypatch):
    active=[True]
    def handler(request):
        active[0]=False
        return httpx.Response(429)
    router=setup(tmp_path,handler)
    router.can_call=lambda:active[0]
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:pytest.fail('paid fallback after stop'))
    assert router.complete_json(system='JSON',user='context') is None
    assert router.last_status=='stopped'


def test_invalid_decision_falls_back_but_opt_out_never_spends(tmp_path,monkeypatch):
    router=setup(tmp_path,lambda request:response({'action':'invented'}),ai_claude_fallback_enabled=False)
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:pytest.fail('fallback disabled'))
    assert router.complete_json(system='JSON',user='context') is None
    assert router.last_status=='primary_unavailable_fallback_disabled'


def test_invalid_groq_schema_can_fall_back_to_valid_claude(tmp_path,monkeypatch):
    router=setup(tmp_path,lambda request:response({'action':'invented'}))
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:decision())
    assert router.complete_json(system='JSON',user='context')==decision()
    assert router.last_provider=='anthropic'


def test_rejection_trace_is_safe_and_counts_real_requests(tmp_path,monkeypatch):
    secret='NEVER_STORE_PRIVATE_PROMPT'
    router=setup(tmp_path,lambda request:httpx.Response(400,json={'error':{'code':'json_validate_failed','param':'response_format','message':'Failed to generate JSON '+secret,'failed_generation':secret}}),ai_claude_fallback_enabled=False)
    progress=[]
    router.on_trace=lambda trace:progress.append(trace)
    assert router.complete_json(system='JSON',user=secret) is None
    attempt=router.last_trace['attempts'][0]
    assert attempt['http_status']==400
    assert attempt['error_category']=='json_generation_failed'
    assert attempt['error_code']=='json_validate_failed'
    assert attempt['generation_requests']==1
    assert attempt['estimated_cost_usd'] is None
    import json
    assert secret not in json.dumps(progress)
    assert router.store.get('ai_request_stats')['groq']['failures']==1
    assert router.store.get('ai_request_stats')['groq']['generation_requests']==1
    router.complete_json(system='JSON',user=secret)
    assert router.store.get('ai_request_stats')['groq']['generation_requests']==1


def test_reasoning_json_request_uses_documented_gpt_oss_options(tmp_path):
    payloads=[]
    def handler(request):
        import json
        payloads.append(json.loads(request.content))
        return response(decision())
    router=setup(tmp_path,handler)
    router.complete_json(system='JSON',user='candidate')
    assert payloads[0]['include_reasoning'] is False
    assert 'reasoning_format' not in payloads[0]


def test_large_context_routes_without_groq_call_or_data_loss(tmp_path,monkeypatch):
    router=setup(tmp_path,lambda request:pytest.fail('over-budget Groq request'),groq_request_token_budget=8000)
    charts=[]
    for i in range(3):
        path=tmp_path/f'{i}.png';path.write_bytes(b'chart');charts.append(str(path))
    forwarded=[]
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:forwarded.append(kw) or decision())
    original=dict(system='JSON policy',user='all original fields '*500,max_tokens=700,images=charts)
    assert router.complete_json(**original)==decision()
    assert forwarded==[original]
    attempt=router.last_trace['attempts'][0]
    assert attempt['status']=='quota_skipped'
    assert attempt['error_category']=='request_token_budget'
    assert attempt['estimated_input_tokens']>0 and attempt['token_budget']==8000
    assert attempt['generation_requests']==0
    assert router.store.get('ai_request_stats')['groq']['generation_requests']==0


def test_local_quota_can_wait_without_any_paid_request(tmp_path,monkeypatch):
    router=setup(tmp_path,lambda request:pytest.fail('should not send'),groq_request_token_budget=1000,ai_claude_fallback_enabled=False)
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:pytest.fail('paid fallback disabled'))
    assert router.complete_json(system='JSON',user='context') is None
    assert router.last_status=='primary_unavailable_fallback_disabled'
    assert router.last_trace['attempts'][0]['generation_requests']==0


def test_shared_minute_window_blocks_other_account_and_expires(tmp_path,monkeypatch):
    now=[1000.]
    monkeypatch.setattr('trading.agents.provider_router.time.time',lambda:now[0])
    calls=[]
    def handler(request):
        calls.append(request)
        return response(decision())
    first=setup(tmp_path,handler,groq_request_token_budget=4000,ai_claude_fallback_enabled=False)
    second=setup(tmp_path,handler,groq_request_token_budget=4000,ai_claude_fallback_enabled=False)
    assert first.complete_json(system='JSON',user='context')==decision()
    assert second.complete_json(system='JSON',user='context') is None
    assert second.last_fallback_reason=='groq_shared_minute_token_budget'
    assert second.last_trace['attempts'][0]['generation_requests']==0
    assert len(calls)==1
    now[0]+=61
    assert second.complete_json(system='JSON',user='context')==decision()
    assert len(calls)==2


def test_completion_budget_reduced_without_touching_input(tmp_path):
    payloads=[]
    def handler(request):
        import json
        payloads.append(json.loads(request.content))
        return response(decision())
    router=setup(tmp_path,handler)
    router.complete_json(system='JSON',user='unchanged context',max_tokens=700)
    assert payloads[0]['max_completion_tokens']==2048
    assert payloads[0]['messages'][1]['content'][0]['text']=='unchanged context'


def test_groq_then_gemini_preserves_context_without_paid_claude(tmp_path,monkeypatch):
    import json
    calls=[]
    def handler(request):
        calls.append((request.url.host,json.loads(request.content)))
        if request.url.host=='api.groq.com':
            return httpx.Response(429,headers={'retry-after':'60'})
        return response(decision())
    router=setup(tmp_path,handler,gemini_api_key='fake-gemini')
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:pytest.fail('valid Gemini HOLD must stop chain'))
    assert router.complete_json(system='JSON rules',user='full context')==decision()
    assert [host for host,_ in calls]==['api.groq.com','generativelanguage.googleapis.com']
    assert calls[0][1]['messages']==calls[1][1]['messages']
    assert router.last_provider=='gemini'
    assert len(router.last_trace['attempts'])==2


def test_gemini_then_groq_then_claude_with_exact_images(tmp_path,monkeypatch):
    calls=[]
    router=setup(tmp_path,lambda request:calls.append(request.url.host) or httpx.Response(503),gemini_api_key='fake-gemini',ai_vision_primary_provider='gemini')
    path=tmp_path/'chart.png';path.write_bytes(b'chart')
    forwarded=[]
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:forwarded.append(kw) or decision())
    router.complete_json(system='JSON',user='context',images=[str(path)])
    assert calls==['generativelanguage.googleapis.com','api.groq.com']
    assert forwarded[0]['images']==[str(path)] and forwarded[0]['user']=='context'
    assert [a['provider'] for a in router.last_trace['attempts']]==['gemini','groq','anthropic']


def test_expired_decision_does_not_start_next_provider(tmp_path,monkeypatch):
    import time
    holder=[]
    def handler(request):
        holder[0].deadline=time.time()-1
        return httpx.Response(429)
    router=setup(tmp_path,handler,gemini_api_key='fake-gemini')
    holder.append(router)
    monkeypatch.setattr(router.gemini,'complete_json',lambda **kw:pytest.fail('stale free fallback'))
    monkeypatch.setattr(router.claude,'complete_json',lambda **kw:pytest.fail('stale paid fallback'))
    assert router.complete_json(system='JSON',user='context') is None
    assert len(router.last_trace['attempts'])==1


def test_gemini_primary_can_run_without_other_keys(tmp_path):
    cfg=Settings(_env_file=None,gemini_api_key='fake',groq_api_key='',anthropic_api_key='',ai_primary_provider='gemini',ai_claude_fallback_enabled=False)
    store=RuntimeStore(str(tmp_path/'gemini-only'))
    router=ProviderRouter(cfg,Budget(store,cfg),store,httpx.Client(transport=httpx.MockTransport(lambda request:response(decision()))))
    assert router.available
    assert router.complete_json(system='JSON',user='context')==decision()
    assert router.last_provider=='gemini'
