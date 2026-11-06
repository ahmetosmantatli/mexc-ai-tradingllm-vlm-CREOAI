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
