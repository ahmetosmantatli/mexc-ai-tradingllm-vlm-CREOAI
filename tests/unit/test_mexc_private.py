import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from trading.exchange.mexc_private import MexcPrivateClient,MexcPrivateError


def setup(handler=None,**kwargs):
    requests=[]
    def call(request):
        requests.append(request)
        return handler(request) if handler else httpx.Response(200,json={'success':True,'code':0,'data':{'orderId':'123'}})
    client=MexcPrivateClient('PRIVATE_KEY','PRIVATE_SECRET',client=httpx.Client(transport=httpx.MockTransport(call)),**kwargs)
    return client,requests


def market(client,**kwargs):
    return client.submit_market(**dict(dict(symbol='BTC_USDT',side='long',contracts=2.,leverage=3,
        external_id='intent_123',stop_loss=90.,take_profit=110.),**kwargs))


def test_exact_post_signed_bytes_and_native_protection(monkeypatch):
    monkeypatch.setattr('trading.exchange.mexc_private.time.time',lambda:1700000000.)
    client,requests=setup(writes_enabled=True)
    assert market(client)=={'orderId':'123'}
    request=requests[0]
    assert request.url.path=='/api/v1/private/order/create'
    signed=b'PRIVATE_KEY1700000000000'+request.content
    assert request.headers['signature']==hmac.new(b'PRIVATE_SECRET',signed,hashlib.sha256).hexdigest()
    body=json.loads(request.content)
    assert body['side']==1 and body['openType']==1 and body['type']==5
    assert body['externalOid']=='intent_123'
    assert body['stopLossPrice']==90. and body['takeProfitPrice']==110.
    assert body['lossTrend']==body['profitTrend']==2
    assert 'PRIVATE' not in repr(client)


def test_get_sorting_encoding_and_path_excluded_from_sign(monkeypatch):
    monkeypatch.setattr('trading.exchange.mexc_private.time.time',lambda:1700000000.)
    client,requests=setup(lambda req:httpx.Response(200,json=dict(success=True,code=0,data=[])))
    client.protection_orders('BTC_USDT',page_num=2,page_size=50)
    req=requests[-1]
    assert req.url.query==b'is_finished=0&page_num=2&page_size=50&symbol=BTC_USDT'
    assert req.headers['Signature']==hmac.new(b'PRIVATE_SECRET',b'PRIVATE_KEY1700000000000'+req.url.query,hashlib.sha256).hexdigest()
    client.client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=dict(success=True,code=0,data={}))))
    # IDs embedded in the path do not enter the parameter signature.
    assert client.order_by_external_id('BTC_USDT','intent_123')=={}


def test_writes_disabled_by_default_and_callback_rechecked_no_http():
    client,requests=setup()
    with pytest.raises(MexcPrivateError,match='writes_disabled'):
        market(client)
    assert not requests
    client.writes_enabled=True
    calls=[]
    client.can_write=lambda:calls.append(1) or len(calls)==1
    with pytest.raises(MexcPrivateError,match='writes_disabled'):
        market(client)
    assert not requests and len(calls)==2


@pytest.mark.parametrize('mode,side,expected',[(1,'long',4),(1,'short',2),(2,'long',4),(2,'short',2)])
def test_close_never_uses_open_side_and_oneway_reduceonly(mode,side,expected):
    client,requests=setup(writes_enabled=True)
    client.close_position(symbol='BTC_USDT',side=side,contracts=2,position_id=45,external_id='close_123',position_mode=mode)
    body=json.loads(requests[-1].content)
    assert body['side']==expected and body['positionId']==45
    assert body.get('reduceOnly')==(True if mode==2 else None)
    assert body['externalOid']=='close_123'


@pytest.mark.parametrize('error,ambiguous',[(httpx.ReadTimeout('PRIVATE_SECRET'),True),(500,True),(408,True),(429,False),(401,False)])
def test_write_errors_are_sanitized_not_retried(error,ambiguous):
    def handler(req):
        if isinstance(error,Exception):raise error
        return httpx.Response(error,json={'message':'PRIVATE_SECRET','request':'PRIVATE_KEY'})
    client,requests=setup(handler,writes_enabled=True)
    with pytest.raises(MexcPrivateError) as caught:
        market(client)
    assert caught.value.ambiguous is ambiguous
    assert len(requests)==1
    assert 'PRIVATE' not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('payload', ['invalid-json',{'success':True,'code':0,'data':None},{'weird':'PRIVATE_SECRET'},[]])
def test_unparseable_success_write_is_ambiguous(payload):
    def handler(req):
        return httpx.Response(200,content=payload) if isinstance(payload,str) else httpx.Response(200,json=payload)
    client,requests=setup(handler,writes_enabled=True)
    with pytest.raises(MexcPrivateError) as caught:
        market(client)
    assert caught.value.ambiguous and len(requests)==1


def test_business_rejection_safe_numeric_code():
    client,requests=setup(lambda req:httpx.Response(200,json={'success':False,'code':700007,'message':'PRIVATE_KEY PRIVATE_SECRET'}),writes_enabled=True)
    with pytest.raises(MexcPrivateError) as caught:
        market(client)
    assert caught.value.code==700007 and caught.value.ambiguous is False
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('updates',[dict(contracts=True),dict(leverage=True),dict(leverage=2.5),
    dict(contracts=float('nan')),dict(symbol='../transfer'),dict(external_id='x?PRIVATE_KEY'),
    dict(stop_loss=0),dict(stop_loss=111),dict(position_mode=3),dict(position_mode=True),dict(position_mode=1.0)])
def test_invalid_order_has_no_http(updates):
    client,requests=setup(writes_enabled=True)
    with pytest.raises((ValidationError,ValueError)):
        market(client,**updates)
    assert not requests


def test_pagination_and_fixed_origin_no_credential_redirect():
    with pytest.raises(ValueError):
        MexcPrivateClient('a','b',base_url='https://attacker.invalid')
    client,requests=setup(lambda req:httpx.Response(302,headers={'location':'https://attacker.invalid'}))
    with pytest.raises(MexcPrivateError):
        client.account_assets()
    assert len(requests)==1
    with pytest.raises(ValueError):
        client.current_orders(page_size=101)
    assert len(requests)==1


def test_read_endpoints_return_data_without_wrappers_and_use_documented_paths():
    reads={
        '/api/v1/private/account/assets':[{'currency':'USDT','equity':50,'availableBalance':40}],
        '/api/v1/private/position/open_positions':[],
        '/api/v1/private/order/list/open_orders':[],
        '/api/v1/private/position/leverage':[],
        '/api/v1/private/position/position_mode':1,
        '/api/v1/private/account/risk_limit':{'BTC_USDT':[]},
        '/api/v1/private/stoporder/list/orders':[],
    }
    client,requests=setup(lambda req:httpx.Response(200,json=dict(success=True,code=0,data=reads[req.url.path])))
    assert client.account_assets()[0]['equity']==50
    assert client.open_positions('BTC_USDT')==[]
    assert client.current_orders()==[]
    assert client.leverage('BTC_USDT')==[]
    assert client.position_mode()==1
    assert client.risk_limits('BTC_USDT')=={'BTC_USDT':[]}
    assert client.protection_orders('BTC_USDT')==[]
    assert all(req.method=='GET' for req in requests)


def test_history_positions_are_a_get_and_report_exchange_fee_and_funding(monkeypatch):
    monkeypatch.setattr('trading.exchange.mexc_private.time.time',lambda:1700000000.)
    row=dict(positionId=42,symbol='BTC_USDT',state=3,closeVol=1,fee=-0.009,holdFee=-0.02)
    client,requests=setup(lambda req:httpx.Response(200,json=dict(success=True,code=0,data=[row])))
    assert client.history_positions('BTC_USDT')==[row]
    request=requests[0]
    assert request.method=='GET'
    assert request.url.path=='/api/v1/private/position/list/history_positions'
    assert b'symbol=BTC_USDT' in request.url.query
    assert request.content==b''


def test_native_position_protection_no_reverse_market_order_and_modify():
    client,requests=setup(writes_enabled=True)
    client.place_protection(position_id=45,contracts=2,stop_loss=90,take_profit=110,request_id='protect_123')
    body=json.loads(requests[-1].content)
    assert requests[-1].url.path=='/api/v1/private/stoporder/place'
    assert body['takeProfitReverse']==body['stopLossReverse']==2
    assert body['takeProfitType']==body['stopLossType']==0
    assert 'externalOid' not in body # Not supported by this endpoint; journal ID is local.
    client.modify_protection(protection_id=67,stop_loss=95,take_profit=110,request_id='modify_123')
    assert requests[-1].url.path=='/api/v1/private/stoporder/change_plan_price'
    assert json.loads(requests[-1].content)['stopPlanOrderId']==67
