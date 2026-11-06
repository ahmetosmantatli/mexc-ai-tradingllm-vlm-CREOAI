import httpx
import pytest
from trading.exchange.errors import ConnectionIssue
from trading.exchange.mexc_readonly import MexcReadOnlyClient
from trading.config import Settings


@pytest.mark.parametrize('code,part',[(406,'IP'),(602,'İmza'),(701,'View Account Details'),(703,'View Order Details'),(513,'saat'),(402,'süresi')])
def test_exchange_diagnostics_never_echo_provider_message(code,part):
    private='DO-NOT-ECHO-THE-SECRET'
    transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'success':False,'code':code,'message':private}))
    client=MexcReadOnlyClient(api_key='key',api_secret=private,_http=httpx.Client(base_url='https://api.mexc.com',transport=transport))
    with pytest.raises(ConnectionIssue) as caught:client.get_futures_account()
    assert part in str(caught.value)
    assert private not in str(caught.value)
    assert str(code) in str(caught.value)


def test_html_response_and_position_error_are_distinct():
    client=MexcReadOnlyClient(api_key='key',api_secret='secret',_http=httpx.Client(base_url='https://api.mexc.com',transport=httpx.MockTransport(lambda req:httpx.Response(403,text='secret-in-html'))))
    with pytest.raises(ConnectionIssue,match='HTTP 403') as caught:client.get_open_positions()
    assert 'secret-in-html' not in str(caught.value)


def test_zero_futures_balance_can_connect():
    def response(request):
        rows=[] if request.url.path.endswith('open_positions') else [dict(currency='USDT',equity=0,availableBalance=0,positionMargin=0,unrealized=0)]
        return httpx.Response(200,json=dict(success=True,code=0,data=rows))
    client=MexcReadOnlyClient(api_key='key',api_secret='secret',_http=httpx.Client(base_url='https://api.mexc.com',transport=httpx.MockTransport(response)))
    assert client.get_futures_account().equity==0
    assert client.get_open_positions()==[]


def test_new_futures_domain_default():
    assert Settings(_env_file=None).mexc_base_url=='https://api.mexc.com'
