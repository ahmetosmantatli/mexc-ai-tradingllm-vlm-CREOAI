"""No network: shared pacing/cache and 510 read cooldown, never retry storms."""
import threading
from types import SimpleNamespace
import httpx
import pytest
from trading.exchange.public_governor import PublicGovernor,PublicDataDeferred
from trading.exchange.mexc_public import MexcPublicClient,PublicMarketError
from trading.quant.scanner import MarketScanner


class Clock:
    def __init__(self):self.value=3001.
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


def client(handler,governor):return MexcPublicClient(transport=httpx.MockTransport(handler),governor=governor)


def test_shared_pacing_applies_across_clients_and_never_sleeps_under_db_lock():
    clock=Clock();governor=PublicGovernor(clock=clock.now,sleep=clock.sleep);calls=[]
    def handler(req):
        calls.append(clock.now());return httpx.Response(200,json={'success':True,'code':0,'data':{}})
    a=client(handler,governor);b=client(handler,governor)
    a.get_ticker('BTC_USDT');b.get_ticker('ETH_USDT');a.get_ticker('SOL_USDT')
    assert len(calls)==3 and calls[1]-calls[0]>=1/3-1e-9 and calls[2]-calls[1]>=1/3-1e-9


def test_510_cools_all_public_clients_without_automatic_retry():
    clock=Clock();governor=PublicGovernor(clock=clock.now,sleep=clock.sleep);calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json={'success':False,'code':510,'message':'PRIVATE RAW'}) if len(calls)==1 else httpx.Response(200,json={'code':0,'data':{}})
    a,b=client(handler,governor),client(handler,governor)
    with pytest.raises(PublicMarketError):a.get_ticker('BTC_USDT')
    with pytest.raises(PublicDataDeferred):b.get_ticker('ETH_USDT')
    assert len(calls)==1
    clock.sleep(31);b.get_ticker('ETH_USDT');assert len(calls)==2


def test_metadata_cache_is_shared_immutable_and_excludes_non_usdt_inactive_contracts():
    clock=Clock();governor=PublicGovernor(clock=clock.now,sleep=clock.sleep);calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json={'code':0,'data':[
            dict(symbol='BTC_USDT',state=0,quoteCoin='USDT',settleCoin='USDT'),
            dict(symbol='FIL_USDC',state=0,quoteCoin='USDC',settleCoin='USDC'),
            dict(symbol='PAUSED_USDT',state=1,quoteCoin='USDT',settleCoin='USDT')]})
    a,b=client(handler,governor),client(handler,governor)
    assert a.list_contract_symbols()==['BTC_USDT']
    first=b._json('GET','/api/v1/contract/detail');first['data'][0]['symbol']='CHANGED'
    assert b.list_contract_symbols()==['BTC_USDT'] and len(calls)==1


def test_kline_cache_expires_at_bar_boundary_not_after_forming_candle_becomes_closed(monkeypatch):
    clock=Clock();governor=PublicGovernor(clock=clock.now,sleep=clock.sleep);calls=[]
    monkeypatch.setattr('trading.exchange.mexc_public.time.time',clock.now)
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json={'code':0,'data':[[3000,100,102,99,100+len(calls),10]]})
    a=client(handler,governor)
    assert a.get_klines('BTC_USDT','Min5')['candles'][0][4]==101
    assert a.get_klines('BTC_USDT','Min5')['candles'][0][4]==101 and len(calls)==1
    clock.sleep(300)
    assert a.get_klines('BTC_USDT','Min5')['candles'][0][4]==102 and len(calls)==2


def test_bulk_scanner_uses_one_price_request_not_per_symbol_fanout():
    from trading.exchange.mexc_public import TickerSnapshot
    class Public:
        use_fixtures=True
        calls=0
        def list_fee_free_candidates(self):return []
        def list_contract_symbols(self):return ['BTC_USDT','ETH_USDT']
        def get_all_tickers(self):
            self.calls+=1
            return [TickerSnapshot(s,100,99.99,100,1e6) for s in self.list_contract_symbols()]
        def get_ticker(self,s):pytest.fail('per-symbol ticker request in bulk scanner')
        def get_klines(self,*a,**kw):return {'candles':[[i,100,101,99,100,1] for i in range(40)]}
    public=Public();result=MarketScanner(public,bulk_tickers=True).scan()
    assert result.scanned==2 and result.kept==2 and public.calls==1


def test_bulk_failure_does_not_fall_back_to_many_individual_requests():
    class Public:
        def list_fee_free_candidates(self):return []
        def list_contract_symbols(self):return ['BTC_USDT','ETH_USDT']
        def get_all_tickers(self):raise PublicMarketError(510)
        def get_ticker(self,s):pytest.fail('retry fanout')
    result=MarketScanner(Public(),bulk_tickers=True).scan()
    assert not result.candidates and result.errors=={'bulk_tickers':'MEXC_CODE_510'}


def test_universe_cooldown_returns_scan_status_instead_of_crashing_worker():
    class Public:
        def list_fee_free_candidates(self):return []
        def list_contract_symbols(self):raise PublicDataDeferred(30)
    result=MarketScanner(Public(),bulk_tickers=True).scan()
    assert result.scanned==0 and result.errors=={'universe':'PUBLIC_COOLDOWN'}


def test_429_honors_retry_after_across_public_clients():
    clock=Clock();governor=PublicGovernor(clock=clock.now,sleep=clock.sleep);calls=[]
    def handler(req):
        calls.append(req);return httpx.Response(429,headers={'retry-after':'120'})
    a,b=client(handler,governor),client(handler,governor)
    with pytest.raises(httpx.HTTPStatusError):a.get_ticker('BTC_USDT')
    clock.sleep(31)
    with pytest.raises(PublicDataDeferred):b.get_ticker('ETH_USDT')
    assert len(calls)==1 and governor.snapshot()['cooldown_seconds']==89
