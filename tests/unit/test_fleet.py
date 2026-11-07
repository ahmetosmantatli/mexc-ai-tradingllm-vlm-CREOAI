from concurrent.futures import ThreadPoolExecutor
from trading.config import Settings
from trading.control import Account
from trading.runtime.fleet import Fleet
from trading.runtime.store import RuntimeStore
from trading.runtime.budget import Budget
from trading.runtime.paper import PaperLedger
from trading.models.domain import Side
from trading.agents.graph import size_proposal_from_signal
from trading.exchange.contracts import ContractRules
from trading.quant.signals import CandidateSignal, StrategyName


def test_fleet_account_variant_isolation_and_inactive_visibility(tmp_path,monkeypatch):
    accounts=[Account('a',1,1,'ka','sa'),Account('b',2,2,'kb','sb')]
    monkeypatch.setattr('trading.runtime.fleet.load_accounts',lambda *a,**kw:accounts)
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),shared_budget_db_path=str(tmp_path/'budget'),telegram_bot_token='fake')
    fleet=Fleet(cfg)
    try:
        assert len(fleet.runners)==4
        a=fleet.runners[('a','text')]
        a.ledger=PaperLedger(a.store,500)
        fleet.runners[('b','text')].ledger=PaperLedger(fleet.runners[('b','text')].store,300)
        a.status('HOLD_TELEGRAM_PAUSED')
        fleet.last_publish['a']=float('inf')
        fleet.publish(accounts[0])
        snapshot=fleet.control_store.get('account:a:status')
        assert snapshot['variants']['text']['paper']['equity']==500
        assert '300' not in fleet.controller.status_text('a')
        fleet.control_store.put('account:a:control',dict(enabled=False,version='vlm',risk_per_trade_pct=.01))
        fleet.publish(accounts[0])
        assert fleet.control_store.get('account:a:status')['variants']['text']['paper']['equity']==500
        assert fleet.runners[('a','vlm')].ledger is None
        assert not fleet.entry_control('a','text')['enabled']
    finally:
        fleet.stop()


def test_shared_budget_concurrent_accounts_cannot_reserve_over_cap(tmp_path):
    cfg=Settings(_env_file=None,anthropic_total_budget_usd=.03,anthropic_daily_budget_usd=.03)
    budgets=[Budget(RuntimeStore(str(tmp_path/'shared')),cfg) for _ in range(4)]
    with ThreadPoolExecutor(4) as pool:
        reservations=list(pool.map(lambda b:b.reserve(.02),budgets))
    assert sum(r is not None for r in reservations)==1
    assert budgets[0].store.get('ai_budget')['total']==.02


def test_closed_position_funding_is_reconciled_once(tmp_path):
    book=PaperLedger(RuntimeStore(str(tmp_path/'paper')),500)
    signal=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    proposal=size_proposal_from_signal(signal,equity=500,risk_per_trade_pct=.01,target_leverage=3)
    book.open(proposal,ContractRules(.001,1,1,1e8,5,.005,.01),99.99,100.01,'one',.01,.02)
    p=book.positions['BTC_USDT']
    p['opened_at']=1000
    qty=p['quantity']
    book.mark('BTC_USDT',105,105.01,105)
    before=book.equity
    book.reconcile_closed_funding('BTC_USDT',1000,2000,.001,100)
    book.reconcile_closed_funding('BTC_USDT',1000,2000,.001,100)
    assert abs(book.equity-(before-qty*.1))<1e-8
    assert book.state['events'][-1]['kind']=='FUNDING_ADJUSTMENT'


def test_trader_profiles_change_effective_engine_and_limits():
    from types import SimpleNamespace
    from trading.config import RiskProfile
    runner=SimpleNamespace(cfg=Settings(_env_file=None))
    Fleet.apply_trader_mode(runner,{'risk_profile':'aggressive_growth','risk_per_trade_pct':.03})
    assert runner.cfg.risk_per_trade_pct==.03
    assert runner.cfg.paper_max_leverage==10
    assert runner.cfg.paper_max_positions==4
    assert runner.risk.preset.profile==RiskProfile.AGGRESSIVE_GROWTH
    Fleet.apply_trader_mode(runner,{'risk_profile':'capital_preservation','risk_per_trade_pct':.03})
    assert runner.cfg.risk_per_trade_pct==.005
    assert runner.cfg.paper_max_positions==2
    assert runner.cfg.daily_loss_limit_pct==.02


def test_demo_capital_no_mexc_credentials_or_deposit(tmp_path,monkeypatch):
    account=Account('demo',1,1,'','')
    monkeypatch.setattr('trading.runtime.fleet.load_accounts',lambda *a,**kw:[account])
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),shared_budget_db_path=str(tmp_path/'budget'),telegram_bot_token='fake',anthropic_api_key='')
    fleet=Fleet(cfg)
    try:
        fleet.apply_capital(account,dict(id='seed',mode='demo',amount=1000,allocation_mode='fixed'))
        runner=fleet.runners[('demo','text')]
        assert not runner.cfg.require_mexc_balance
        assert runner.cfg.paper_seed_usdt==1000
        runner.account_client.get_futures_account=lambda:(_ for _ in ()).throw(AssertionError('must not fetch real account'))
        result=runner.cycle()
        assert result['paper']['equity']==1000
        assert result['real_account'] is None
        assert not result['live_orders_enabled']
    finally:fleet.stop()


def test_account_capital_fixed_slice_and_reject_excess(tmp_path,monkeypatch):
    import time
    from trading.exchange.account import FuturesAccount
    account=Account('slice',1,1,'key','secret')
    monkeypatch.setattr('trading.runtime.fleet.load_accounts',lambda *a,**kw:[account])
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),shared_budget_db_path=str(tmp_path/'budget'),telegram_bot_token='fake')
    fleet=Fleet(cfg)
    try:
        old=fleet.runners[('slice','text')]
        old.account_client.get_futures_account=lambda:FuturesAccount(10000,9000,1000,0,time.time())
        import pytest
        with pytest.raises(ValueError,match='aşıyor'):
            fleet.apply_capital(account,dict(id='bad',mode='account',amount=10000,allocation_mode='fixed'))
        fleet.apply_capital(account,dict(id='good',mode='account',amount=1000,allocation_mode='fixed'))
        for version in ('text','vlm'):
            assert fleet.runners[('slice',version)].cfg.paper_allocation_usdt==1000
        assert fleet.controller.control('slice')['capital_chosen']
        assert not fleet.controller.control('slice')['enabled']
    finally:fleet.stop()


def test_stop_updates_tighten_only_and_queue_notification(tmp_path):
    book=PaperLedger(RuntimeStore(str(tmp_path/'p')),500)
    signal=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    proposal=size_proposal_from_signal(signal,equity=500,risk_per_trade_pct=.01,target_leverage=3)
    book.open(proposal,ContractRules(.001,1,1,1e8,5,.005,.01),99.99,100.01,'one',.01,.02)
    p=book.positions['BTC_USDT'];opened=p['opened_at']
    assert not book.update_stop('BTC_USDT',97,102,102.01,opened,'widen',1)
    assert book.update_stop('BTC_USDT',100.5,102,102.01,opened,'confirmed structure',2)
    assert book.positions['BTC_USDT']['initial_stop']==98
    assert book.state['events'][-1]['kind']=='STOP_UPDATED'
    assert not book.update_stop('BTC_USDT',101,102,102.01,opened,'duplicate evidence',2)


def test_invalid_management_evidence_never_mutates_stop(tmp_path):
    import pytest
    book=PaperLedger(RuntimeStore(str(tmp_path/'p')),500)
    signal=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    proposal=size_proposal_from_signal(signal,equity=500,risk_per_trade_pct=.01,target_leverage=3)
    book.open(proposal,ContractRules(.001,1,1,1e8,5,.005,.01),99.99,100.01,'one',.01,.02)
    p=book.positions['BTC_USDT']
    with pytest.raises(ValueError):
        book.update_stop('BTC_USDT',100.5,102,102.01,p['opened_at'],'bad evidence',float('nan'))
    assert p['stop']==98
    assert book.store.get('paper')['positions']['BTC_USDT']['stop']==98


def test_management_persistence_failure_latches_new_entry_gate(tmp_path,monkeypatch):
    import sqlite3,time
    from types import SimpleNamespace
    store=RuntimeStore(str(tmp_path/'p'))
    book=SimpleNamespace(positions={'BTC_USDT':{'opened_at':1}},update_stop=lambda *a:(_ for _ in ()).throw(sqlite3.OperationalError('disk')))
    runner=SimpleNamespace(lock=store.lock,ledger=book)
    public=SimpleNamespace(get_klines=lambda *a,**kw:{'candles':[]},get_ticker=lambda *a:SimpleNamespace(bid1=102,ask1=102.01,raw={'timestamp':time.time()*1000}),close=lambda:None)
    monkeypatch.setattr('trading.runtime.fleet.MexcPublicClient',lambda **kw:public)
    monkeypatch.setattr('trading.runtime.management.propose_management',lambda *a,**kw:dict(action='tighten_stop',new_stop=100.5,evidence_at=time.time()-1))
    fleet=Fleet.__new__(Fleet)
    fleet.cfg=Settings(_env_file=None)
    fleet.manage_positions(('a','text'),runner)
    assert runner.management_fatal
