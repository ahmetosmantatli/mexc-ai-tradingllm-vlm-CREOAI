import base64
import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from trading.agents.gemini_client import GeminiClient
from trading.evaluation.service import ModelDecision
from trading.runtime.store import RuntimeStore


def decision():
    return dict(action='hold',signal_id=None,rationale='evidence',invalidation='level',watch_note='wait')


def setup(tmp_path,handler,**overrides):
    cfg=SimpleNamespace(gemini_api_key='fake-gemini',gemini_text_model='text-model',
        gemini_vision_model='vision-model',gemini_max_completion_tokens=2048)
    for key,value in overrides.items():setattr(cfg,key,value)
    client=GeminiClient(cfg,RuntimeStore(str(tmp_path/'state')),
        httpx.Client(transport=httpx.MockTransport(handler)))
    client.response_validator=ModelDecision.model_validate
    return client


def good_response():
    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(decision())}}],
        'usage':{'prompt_tokens':100,'completion_tokens':20}})


@pytest.mark.parametrize('vision',[False,True])
def test_complete_context_and_three_images_are_preserved(tmp_path,vision):
    sent=[]
    def handler(request):
        sent.append(request)
        return good_response()
    client=setup(tmp_path,handler)
    paths=[]
    if vision:
        for i in range(3):
            path=tmp_path/f'{i}.png';path.write_bytes(b'image'+bytes([i]));paths.append(str(path))
    assert client.complete_json(system='exact system',user='exact market memory',images=paths)==decision()
    req=sent[0];body=json.loads(req.content)
    assert str(req.url)==GeminiClient.ENDPOINT
    assert req.headers['authorization']=='Bearer fake-gemini'
    assert body['model']==('vision-model' if vision else 'text-model')
    assert body['messages'][0]==dict(role='system',content='exact system')
    content=body['messages'][1]['content']
    assert content[0]==dict(type='text',text='exact market memory')
    assert len(content)==1+len(paths)
    for i,path in enumerate(paths):
        assert base64.b64decode(content[i+1]['image_url']['url'].split(',')[1])==Path(path).read_bytes()
    assert body['max_tokens']==2048 and body['response_format']=={'type':'json_object'}
    assert not {'reasoning_effort','include_reasoning','max_completion_tokens'}&body.keys()
    assert client.last_attempt['input_tokens']==100
    assert client.last_attempt['estimated_cost_usd'] is None
    assert client.store.get('gemini_usage')['successful_calls']==1


def test_rate_limit_shared_cooldown_and_distinct_models(tmp_path):
    sent=[]
    def handler(request):
        sent.append(request)
        return httpx.Response(429,headers={'retry-after':'120'},json={'error':{'message':'private details'}})
    client=setup(tmp_path,handler)
    assert client.complete_json(system='json',user='context') is None
    assert client.last_attempt['generation_requests']==1
    assert client.last_attempt['error_category']=='rate_limit'
    other=GeminiClient(client.settings,client.store,client.client)
    assert other.complete_json(system='json',user='context') is None
    assert other.last_attempt['status']=='cooldown_skipped'
    assert len(sent)==1
    path=tmp_path/'image.png';path.write_bytes(b'image')
    assert other.complete_json(system='json',user='context',images=[str(path)]) is None
    assert len(sent)==2


@pytest.mark.parametrize('condition',['stopped','missing_key'])
def test_stop_or_missing_key_does_not_send(tmp_path,condition):
    client=setup(tmp_path,lambda req:pytest.fail('unexpected network'))
    if condition=='stopped':client.can_call=lambda:False
    else:client.settings.gemini_api_key=''
    assert client.complete_json(system='JSON',user='context') is None
    assert client.last_status==condition
    assert client.last_attempt['generation_requests']==0


def test_invalid_schema_keeps_usage_without_raw_private_content(tmp_path):
    secret='DO_NOT_STORE_THIS'
    client=setup(tmp_path,lambda req:httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'action':secret})}}],
        'usage':{'prompt_tokens':17,'completion_tokens':4}}))
    assert client.complete_json(system='JSON',user=secret) is None
    assert client.last_attempt['input_tokens']==17
    assert client.last_attempt['error_category']=='invalid_output_or_local_validation'
    assert secret not in json.dumps(client.last_attempt)
    assert client.store.get('gemini_usage') is None


def test_http_error_does_not_store_provider_message(tmp_path):
    secret='PRIVATE_PROMPT_OR_KEY'
    client=setup(tmp_path,lambda req:httpx.Response(400,json={'error':{'message':secret,'code':400}}))
    assert client.complete_json(system='JSON',user='context') is None
    assert client.last_attempt['http_status']==400
    assert secret not in json.dumps(client.last_attempt)


def test_safe_domain_validation_reason_is_visible_without_model_output(tmp_path):
    client=setup(tmp_path,lambda request:good_response())
    def reject(payload):raise ValueError('independent watch trade requires a fresh price plan')
    client.response_validator=reject
    assert client.complete_json(system='JSON',user='context') is None
    assert client.last_attempt['validation_issues']==[dict(field='response',type='watch_requires_fresh_price_plan')]
    assert client.last_attempt['input_tokens']==100
