from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import copy
import json

from trading.config import Settings
from trading.evaluation.context import account_research_context
from trading.evaluation.service import ContextEvaluator
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal,StrategyName
from trading.runtime.store import RuntimeStore

NOW=1794052800 # stable UTC epoch; report day is derived explicitly below.


def ledger():
    return dict(currency='USDT',initial=500.,cash=520.,realized=22.,fees=.7,funding=.1,
        peak=550.,day=datetime.fromtimestamp(NOW,ZoneInfo('Europe/Istanbul')).date().isoformat(),day_start=530.,
        positions={'BTC_USDT':dict(symbol='BTC_USDT',side='long',entry=100.,stop=98.,target=104.,
            margin=50.,notional=500.,unrealized=5.,opened_at=(NOW-60)*1000)},
        closed_positions=[dict(symbol='IDOL_USDT',side='long',strategy='trend',leverage=10,close_reason='TP',
            opened_at=(NOW-7200)*1000,closed_at=(NOW-3600)*1000,gross_pnl=23.,entry_fee=.3,exit_fee=.4,funding=.1)],
        events=[dict(kind='OPEN',event_at=(NOW-7200)*1000)])


def goal(**overrides):
    values=dict(research_goal_enabled=True,research_goal_multiple=14,research_goal_days=7,research_goal_stretch_multiple=200)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_operating_context_reports_balance_and_costs_without_try_conversion(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'));store.put('paper',ledger())
    context=account_research_context(store,525.,now=NOW)
    assert context['initial_usdt']==500 and context['equity_snapshot_usdt']==525
    assert context['open_margin_usdt']==50 and context['available_usdt']==475
    assert context['open_unrealized_usdt']==5 and context['realized_net_total_usdt']==22
    assert context['ledger_equity_usdt']==525 and context['daily_equity_change_usdt']==-5
    assert context['daily_closed_realized_net_usdt']==22.2
    assert context['daily_closed_trade_fees_usdt']==.7
    assert context['close_reason_counts']==dict(TP=1,SL=0,MANUAL=0,LIQ=0,UNKNOWN=0)
    assert context['open_position_count']==1 and context['closed_position_count']==1
    assert context['daily_loss_pct']==max(0,1-525/530)
    assert context['drawdown_pct']==max(0,1-525/550)
    assert context['objective']==dict(enabled=False,status='not_configured')
    assert store.get('research_objective_start') is None
    assert 'TRY conversion' in context['balance_source']


def test_goal_anchor_survives_restart_and_current_balance_changes(tmp_path):
    path=str(tmp_path/'state');store=RuntimeStore(path);store.put('paper',ledger())
    first=account_research_context(store,525.,goal(),now=NOW)
    objective=first['objective']
    assert objective['reference_initial_usdt']==500
    assert objective['target_usdt']==7000 and objective['stretch_target_usdt']==100000
    assert objective['remaining_usdt']==6475 and objective['remaining_multiple']==7000/525
    assert objective['started_at']==NOW-7200
    assert objective['deadline_at']==NOW-7200+7*86400
    assert 'Reporting objective only' in objective['policy']
    restarted=RuntimeStore(path)
    later=account_research_context(restarted,600.,goal(),now=NOW+300)['objective']
    assert later['started_at']==objective['started_at'] and later['target_usdt']==7000
    assert later['remaining_usdt']==6400 and later['remaining_seconds']==objective['remaining_seconds']-300
    assert later['progress_fraction']>objective['progress_fraction']


def test_goal_missing_session_start_is_labeled_not_restarted(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'))
    state=ledger();state.update(events=[],positions={},closed_positions=[]);store.put('paper',state)
    first=account_research_context(store,500.,goal(),now=NOW)['objective']
    assert first['started_at']==NOW and 'original_session_start_unknown' in first['start_source']
    again=account_research_context(store,500.,goal(),now=NOW+60)['objective']
    assert again['started_at']==NOW
    assert account_research_context(store,500.,goal(research_goal_enabled=False),now=NOW+90)['objective']['enabled'] is False
    assert account_research_context(store,500.,goal(),now=NOW+120)['objective']['started_at']==NOW


def test_last_closed_details_bounded_counts_include_all_and_legacy_costs_unknown(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'));state=ledger()
    state['positions']['BTC_USDT'].pop('margin')
    records=[]
    for index in range(12):
        records.append(dict(symbol='TEST_USDT',opened_at=(NOW-1000-index*10)*1000,closed_at=(NOW-index*10)*1000,
            close_reason=('SL','TP','MANUAL','LIQ')[index%4],close_net=index))
    state['closed_positions']=records
    store.put('paper',state)
    context=account_research_context(store,525.,now=NOW)
    assert len(context['recent_closed'])==5 and context['closed_position_count']==12
    assert all(context['close_reason_counts'][reason]==3 for reason in ('SL','TP','MANUAL','LIQ'))
    assert context['available_usdt'] is None and context['open_margin_usdt'] is None
    assert context['daily_pnl_complete'] is False
    assert all(row['net_complete'] is False for row in context['recent_closed'])
    assert 'events' not in context and 'closed_positions' not in context


def test_unknown_currency_and_uninitialized_ledger_do_not_invent_seed_or_fx(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'))
    missing=account_research_context(store,500.,goal(),now=NOW)
    assert missing['ledger_available'] is False and missing['objective']['enabled'] is False
    store.put('paper',dict(currency='TRY',initial=500,positions={}))
    context=account_research_context(store,500.,goal(),now=NOW)
    assert context['currency']=='TRY'
    assert context['objective']['status']=='unsupported_or_unknown_ledger_currency'
    assert store.get('research_objective_start') is None


def test_evaluator_passes_measured_operating_context_and_clock_does_not_defeat_dedup(tmp_path,monkeypatch):
    store=RuntimeStore(str(tmp_path/'state'));store.put('paper',ledger())
    candidate=CandidateSignal(symbol='ETH_USDT',side=Side.LONG,strategy=StrategyName.TREND,
        entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='same',to_context=lambda:{}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    from trading.evaluation.context import account_research_context as real
    clock=[NOW]
    monkeypatch.setattr('trading.evaluation.service.account_research_context',lambda store,equity,settings:real(store,equity,goal(),now=clock[0]))
    calls=[]
    def complete(**kwargs):
        calls.append(json.loads(kwargs['user']))
        return dict(action='hold',signal_id=None,rationale='Teyit yok.',invalidation='Dip',watch_note='Kapanış bekle.')
    llm=SimpleNamespace(budget=None,complete_json=complete)
    evaluator=ContextEvaluator(llm,store,None,Settings(_env_file=None))
    parameters=dict(equity=525.,risk_per_trade_pct=.03,target_leverage=10)
    evaluator.evaluate_candidates([candidate],**parameters)
    assert calls[0]['account']['operating_context']['close_reason_counts']['TP']==1
    assert calls[0]['account']['operating_context']['objective']['target_usdt']==7000
    clock[0]+=30
    assert evaluator.evaluate_candidates([candidate],**parameters).metadata['provider_called'] is False
    assert len(calls)==1
