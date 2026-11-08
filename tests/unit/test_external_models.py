import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from dotenv import dotenv_values

from trading.agents.external_catalog import (CF_MODELS, ExternalRoute, cloudflare_routes, discover,
                                              load_routes, openrouter_routes, zero_pricing)
from trading.agents.external_client import ExternalClient
from trading.agents.provider_pool import ProviderPool
from trading.config import Settings
from trading.control.external_models import save_manifest, setup_env
from trading.runtime.store import RuntimeStore


def cfg(**kw):
    base = dict(_env_file=None, anthropic_api_key='', groq_api_key='', gemini_api_key='',
                ai_claude_fallback_enabled=False, ai_primary_provider='groq', ai_vision_primary_provider='gemini',
                ai_pool_retry_seconds=0, ai_pool_max_retry_attempts=0,
                external_models_enabled=True, openrouter_api_key='secret-or',
                cloudflare_api_token='secret-cf', cloudflare_account_id='a' * 32, cloudflare_free_plan_confirmed=True)
    return Settings(**(base | kw))


def model(ident='test/vision:free', **kw):
    return dict(id=ident, context_length=131072, top_provider=dict(max_completion_tokens=8192),
                architecture=dict(input_modalities=['text', 'image'], output_modalities=['text']),
                supported_parameters=['structured_outputs', 'tools'], pricing=dict(prompt='0', completion='0', request='0')) | kw


def route(**kw):
    return ExternalRoute.model_validate(openrouter_routes([model()])[0][0] | kw)


def reply(result=None, **kw):
    return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(result or {'action': 'hold'})))],
                                        usage=dict(prompt_tokens=100, completion_tokens=30)) | kw)


def transport(calls, post=None, remaining=50, models=None):
    def handle(req):
        calls.append(req)
        if req.url.path == '/api/v1/key':
            return httpx.Response(200, json=dict(data=dict(free_model_daily_requests=dict(used=50-remaining, limit=50, remaining=remaining))))
        if req.url.path == '/api/v1/models/user':
            return httpx.Response(200, json=dict(data=models if models is not None else [model()]))
        return post(req) if post else reply()
    return httpx.Client(transport=httpx.MockTransport(handle))


def manifest(path, routes):
    save_manifest(path, dict(version=1, observed_at=time.time(), routes=[r.model_dump() for r in routes], reports=[]))


@pytest.mark.parametrize('pricing', [None, {}, {'prompt': '0'}, {'prompt': 'NaN', 'completion': '0'},
    {'prompt': '0', 'completion': '0', 'request': '0.1'}, {'prompt': '0', 'completion': '-1'},
    {'prompt': '0', 'completion': '0', 'image': None}])
def test_uncertain_or_nonzero_pricing_rejected(pricing):
    assert not zero_pricing(pricing)


def test_catalog_only_free_chat_with_known_capabilities():
    rows = [model(), model('test/paid'), model('test/audio:free', architecture=dict(input_modalities=['text'], output_modalities=['audio'])),
            model('test/small:free', context_length=8192), model('test/plain:free', supported_parameters=[]),
            model('test/embedding:free', endpoints=['/api/v1/embeddings']), model('test/unknown:free', top_provider={})]
    good, bad = openrouter_routes(rows)
    assert [r['model'] for r in good] == ['test/vision:free']
    assert len(bad) == 6


def test_tool_only_and_text_only_are_marked():
    good, _ = openrouter_routes([model(supported_parameters=['tools'], architecture=dict(input_modalities=['text'], output_modalities=['text']))])
    assert good[0]['format'] == 'tool' and not good[0]['vision']


def test_cloudflare_only_known_free_models_and_rates():
    good, bad = cloudflare_routes([{'name': n} for n in [*CF_MODELS, '@cf/moonshotai/kimi-k2.6']])
    assert len(good) == 3 and len(bad) == 1
    assert sum(r['vision'] for r in good) == 1
    assert all(r['neuron_input'] > 0 for r in good)


def test_discovery_no_generation_no_secrets_and_cf_requires_free_plan(tmp_path):
    calls = []
    def handle(req):
        calls.append(req)
        if 'cloudflare' in req.url.host:
            return httpx.Response(200, json=dict(success=True, result=[{'name': n} for n in CF_MODELS]))
        if req.url.path.endswith('/key'):
            return httpx.Response(200, json=dict(data=dict(free_model_daily_requests=dict(used=0, remaining=50, limit=50))))
        return httpx.Response(200, json=dict(data=[model()]))
    result = discover(cfg(cloudflare_free_plan_confirmed=False), httpx.Client(transport=httpx.MockTransport(handle)))
    assert len(result['routes']) == 1
    assert all(r.method == 'GET' for r in calls)
    assert 'secret-' not in json.dumps(result)
    save_manifest(tmp_path/'models.json', result)
    assert len(load_routes(tmp_path/'models.json')) == 1
    assert (tmp_path/'models.json').stat().st_mode & 0o777 == 0o600


def test_stale_modified_or_paid_manifest_is_disabled(tmp_path):
    path=tmp_path/'models.json'
    manifest(path,[route()])
    assert load_routes(path, now=time.time()+86401) == []
    manifest(path,[route(model='paid/name')])
    assert load_routes(path) == []
    cf=ExternalRoute.model_validate(cloudflare_routes([{'name':next(iter(CF_MODELS))}])[0][0])
    manifest(path,[cf.model_copy(update=dict(neuron_input=.01))])
    assert load_routes(path) == []


def test_full_context_schema_images_and_valid_hold_no_extra_calls(tmp_path):
    calls=[];client=transport(calls)
    store=RuntimeStore(str(tmp_path/'state'))
    c=ExternalClient(cfg(),route(),store,client)
    c.decision_schema=dict(type='object',properties={'action':dict(type='string',enum=['hold'])},required=['action'],additionalProperties=False)
    c.response_validator=lambda r: None if r=={'action':'hold'} else pytest.fail('changed decision')
    assert c.complete_json(system='SYSTEM',user='unique context' * 100,images=[b'image']) == {'action':'hold'}
    payload=json.loads(calls[-1].content)
    assert payload['messages'][0]['content']=='SYSTEM'
    assert payload['messages'][1]['content'][0]['text']=='unique context'*100
    assert payload['messages'][1]['content'][1]['image_url']['url'].endswith(base64.b64encode(b'image').decode())
    assert payload['response_format']['json_schema']['schema']==c.decision_schema
    assert c.last_trace['attempts'][0]['input_tokens']==100
    assert len(calls)==3


@pytest.mark.parametrize('post', [lambda req:reply(choices=[dict(finish_reason='length', message=dict(content='{"action":"hold"}'))]),
                                lambda req:reply({'action':'trade'}), lambda req:httpx.Response(200,json={'error':{'message':'secret-or'}})])
def test_invalid_refused_or_truncated_never_become_decision(tmp_path,post):
    c=ExternalClient(cfg(),route(),RuntimeStore(str(tmp_path/'state')),transport([],post))
    def validate(r):
        if r!={'action':'hold'}:raise ValueError('private input secret-or')
    c.response_validator=validate
    assert c.complete_json(system='s',user='u') is None
    assert 'secret-or' not in json.dumps(c.last_trace)


def test_text_only_route_does_not_drop_images(tmp_path):
    calls=[]
    c=ExternalClient(cfg(),route(vision=False),RuntimeStore(str(tmp_path/'state')),transport(calls))
    assert c.complete_json(system='s',user='u',images=[b'image']) is None
    assert not calls


@pytest.mark.parametrize('models,remaining', [([model(pricing=dict(prompt='1',completion='0'))],50),([model()],0)])
def test_changed_price_or_exhausted_key_never_sends_generation(tmp_path,models,remaining):
    calls=[]
    c=ExternalClient(cfg(),route(),RuntimeStore(str(tmp_path/'state')),transport(calls,models=models,remaining=remaining))
    assert c.complete_json(system='s',user='u') is None
    assert all(req.method=='GET' for req in calls)


def test_tool_only_adapter_is_not_tool_execution(tmp_path):
    calls=[]
    r=route(format='tool')
    client=transport(calls,models=[model(supported_parameters=['tools'])], post=lambda req:reply(choices=[dict(finish_reason='tool_calls',message=dict(tool_calls=[dict(function=dict(name='return_decision',arguments='{"action":"hold"}'))]))]))
    c=ExternalClient(cfg(),r,RuntimeStore(str(tmp_path/'state')),client)
    assert c.complete_json(system='s',user='u')=={'action':'hold'}
    assert json.loads(calls[-1].content)['tool_choice']['function']['name']=='return_decision'


def test_shared_cloudflare_neuron_budget_survives_restart_and_unknown_outcome(tmp_path):
    routes=[ExternalRoute.model_validate(r) for r in cloudflare_routes([{'name':n} for n in CF_MODELS])[0]]
    store=RuntimeStore(str(tmp_path/'state'));calls=[]
    def timeout(req):
        calls.append(req);raise httpx.ReadTimeout('secret-cf',request=req)
    client=httpx.Client(transport=httpx.MockTransport(timeout))
    config=cfg(cloudflare_daily_neuron_budget=100)
    c=ExternalClient(config,routes[0],store,client)
    assert c.complete_json(system='s',user='u') is None
    assert c.accounting()['neurons_reserved_or_estimated']>0
    second=ExternalClient(config,routes[-1],RuntimeStore(str(tmp_path/'state')),client)
    assert second.accounting()==c.accounting()
    before=len(calls)
    assert second.complete_json(system='s',user='u') is None
    assert len(calls)==before  # More expensive model cannot evade account budget.


def test_cf_known_usage_reconciles_local_estimate(tmp_path):
    r=ExternalRoute.model_validate(cloudflare_routes([{'name':next(iter(CF_MODELS))}])[0][0])
    c=ExternalClient(cfg(),r,RuntimeStore(str(tmp_path/'state')),httpx.Client(transport=httpx.MockTransport(lambda req:reply())))
    assert c.complete_json(system='s',user='u')=={'action':'hold'}
    assert c.accounting()['neurons_reserved_or_estimated']==pytest.approx((100*r.neuron_input+30*r.neuron_output)/1e6)


def test_pool_opt_in_dynamic_refresh_and_shared_account_quotas(tmp_path):
    path=tmp_path/'models.json';manifest(path,[route(),route(model='other/model:free')])
    config=cfg(external_models_file=str(path))
    calls=[];client=transport(calls,models=[model(),model('other/model:free')])
    pool=ProviderPool(config,None,RuntimeStore(str(tmp_path/'state')),client)
    assert len(pool._slots)==2 and len(pool.snapshot()['groups'])==1
    assert pool.analysis_capacity()['free_slots']==1
    assert pool.complete_json(system='s',user='u')=={'action':'hold'}
    assert sum(r.method=='POST' for r in calls)==1
    assert pool.snapshot()['groups'][0]['requests_daily_remaining']==49
    disabled=ProviderPool(config.model_copy(update=dict(external_models_enabled=False)),None,pool.store,client)
    assert not disabled._slots
    manifest(path,[])
    assert not pool.snapshot()['groups']


def test_429_across_or_models_preserves_retry_after(tmp_path):
    calls=[];store=RuntimeStore(str(tmp_path/'state'))
    client=transport(calls,post=lambda req:httpx.Response(429,headers={'Retry-After':'60'},json={'error':'secret-or'}))
    first=ExternalClient(cfg(),route(),store,client)
    assert first.complete_json(system='s',user='u') is None
    second=ExternalClient(cfg(),route(model='other/model:free'),store,client)
    assert second.retry_at()>time.time()+55
    assert not second.capacity_available()
    assert second.complete_json(system='s',user='u') is None
    assert sum(r.method=='POST' for r in calls)==1


def test_stop_and_oversized_context_are_local_only(tmp_path):
    calls=[];c=ExternalClient(cfg(),route(),RuntimeStore(str(tmp_path/'state')),transport(calls))
    assert c.complete_json(system='s',user='x'*131072) is None
    c.can_call=lambda:False
    assert c.complete_json(system='s',user='u') is None
    assert not calls


def test_setup_preserves_other_keys_and_never_enables_paid_or_live(tmp_path):
    path=tmp_path/'.env';path.write_text('MEXC_API_KEY=keep-me\nAI_CLAUDE_FALLBACK_ENABLED=false\nLIVE_TRADING_ENABLED=false\n')
    values=iter(['new-or','new-cf','a'*32])
    setup_env(path,free_plan=True,ask=lambda prompt:next(values))
    env=dotenv_values(path)
    assert env['MEXC_API_KEY']=='keep-me'
    assert env['AI_CLAUDE_FALLBACK_ENABLED']=='false'
    assert env['LIVE_TRADING_ENABLED']=='false'
    assert env['OPENROUTER_API_KEY']=='new-or'
    assert env['CLOUDFLARE_FREE_PLAN_CONFIRMED']=='true'
    assert path.stat().st_mode & 0o777==0o600
    assert list(tmp_path.glob('.env.backup-external-*'))[0].stat().st_mode & 0o777==0o600


def test_external_keys_redacted_from_settings_repr():
    assert 'secret-or' not in repr(cfg()) and 'secret-cf' not in repr(cfg())


def test_catalog_429_is_shared_and_never_generates(tmp_path):
    calls=[]
    def reject(req):
        calls.append(req)
        return httpx.Response(429,headers={'Retry-After':'120'},json={'error':'secret-or'})
    store=RuntimeStore(str(tmp_path/'state'))
    client=httpx.Client(transport=httpx.MockTransport(reject))
    first=ExternalClient(cfg(),route(),store,client)
    assert first.complete_json(system='s',user='u') is None
    second=ExternalClient(cfg(),route(model='other/model:free'),store,client)
    assert second.retry_at()>time.time()+115
    assert second.complete_json(system='s',user='u') is None
    assert len(calls)==1 and calls[0].method=='GET'
    assert first.last_trace['attempts'][0]['generation_requests']==0


def test_unknown_key_quota_and_redirects_do_not_admit(tmp_path):
    calls=[]
    def reject(req):
        calls.append(req)
        return httpx.Response(302,headers={'Location':'https://other.test/steal'})
    c=ExternalClient(cfg(),route(),RuntimeStore(str(tmp_path/'state')),httpx.Client(transport=httpx.MockTransport(reject),follow_redirects=True))
    assert c.complete_json(system='s',user='u') is None
    assert len(calls)==1
    assert c.last_trace['attempts'][0]['http_status']==302


def test_schema_size_is_counted_not_truncated(tmp_path):
    calls=[]
    c=ExternalClient(cfg(),route(context_length=32768),RuntimeStore(str(tmp_path/'state')),transport(calls))
    c.decision_schema={'type':'object','description':'x'*40000}
    assert c.complete_json(system='s',user='u') is None
    assert not calls and len(c.decision_schema['description'])==40000


def test_authenticated_discovery_daily_limit_is_shared_not_per_model(tmp_path):
    path=tmp_path/'manifest.json'
    save_manifest(path,dict(version=1,observed_at=time.time(),routes=[route().model_dump(),route(model='other/model:free').model_dump()],
                            reports=[dict(provider='openrouter',daily_quota=dict(limit=1000,remaining=1000,used=0))]))
    p=ProviderPool(cfg(external_models_file=str(path)),None,RuntimeStore(str(tmp_path/'state')),transport([]))
    assert len(p.snapshot()['groups'])==1
    assert p.snapshot()['groups'][0]['requests_daily_remaining']==1000


def test_cloudflare_concurrent_unknown_reservations_cannot_exceed_budget(tmp_path):
    r=ExternalRoute.model_validate(cloudflare_routes([{'name':next(iter(CF_MODELS))}])[0][0])
    store=RuntimeStore(str(tmp_path/'state'))
    c=ExternalClient(cfg(cloudflare_daily_neuron_budget=100),r,store,transport([]))
    def reserve(_):
        try:return c._reserve_neurons('s','u',2048,None)
        except ValueError:return None
    with ThreadPoolExecutor(8) as executor:
        results=list(executor.map(reserve,range(8)))
    assert sum(x is not None for x in results)==1
    assert 0<c.accounting()['neurons_reserved_or_estimated']<=100


def test_malformed_manifest_cannot_break_existing_provider(tmp_path):
    path=tmp_path/'manifest.json';path.write_text('[]')
    p=ProviderPool(cfg(external_models_file=str(path),groq_api_key='existing-groq'),None,RuntimeStore(str(tmp_path/'state')),transport([]))
    assert [slot.provider for slot in p._slots]==['groq']


def test_specialized_guard_is_not_a_trading_decision_model():
    good,bad=openrouter_routes([model('test/safeguard:free')])
    assert not good and bad[0]['reason']=='specialized_non_decision_model'


def test_metadata_and_generation_share_one_deadline(tmp_path,monkeypatch):
    now=[time.time()];calls=[]
    def handle(req):
        calls.append(req)
        now[0]+=26
        return httpx.Response(200,json=dict(data=dict(free_model_daily_requests=dict(limit=50,used=0,remaining=50))))
    monkeypatch.setattr('trading.agents.external_client.time.time',lambda:now[0])
    c=ExternalClient(cfg(),route(),RuntimeStore(str(tmp_path/'state')),httpx.Client(transport=httpx.MockTransport(handle)))
    assert c.complete_json(system='s',user='u') is None
    assert len(calls)==1 and calls[0].url.path=='/api/v1/key'
    assert c.last_trace['attempts'][0]['error_category']=='stopped'
