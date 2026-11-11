"""READY→fill measurement journal: gate snapshot, decisions, fill, outcome, export."""
from trading.control.usage_report import export
from trading.runtime.ready_fill import (
    card_kind, gate_snapshot, list_events, map_ai_decision, mark_execution, mark_outcome,
    outcome_from_close, read_events, record_ready_review, realized_r_from_intent, summarize,
    summary_line,
)
from trading.runtime.store import RuntimeStore


FEE = 0.0002
SLIP = 0.0005


def _ready(setup_id='early-1', *, kind='htf_zone_internal', side='long', family='pullback'):
    return dict(
        setup_id=setup_id, state='ready', side=side, family=family,
        evidence=dict(kind=kind, stage='confirmed'),
        stop_loss_price=98.0, take_profit_price=104.0,
        opposing_target_levels=[dict(price=104.0, timeframe='1h', open_time=1, field='high')],
    )


def test_card_kind_and_gate_net_r():
    early = _ready()
    zone = _ready('zone-1', kind='htf_zone_mss')
    assert card_kind(early) == 'early_weak_swing'
    assert card_kind(zone) == 'zone'
    snap = gate_snapshot(early, entry=100.0, fee=FEE, slippage=SLIP)
    assert snap['entry'] == 100.0 and snap['stop'] == 98.0 and snap['target'] == 104.0
    assert snap['net_r_at_gate'] is not None and snap['net_r_at_gate'] > 1.0


def test_map_ai_decision_labels():
    assert map_ai_decision(dict(model_action='hold', status='completed'))[0] == 'BEKLE'
    assert map_ai_decision(dict(model_action='trade', status='completed'), selected=True)[0] == 'trade'
    decision, detail = map_ai_decision(dict(model_action='trade', status='completed'), selected=False)
    assert decision == 'trade' and detail == 'other_setup_selected'
    assert map_ai_decision(dict(status='completed'), veto_reason='cost_adjusted_r') == (
        'veto', 'cost_adjusted_r')
    assert map_ai_decision(dict(status='invalid_model_sizing', action='hold'))[0] == 'reject'
    decision, detail = map_ai_decision(
        dict(model_action='hold', action='trade', ready_setup_after_hold=True, status='completed'),
        selected=True)
    assert decision == 'BEKLE' and detail == 'ready_setup_after_hold'


def test_record_ready_review_logs_every_ready_card(tmp_path):
    store = RuntimeStore(str(tmp_path / 'runtime.sqlite3'))
    options = [
        _ready('early-1'),
        _ready('zone-1', kind='htf_zone_choch'),
        dict(setup_id='watch-1', state='watch', side='long', evidence=dict(kind='htf_zone_mss'),
             stop_loss_price=98.0, take_profit_price=104.0),
    ]
    trace = dict(id='trace-a', status='completed', action='trade', model_action='trade',
                 completed_at=100.0, setup_id='early-1')
    rows = record_ready_review(
        store, symbol='BTC_USDT', options=options, entry=100.0, fee=FEE, slippage=SLIP,
        trace=trace, selected_setup_id='early-1')
    assert len(rows) == 2
    by_id = {row['setup_id']: row for row in list_events(store)}
    assert by_id['early-1']['selected'] is True
    assert by_id['early-1']['ai_decision'] == 'trade'
    assert by_id['early-1']['card_kind'] == 'early_weak_swing'
    assert by_id['zone-1']['selected'] is False
    assert by_id['zone-1']['decision_detail'] == 'other_setup_selected'
    assert 'watch-1' not in by_id
    summary = store.get('ready_fill_summary')
    assert summary['cards'] == 2
    assert summary_line(summary).startswith('READY→fill:')


def test_veto_and_execution_and_outcome_lifecycle(tmp_path):
    store = RuntimeStore(str(tmp_path / 'runtime.sqlite3'))
    options = [_ready('early-1')]
    trace = dict(id='trace-b', status='completed', action='trade', model_action='trade',
                 completed_at=110.0, setup_id='early-1')
    record_ready_review(
        store, symbol='ETH_USDT', options=options, entry=100.0, fee=FEE, slippage=SLIP,
        trace=trace, selected_setup_id='early-1', veto_reason='cost_adjusted_r')
    row = list_events(store)[0]
    assert row['ai_decision'] == 'veto' and row['veto_reason'] == 'cost_adjusted_r'
    assert row['fill_status'] == 'none' and row['order_sent'] is False

    # A later accepted trade on a new review.
    trace2 = dict(id='trace-c', status='completed', action='trade', model_action='trade',
                  completed_at=120.0, setup_id='early-1')
    record_ready_review(
        store, symbol='ETH_USDT', options=options, entry=100.0, fee=FEE, slippage=SLIP,
        trace=trace2, selected_setup_id='early-1')
    marked = mark_execution(
        store, trace_id='trace-c', fill_status='filled', execution_reason='LIVE_PROTECTED',
        intent_id='intent-1', order_sent=True, setup_id='early-1')
    assert marked['order_sent'] is True and marked['fill_status'] == 'filled'
    assert marked['outcome'] == 'open'

    intent = dict(
        intent_id='intent-1', status='closed', close_reason='TP', realized_complete=True,
        net_pnl=2.5, entry_metrics=dict(initial_stop_risk_usdt=1.25),
        proposal=dict(metadata=dict(analysis_trace_id='trace-c', setup_id='early-1')))
    assert outcome_from_close('TP') == 'target_hit'
    assert realized_r_from_intent(intent) == 2.0
    closed = mark_outcome(store, intent)
    assert closed['outcome'] == 'target_hit' and closed['realized_r'] == 2.0
    assert closed['close_reason'] == 'TP'


def test_reject_fill_status(tmp_path):
    store = RuntimeStore(str(tmp_path / 'runtime.sqlite3'))
    record_ready_review(
        store, symbol='SOL_USDT', options=[_ready('z')], entry=100.0, fee=FEE, slippage=SLIP,
        trace=dict(id='t', status='completed', action='trade', model_action='trade',
                   completed_at=1.0, setup_id='z'),
        selected_setup_id='z')
    marked = mark_execution(
        store, trace_id='t', fill_status='rejected', execution_reason='HOLD_LIVE_NO_FUNDS',
        intent_id='i2', setup_id='z')
    assert marked['fill_status'] == 'rejected' and marked['order_sent'] is True


def test_usage_report_includes_ready_fill_files(tmp_path):
    root = tmp_path / 'data'
    store = RuntimeStore(str(root / 'friend' / 'live' / 'runtime.sqlite3'))
    record_ready_review(
        store, symbol='BTC_USDT', options=[_ready('early-1'), _ready('zone-1', kind='htf_zone_mss')],
        entry=100.0, fee=FEE, slippage=SLIP,
        trace=dict(id='u1', status='completed', action='hold', model_action='hold',
                   completed_at=50.0),
        selected_setup_id=None)
    out = tmp_path / 'reports'
    report = export(root, out)
    assert report['ready_fill_summary']['cards'] == 2
    assert report['ready_fill_summary']['by_ai_decision']['BEKLE'] == 2
    assert (out / 'ready-fill.json').exists()
    text = (out / 'ready-fill-summary.txt').read_text()
    assert 'READY→fill:' in text
    assert 'early-1' in text
    assert 'SECRET' not in text
    assert (out / 'ready-fill-summary.txt').stat().st_mode & 0o777 == 0o600


def test_read_events_skips_non_runtime_layout(tmp_path):
    root = tmp_path / 'data'
    good = RuntimeStore(str(root / 'ahmet' / 'live' / 'runtime.sqlite3'))
    record_ready_review(
        good, symbol='BTC_USDT', options=[_ready()], entry=100.0, fee=FEE, slippage=SLIP,
        trace=dict(id='g', status='completed', action='hold', model_action='hold', completed_at=1.0))
    stray = RuntimeStore(str(root / 'misc' / 'runtime.sqlite3'))
    record_ready_review(
        stray, symbol='BTC_USDT', options=[_ready('x')], entry=100.0, fee=FEE, slippage=SLIP,
        trace=dict(id='s', status='completed', action='hold', model_action='hold', completed_at=1.0))
    rows = read_events(root)
    assert len(rows) == 1 and rows[0]['account'] == 'ahmet'


def test_summarize_mean_realized_r():
    rows = [
        dict(ai_decision='trade', fill_status='filled', outcome='target_hit', order_sent=True,
             card_kind='early_weak_swing', realized_r=1.5),
        dict(ai_decision='BEKLE', fill_status='none', outcome='open', order_sent=False,
             card_kind='zone', realized_r=None),
        dict(ai_decision='trade', fill_status='filled', outcome='stop_hit', order_sent=True,
             card_kind='zone', realized_r=-1.0),
    ]
    summary = summarize(rows)
    assert summary['cards'] == 3 and summary['filled'] == 2
    assert summary['realized_r_samples'] == 2
    assert abs(summary['mean_realized_r'] - 0.25) < 1e-9
