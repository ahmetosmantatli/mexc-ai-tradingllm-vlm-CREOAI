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


def test_telegram_demo_to_trade_stop_update_close_report_and_restart(tmp_path,monkeypatch):
    """Entire paper lifecycle without a real wallet or provider call."""
    import time,httpx,json
    from trading.agents.schemas import EvaluationResult,GraphAction,MarketAnalystNote,StrategyReviewNote,PortfolioAnalystNote
    from trading.exchange.mexc_public import TickerSnapshot
    from trading.quant.scanner import ScanResult
    monkeypatch.setattr('trading.runtime.fleet.PositionProtection.start',lambda self:self)
    account=Account('demo',7,7,'','')
    monkeypatch.setattr('trading.runtime.fleet.load_accounts',lambda *a,**kw:[account])
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),shared_budget_db_path=str(tmp_path/'budget'),telegram_bot_token='fake',anthropic_api_key='fake')
    fleet=Fleet(cfg)
    calls=[]
    def transport(req):
        body=json.loads(req.content)
        calls.append((req.url.path.rsplit('/',1)[-1],body))
        return httpx.Response(200,json={'ok':True,'result':[] if req.url.path.endswith('getUpdates') else {'message_id':1}})
    fleet.controller.client=httpx.Client(transport=httpx.MockTransport(transport))
    def message(text):
        fleet.controller.dispatch({'message':{'chat':{'id':7,'type':'private'},'from':{'id':7},'text':text}})
    try:
        message('/demo 500')
        assert fleet.controller.control('demo')['capital_pending']
        fleet.execute_commands(account)
        fleet.controller.flush()
        setup_message=next(body for method,body in calls if method=='sendMessage' and 'PAPER kasası hazır' in body.get('text',''))
        assert '2/4' in setup_message['text']
        assert {b['callback_data'] for row in setup_message['reply_markup']['inline_keyboard'] for b in row} >= {'control:text','control:vlm'}
        message('/version text')
        fleet.controller._command('demo','trader_balanced')
        message('/run')
        assert fleet.controller.control('demo')['enabled']
        runner=fleet.runners[('demo','text')]
        runner.external_scan=False  # Exercise the standalone synchronous test path.
        Fleet.apply_trader_mode(runner,fleet.controller.control('demo'))
        fleet.funding_health[('demo','text')]=True
        control_provider=runner.external_control
        runner.external_control=lambda:{'enabled':False}
        runner.cycle() # Seed without network or model calls.
        runner.external_control=control_provider
        runner.protection.tick()
        signal=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
        class Public:
            use_fixtures=True
            def get_ticker(self,symbol):return TickerSnapshot(symbol,100,99.99,100.01,1e6,raw={'timestamp':time.time()*1000,'fairPrice':100})
            def get_klines(self,*a,**kw):return {'candles':[[int(time.time())-1000,100,101,99,100,1]]}
            def get_contract_rules(self,symbol):return ContractRules(.001,1,1,1e8,5,.005,.01)
        runner.public=Public()
        monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(1,1,candidates=[signal]))
        proposal=size_proposal_from_signal(signal,equity=500,risk_per_trade_pct=.01,target_leverage=5)
        monkeypatch.setattr(runner.graph,'evaluate_candidates',lambda *a,**kw:EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=signal,market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),portfolio=PortfolioAnalystNote(mode='llm'),metadata={'decision_ai':{'provider':'anthropic','model':'claude-sonnet-4-6','fallback_reason':'groq_http_429'}}))
        runner.store.put('last_scan_at',0)
        runner.store.put('last_evaluation_at',0)
        assert runner.cycle()['reason']=='PAPER_OPENED'
        p=runner.ledger.positions['BTC_USDT']
        assert p['decision_ai']['model']=='claude-sonnet-4-6'
        runner.llm.last_provider='groq'
        runner.llm.last_model='a-later-model'
        assert runner.ledger.update_stop('BTC_USDT',100.5,102,102.01,p['opened_at'],'confirmed structure',time.time()-1)
        runner.ledger.mark('BTC_USDT',100.4,100.41,100.4)
        runner.status('HOLD_TEST_COMPLETE')
        fleet.last_publish['demo']=time.time()
        fleet.publish(account)
        fleet.controller.flush()
        report=fleet.control_store.get('account:demo:status')['reports'][0]
        assert report['closed_trades']==1 and report['net_pnl']>0
        texts='\n'.join(body.get('text','') for method,body in calls if method=='sendMessage')
        assert 'İşlem açıldı' in texts and 'Stop güncellendi' in texts and 'İşlem kapandı: SL' in texts
        assert 'Giriş kararını veren AI: anthropic | claude-sonnet-4-6' in texts
        assert 'Yedek geçiş nedeni: groq_http_429' in texts
        assert 'a-later-model' not in texts
        assert 'Planlanan stop kaybı: %' in texts
        assert 'Aday algoritması: trend' in texts
        assert runner.ledger.state['closed_positions'][-1]['decision_ai']['model']=='claude-sonnet-4-6'
        message('/stop')
        assert not fleet.controller.control('demo')['enabled']
        ending=runner.ledger.equity
    finally:fleet.stop()
    restarted=Fleet(cfg)
    try:
        assert restarted.runners[('demo','text')].ledger.equity==ending
        assert not restarted.controller.control('demo')['enabled']
    finally:restarted.stop()


def test_fleet_scanning_continues_while_model_is_blocked_and_pause_stops_new_scans(tmp_path,monkeypatch):
    import threading,time
    monkeypatch.setattr('trading.runtime.fleet.PositionProtection.start',lambda self:self)
    account=Account('demo',7,7,'','')
    monkeypatch.setattr('trading.runtime.fleet.load_accounts',lambda *a,**kw:[account])
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),shared_budget_db_path=str(tmp_path/'budget'),telegram_bot_token='fake')
    fleet=Fleet(cfg)
    entered=threading.Event();release=threading.Event();scanned=threading.Event()
    runner=fleet.runners[('demo','text')]
    fleet.controller.tick=lambda:None
    fleet.publish=lambda account:None
    fleet.controller.set_control('demo',enabled=True,stopped=False,capital_mode='demo',capital_amount=500,capital_chosen=True,version='text',version_chosen=True,profile_chosen=True)
    fleet.last_scan_schedule[('demo','text')]=time.time()
    def analysis():
        entered.set()
        assert release.wait(5)
    runner.cycle=analysis
    runner.scan_market=lambda:scanned.set()
    fleet.funding_pool.submit=lambda *a,**kw:None
    fleet.management_pool.submit=lambda *a,**kw:None
    try:
        fleet.tick()
        assert entered.wait(1)
        fleet.last_scan_schedule[('demo','text')]=0
        fleet.tick()
        assert scanned.wait(1)
        assert not fleet.futures['demo'].done()
        fleet.scan_futures[('demo','text')].result(timeout=1)
        scanned.clear()
        fleet.controller.set_control('demo',enabled=False,stopped=True)
        fleet.last_scan_schedule[('demo','text')]=0
        fleet.tick()
        assert not scanned.is_set()
    finally:
        release.set()
        fleet.stop()


def test_pool_mode_preserves_every_parallel_analysis_notification(tmp_path,monkeypatch):
    from trading.agents.provider_pool import ProviderPool
    account=Account('demo',7,7,'','')
    monkeypatch.setattr('trading.runtime.fleet.load_accounts',lambda *a,**kw:[account])
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),shared_budget_db_path=str(tmp_path/'budget'),
        telegram_bot_token='fake',ai_provider_pool_enabled=True,groq_api_key='private-one',groq_api_key_2='private-two')
    fleet=Fleet(cfg)
    try:
        runner=fleet.runners[('demo','text')]
        assert isinstance(runner.llm,ProviderPool)
        runner.ledger=PaperLedger(runner.store,500)
        runner.status('HOLD_EVALUATION_INTERVAL')
        traces=[dict(id=str(i),symbol=symbol,started_at=i,completed_at=i+1,execution_status='not_opened',
                     status='completed',action='hold',attempts=[]) for i,symbol in enumerate(('AAA_USDT','BBB_USDT'),1)]
        runner.store.put('analysis_history',traces)
        runner.store.put('analysis_trace',traces[-1])
        fleet.last_publish['demo']=float('inf')
        fleet.publish(account)
        fleet.publish(account)
        rows=fleet.control_store.get('account:demo:outbox')
        analyses=[r for r in rows if r['id'].startswith('analysis:')]
        assert len(analyses)==2
        assert any('AAA_USDT' in r['text'] for r in analyses)
        assert any('BBB_USDT' in r['text'] for r in analyses)
        assert 'private-one' not in str(rows) and 'private-two' not in str(rows)
        snapshot=fleet.control_store.get('account:demo:status')
        assert snapshot['parallel_candidates']==2
        assert snapshot['provider_pool']['groups']
    finally:
        fleet.stop()
