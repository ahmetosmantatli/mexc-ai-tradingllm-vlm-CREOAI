from datetime import datetime,timezone
import json
from types import SimpleNamespace
import httpx
import pytest
from trading.agents.provider_limits import read_groq_headers,blocked,gemini_retry
from trading.agents.quota_routes import parse_routes
from trading.control.provider_status import catalog
from .test_provider_pool import settings,pool,response


def route(**kw):
    return dict(provider='groq',model='verified-text-model',modes=['text'],quota_bucket='verified-text-model',
        rpm=30,tpm=16000,rpd=2,tpd=200000,free_access_verified=True,**kw)


def test_gemini_resets_at_pacific_midnight_but_preserves_minute_window(tmp_path,monkeypatch):
    now=datetime(2026,11,8,8,0,1,tzinfo=timezone.utc).timestamp()
    clock=[now];monkeypatch.setattr('trading.agents.provider_pool.time.time',lambda:clock[0])
    p,store=pool(tmp_path,settings(groq_api_key='',gemini_api_key='private-gem',gemini_rpd=1,gemini_rpm=1))
    slot=p._slots[0];key=p._quota_key(slot,False)
    store.put(key,dict(rows=[dict(id='yesterday',at=now-2,tokens=100)],lease=None,last_used=0))
    row=next(r for r in p.snapshot()['groups'] if r['model']==p.settings.gemini_text_model)
    assert row['requests_daily_remaining']==1 and row['requests_minute_remaining']==0
    assert row['daily_window']=='pacific_calendar_day'
    assert p._reserve([slot],'x','y',700,None) is None
    clock[0]+=61
    assert p._reserve([slot],'x','y',700,None) is not None


def test_groq_not_blindly_reset_at_gemini_midnight(tmp_path,monkeypatch):
    now=datetime(2026,11,8,8,1,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr('trading.agents.provider_pool.time.time',lambda:now)
    p,store=pool(tmp_path,settings(groq_rpd=1))
    slot=p._slots[0];store.put(p._quota_key(slot,False),dict(rows=[dict(at=now-600,tokens=100)],lease=None,last_used=0))
    assert p._reserve([slot],'x','y',700,None) is None


def test_verified_alternate_model_uses_identical_context_when_base_exhausted(tmp_path):
    sent=[]
    cfg=settings(ai_free_model_routes=json.dumps([route()]),groq_rpd=1)
    p,store=pool(tmp_path,cfg,lambda req:(sent.append(json.loads(req.content)) or response()))
    base=p._slots[0];import time
    store.put(p._quota_key(base,False),dict(rows=[dict(at=time.time()-100,tokens=100)],lease=None,last_used=0))
    assert p.complete_json(system='complete policy',user='complete context')=={'action':'hold'}
    assert sent[0]['model']=='verified-text-model'
    assert sent[0]['messages'][0]['content']=='complete policy'
    assert sent[0]['messages'][1]['content'][0]['text']=='complete context'
    assert not p._supports(p._slots[1],True) # text route never receives VLM images


def test_shared_model_alias_bucket_does_not_multiply_quota(tmp_path):
    cfg=settings(ai_free_model_routes=json.dumps([dict(route(),quota_bucket='openai/gpt-oss-20b')]),groq_rpd=1)
    p,store=pool(tmp_path,cfg)
    base,alias=p._slots
    assert p._quota_key(base,False)==p._quota_key(alias,False)
    assert p._limits(alias,False)[2]==1
    assert p.complete_json(system='x',user='y')=={'action':'hold'}
    assert p.complete_json(system='x',user='y') is None


def test_unverified_or_invalid_quota_routes_never_load():
    for bad in [dict(route(),free_access_verified=False),dict(route(),rpm=0),dict(route(),modes=[]),{'model':'x'}]:
        with pytest.raises(ValueError,match='invalid verified'):
            parse_routes(json.dumps([bad]))


def test_groq_header_numbers_block_until_their_reset_and_do_not_retain_secrets():
    headers=httpx.Headers({'x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'2m3.5s',
        'x-ratelimit-remaining-tokens':'123','x-ratelimit-reset-tokens':'1s','Authorization':'SECRET'})
    data=read_groq_headers(headers,now=100)
    assert data['requests_reset_at']==223.5
    assert blocked(data,50,110) and not blocked(data,50,224)
    assert 'SECRET' not in json.dumps(data)


def test_last_groq_response_header_exhaustion_blocks_later_http(tmp_path):
    sent=[]
    def answer(req):
        sent.append(req)
        r=response();r.headers.update({'x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'1h'})
        return r
    p,store=pool(tmp_path,transport=answer)
    assert p.complete_json(system='x',user='y')=={'action':'hold'}
    assert p.complete_json(system='x',user='y') is None and len(sent)==1
    assert p.snapshot()['groups'][0]['provider_reported']['requests_remaining']==0


def test_gemini_structured_daily_failure_waits_until_next_pacific_day():
    now=datetime(2026,11,8,9,0,tzinfo=timezone.utc).timestamp()
    r=httpx.Response(429,json={'error':{'details':[
        {'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[{'quotaId':'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]},
        {'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'5s'}]}})
    delay,daily=gemini_retry(r,now=now)
    assert daily and delay==23*3600
    short=httpx.Response(429,json={'error':{'details':[{'@type':'google.rpc.RetryInfo','retryDelay':'5s'}]}})
    assert gemini_retry(short,now=now)==(5,False)


def test_catalog_only_uses_get_and_never_claims_free_access():
    calls=[]
    def answer(req):
        calls.append(req)
        assert req.method=='GET' and 'SECRET' not in str(req.url)
        if req.url.host=='api.groq.com':return httpx.Response(200,json={'data':[{'id':'test-text'}]})
        return httpx.Response(200,json={'models':[{'name':'models/test-gem','supportedGenerationMethods':['generateContent']} ]})
    cfg=SimpleNamespace(groq_api_key='SECRET-g',groq_api_key_2='',gemini_api_key='SECRET-m',gemini_api_key_2='')
    with httpx.Client(transport=httpx.MockTransport(answer)) as client:data=catalog(cfg,client)
    assert len(calls)==2 and all(r['quota_verified'] is False and r['free_access_verified'] is False for r in data)
    assert 'SECRET' not in json.dumps(data)


def test_telegram_quota_reset_label_is_provider_day_not_rolling_gemini():
    from trading.control.telegram import TelegramController
    stamp=datetime(2026,11,8,8,tzinfo=timezone.utc).timestamp()
    group=dict(provider='gemini',model='test',quota_group='group',requests_daily_remaining=500,next_daily_reset_at=stamp)
    fake=SimpleNamespace(snapshot_provider=lambda _:dict(provider_pool={'groups':[group]}),progress_text=lambda _: '',algorithm_text=lambda: '')
    text=TelegramController.quota_text(fake,'friend')
    assert '08.11 11:00' in text and 'Kalan günlük istek 500' in text


def test_gemma_native_preserves_system_context_images_and_validates_json(tmp_path):
    import base64
    image=tmp_path/'chart.png';image.write_bytes(b'chart-fixture')
    sent=[]
    def answer(req):
        sent.append(req);payload=json.loads(req.content)
        assert req.url.path.endswith('/gemma-4-26b-a4b-it:generateContent')
        assert payload['systemInstruction']['parts']==[{'text':'complete policy'}]
        assert payload['contents'][0]['parts'][0]['text']=='full numeric context'
        assert base64.b64decode(payload['contents'][0]['parts'][1]['inlineData']['data'])==b'chart-fixture'
        return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'{"action":"hold"}'}]}}],
            'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':20}})
    cfg=settings(groq_api_key='',gemini_api_key='SECRET',gemini_vision_model='gemma-4-26b-a4b-it')
    p,_=pool(tmp_path,cfg,answer)
    assert p.complete_json(system='complete policy',user='full numeric context',images=[str(image)])=={'action':'hold'}
    assert len(sent)==1


def test_configuration_script_preserves_secrets_and_does_not_enable_live(tmp_path):
    import runpy
    from pathlib import Path
    script=Path(__file__).resolve().parents[2]/'scripts'/'configure-free-models.py'
    module=runpy.run_path(str(script))
    env=tmp_path/'.env';env.write_text('MEXC_API_SECRET=SECRET\nLIVE_TRADING_ENABLED=false\nAI_PRIMARY_PROVIDER=anthropic\nAI_PRIMARY_PROVIDER=groq\n')
    backup=module['configure'](env)
    content=env.read_text()
    assert content.count('AI_PRIMARY_PROVIDER=')==1
    assert 'MEXC_API_SECRET=SECRET' in content and 'LIVE_TRADING_ENABLED=false' in content
    assert 'AI_CLAUDE_FALLBACK_ENABLED=false' in content and 'AI_FREE_MODEL_ROUTES=' in content
    assert 'SECRET' in backup.read_text()
    data=module['supplied_routes']()
    assert len(parse_routes(json.dumps(data)))==9
    gemma=[r for r in data if r['model'].startswith('gemma')]
    assert len({r['quota_bucket'] for r in gemma})==1
