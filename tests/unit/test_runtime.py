from types import SimpleNamespace
from time import time
import httpx
import pytest

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.graph import EvaluationGraph, size_proposal_from_signal
from trading.agents.schemas import GraphAction
from trading.config import Settings
from trading.exchange.account import FuturesAccount
from trading.exchange.contracts import ContractRules
from trading.exchange.hmac_signing import sign_payload
from trading.exchange.mexc_readonly import MexcReadOnlyClient
from trading.exchange.mexc_public import TickerSnapshot
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal, StrategyName
from trading.runtime.budget import Budget
from trading.runtime.paper import PaperLedger
from trading.runtime.runner import PaperRunner
from trading.runtime.store import RuntimeStore
from trading.runtime.telegram import Telegram


def cfg(**kw):
    return Settings(_env_file=None, **kw)


def signal(side=Side.LONG):
    return CandidateSignal(symbol='BTC_USDT', side=side, strategy=StrategyName.TREND,
        entry_price=100, stop_loss_price=98 if side==Side.LONG else 102,
        take_profit_price=104 if side==Side.LONG else 96, confidence=.8)


def rules(**kw):
    values = dict(size=.001, step=1, minimum=1, maximum=1e8, max_leverage=5,
                  maintenance=.005, price_step=.01)
    values.update(kw)
    return ContractRules(**values)


def ledger(tmp_path, seed=500, side=Side.LONG):
    store = RuntimeStore(str(tmp_path/'state.sqlite3'))
    book = PaperLedger(store, seed)
    proposal = size_proposal_from_signal(signal(side), equity=seed, risk_per_trade_pct=.01, target_leverage=3)
    book.open(proposal, rules(), 99.99, 100.01, 'signal-1', .01, .02)
    return book, proposal


def test_account_equity_is_not_available_and_zero_is_valid():
    payload = dict(success=True, code=0, data=[dict(currency='USDT', equity='500', availableBalance='300', positionMargin='190', unrealized='10')])
    a = FuturesAccount.parse(payload)
    assert a.equity == 500 and a.available == 300
    payload['data'][0].update(equity='0', availableBalance='0')
    assert FuturesAccount.parse(payload).equity == 0


@pytest.mark.parametrize('payload', [dict(success=False, code=602, data=[]), dict(success=True, data=[]), dict(success=True, data=[dict(currency='USDT', equity='NaN', availableBalance=1)])])
def test_invalid_account_never_becomes_seed(payload):
    with pytest.raises((ValueError, KeyError)):
        FuturesAccount.parse(payload)


def test_readonly_signature_and_method_allowlist(monkeypatch):
    monkeypatch.setattr('trading.exchange.mexc_readonly.current_timestamp_ms', lambda: 1700000000000)
    client = MexcReadOnlyClient(api_key='key', api_secret='secret')
    h = client.signed_headers(params={'symbol':'BTC_USDT'})
    assert h['Signature'] == sign_payload('secret', 'key1700000000000symbol=BTC_USDT')
    for method, path in [('POST','/api/v1/private/account/assets'), ('GET','/api/v1/private/account/transfer')]:
        with pytest.raises(ValueError):
            client.request(method, path)


@pytest.mark.parametrize('side,price,reason', [(Side.LONG,105,'TP'), (Side.LONG,97,'SL'), (Side.SHORT,95,'TP'), (Side.SHORT,103,'SL')])
def test_position_close_net_fees_and_restart(tmp_path, side, price, reason):
    book, _ = ledger(tmp_path, side=side)
    assert book.equity < 500 and book.available < book.equity
    restored = PaperLedger(RuntimeStore(str(tmp_path/'state.sqlite3')), 700)
    assert len(restored.positions)==1 and restored.equity==book.equity
    restored.mark('BTC_USDT', price-.01, price+.01, price)
    assert restored.positions=={}
    e = restored.state['events'][-1]
    assert e['reason']==reason
    assert restored.equity==pytest.approx(500+e['net_pnl'])
    assert restored.state['fees']>0
    assert restored.equity < 500 if reason=='SL' else restored.equity>500


def test_gap_can_liquidate_before_stop(tmp_path):
    book, _ = ledger(tmp_path)
    book.mark('BTC_USDT', 40, 40.01, 40)
    assert book.state['events'][-1]['reason']=='LIQ'
    assert book.state['liquidation_count']==1


def test_funding_charged_once_across_restart(tmp_path):
    book, _ = ledger(tmp_path)
    original = book.equity
    quantity = book.positions['BTC_USDT']['quantity']
    book.settle_funding('BTC_USDT', 1234, .001, 100)
    after = PaperLedger(book.store, 700)
    after.settle_funding('BTC_USDT', 1234, .001, 100)
    assert after.equity == pytest.approx(original-quantity*100*.001)


def test_same_signal_not_reopened_after_close(tmp_path):
    book, p = ledger(tmp_path)
    assert book.open(p, rules(), 99.99, 100.01, 'signal-1', .01, .02) is None
    book.mark('BTC_USDT', 105, 105.01, 105)
    assert book.open(p, rules(), 99.99, 100.01, 'signal-1', .01, .02) is None


def test_loss_budget_resizes_from_500_to_300(tmp_path):
    book, _ = ledger(tmp_path)
    first_qty=book.positions['BTC_USDT']['quantity']
    book.mark('BTC_USDT', 97,97.01,97)
    book.state['cash']=300
    book.save()
    p = size_proposal_from_signal(signal(), equity=book.equity, risk_per_trade_pct=.01,target_leverage=3)
    book.open(p,rules(),99.99,100.01,'next',.01,.02)
    assert book.positions['BTC_USDT']['quantity']<first_qty
    assert p.max_loss_if_stopped==3


def test_minimum_quantity_and_liquidation_buffer_rejected(tmp_path):
    store=RuntimeStore(str(tmp_path/'s'))
    book=PaperLedger(store,10)
    p=size_proposal_from_signal(signal(),equity=10,risk_per_trade_pct=.01,target_leverage=3)
    with pytest.raises(ValueError,match='quantity'):
        book.open(p,rules(minimum=10000),99.99,100.01,'one',.01,.02)
    with pytest.raises(ValueError,match='liquidation'):
        book.open(p,rules(),99.99,100.01,'two',.01,.4)
    assert book.positions=={} and book.equity==10


def test_budget_reservation_survives_restart_and_daily_cap(tmp_path):
    store=RuntimeStore(str(tmp_path/'s'))
    settings=cfg(anthropic_daily_budget_usd=.02,anthropic_total_budget_usd=.03)
    budget=Budget(store,settings)
    reservation=budget.reserve(.015)
    assert reservation
    assert Budget(RuntimeStore(str(tmp_path/'s')),settings).reserve(.01) is None
    budget.settle(reservation,100,100)
    assert budget.reserve(.01)
    assert store.get('ai_budget')['total']==pytest.approx(.0118)


def test_claude_error_holds_instead_of_stub_trade(monkeypatch):
    llm=AnthropicClient(settings=cfg(anthropic_api_key='test'))
    monkeypatch.setattr(llm,'complete_json',lambda **kw: None)
    result=EvaluationGraph(llm).evaluate_candidates([signal()],equity=500)
    assert result.action==GraphAction.HOLD


def test_no_candidate_makes_no_paid_call(monkeypatch):
    llm=AnthropicClient(settings=cfg(anthropic_api_key='test'))
    monkeypatch.setattr(llm,'complete_json',lambda **kw: pytest.fail('unexpected paid call'))
    assert EvaluationGraph(llm).evaluate_candidates([],equity=500).action==GraphAction.HOLD


def test_missing_credentials_never_uses_env_seed(tmp_path):
    runner=PaperRunner(cfg(runtime_db_path=str(tmp_path/'s'),starting_capital_try=700))
    result=runner.cycle()
    assert result['reason']=='HOLD_ACCOUNT_UNAVAILABLE'
    assert runner.ledger is None


def test_account_deposit_not_overwriting_existing_paper_pnl(tmp_path):
    store=RuntimeStore(str(tmp_path/'s'))
    account=SimpleNamespace(get_futures_account=lambda: FuturesAccount(500,500,0,0,time()),get_open_positions=lambda: [])
    runner=PaperRunner(cfg(mexc_api_key='x',mexc_api_secret='y'),store=store,account_client=account)
    runner.cycle()
    runner.ledger.state['cash']=300
    runner.ledger.save()
    account.get_futures_account=lambda: FuturesAccount(700,700,0,0,time())
    result=runner.cycle()
    assert result['real_account']['equity']==700
    assert result['paper']['equity']==300
    assert result['reason']=='HOLD_LOSS_CIRCUIT_BREAKER'


def test_stale_quote_refused(tmp_path):
    public=SimpleNamespace(get_ticker=lambda _: TickerSnapshot('BTC_USDT',100,99,101,1e6,raw=dict(timestamp=(time()-60)*1000,fairPrice=100)))
    runner=PaperRunner(cfg(),store=RuntimeStore(str(tmp_path/'s')),public=public)
    with pytest.raises(ValueError,match='stale'):
        runner.quote('BTC_USDT')


def test_telegram_retry_and_chat_allowlist(tmp_path):
    book,_=ledger(tmp_path)
    sent=[]
    def transport(request):
        import json
        body=json.loads(request.content)
        if request.url.path.endswith('getUpdates'):
            return httpx.Response(200,json=dict(ok=True,result=[dict(update_id=1,message=dict(chat=dict(id=999),text='/durum'))]))
        sent.append(body)
        return httpx.Response(200,json=dict(ok=True,result={}))
    settings=cfg(telegram_bot_token='fake',telegram_chat_id='123')
    telegram=Telegram(settings,book.store,httpx.Client(transport=httpx.MockTransport(transport)))
    telegram.tick(book,'status')
    assert len(sent)==2 # one open event and one scheduled status; unauthorized command ignored
    assert book.state['events'][0]['sent']
    telegram.tick(book,'status')
    assert len(sent)==2


def test_telegram_failure_does_not_lose_trade_event(tmp_path):
    book,_=ledger(tmp_path)
    telegram=Telegram(cfg(telegram_bot_token='fake',telegram_chat_id='1'),book.store,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))))
    telegram.tick(book,'status')
    assert not book.state['events'][0]['sent']
    assert not book.store.get('telegram_health')['ok']


def test_full_runner_open_close_restart_without_private_mutations(tmp_path, monkeypatch):
    from trading.agents.schemas import EvaluationResult, MarketAnalystNote, StrategyReviewNote, PortfolioAnalystNote
    from trading.quant.scanner import ScanResult
    now=time()
    selected=signal()
    class Public:
        def get_ticker(self,symbol):
            return TickerSnapshot(symbol,100,99.99,100.01,1e6,raw=dict(timestamp=time()*1000,fairPrice=100))
        def get_klines(self,*args,**kw):
            return {'candles':[[int(now)-1000,100,101,99,100,1]]}
        def get_contract_rules(self,symbol):
            return rules()
        def get_funding_history(self,symbol):
            return []
    settings=cfg(mexc_api_key='read',mexc_api_secret='secret',anthropic_api_key='fake', runtime_db_path=str(tmp_path/'s'))
    account=SimpleNamespace(get_futures_account=lambda:FuturesAccount(500,500,0,0,time()),get_open_positions=lambda:[])
    runner=PaperRunner(settings,public=Public(),account_client=account)
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**k:ScanResult(1,1,candidates=[selected]))
    proposal=size_proposal_from_signal(selected,equity=500,risk_per_trade_pct=.01,target_leverage=3)
    monkeypatch.setattr(runner.graph,'evaluate_candidates',lambda *a,**k:EvaluationResult(action=GraphAction.TRADE,proposal=proposal,
        selected_signal=selected,market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),portfolio=PortfolioAnalystNote(mode='llm')))
    assert runner.cycle()['reason']=='PAPER_OPENED'
    assert len(runner.ledger.positions)==1
    restarted=PaperRunner(settings,public=Public(),account_client=account)
    assert len(restarted.ledger.positions)==1
    restarted.public.get_ticker=lambda symbol:TickerSnapshot(symbol,105,104.99,105.01,1e6,raw=dict(timestamp=time()*1000,fairPrice=105))
    result=restarted.cycle()
    assert result['paper']['open_positions']==0
    assert restarted.ledger.state['events'][-1]['reason']=='TP'
    assert result['paper']['equity']>500


def test_monitor_stop_still_runs_when_account_unavailable(tmp_path):
    book,_=ledger(tmp_path)
    public=SimpleNamespace(get_ticker=lambda symbol:TickerSnapshot(symbol,97,96.99,97.01,1e6,raw=dict(timestamp=time()*1000,fairPrice=97)),get_funding_history=lambda symbol:[])
    runner=PaperRunner(cfg(),store=book.store,public=public)
    result=runner.cycle()
    assert result['reason']=='HOLD_ACCOUNT_UNAVAILABLE'
    assert not runner.ledger.positions
    assert runner.ledger.state['events'][-1]['reason']=='SL'
