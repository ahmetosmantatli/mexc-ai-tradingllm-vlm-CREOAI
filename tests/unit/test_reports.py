from copy import deepcopy
from datetime import datetime, timezone
import pytest
from trading.runtime.reports import daily_trade_report, format_daily_report


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


def state(rows, positions=None):
    return dict(currency='USDT',cash=500,realized=12,closed_positions=rows,positions=positions or {})


def trade(**updates):
    row=dict(symbol='BTC_USDT',side='long',strategy='sweep_reclaim',leverage=7,
             opened_at=stamp('2026-11-06T18:00:00+00:00'),closed_at=stamp('2026-11-06T22:30:00+00:00'),
             entry=100,exit_price=110,gross_pnl=15,entry_fee=.5,exit_fee=.6,funding=.9,
             close_reason='TP',mfe_r=3,mae_r=.5)
    row.update(updates)
    return row


def test_timezone_uses_closure_day_not_utc_or_open_day():
    data=state([trade()])
    assert daily_trade_report(data,day='2026-11-06')['closed_trades']==0
    report=daily_trade_report(data,day='2026-11-07')
    assert report['closed_trades']==1 and report['wins']==1
    assert report['net_pnl']==pytest.approx(13)
    assert report['trades'][0]['closed_at'].startswith('2026-11-07T01:30')
    assert report['pnl_complete']


def test_net_recomputed_after_funding_correction_not_double_subtracted():
    row=trade(close_net=13, funding=1.9)
    report=daily_trade_report(state([row]),day='2026-11-07')
    assert report['net_pnl']==pytest.approx(12)
    assert report['closed_trade_funding']==pytest.approx(1.9)
    assert report['closed_trade_entry_fees']==.5
    assert report['closed_trade_exit_fees']==.6


def test_explicit_original_close_net_basis_adjusts_later_funding():
    row=trade()
    del row['gross_pnl']
    row.update(close_net=13,close_net_basis='at_close',funding_at_close=.9,funding=1.9)
    report=daily_trade_report(state([row]),day='2026-11-07')
    assert report['net_pnl']==pytest.approx(12)
    assert report['pnl_complete']


def test_legacy_missing_fields_are_unknown_and_disclosed():
    row=dict(symbol='ETH_USDT',closed_at=stamp('2026-11-07T10:00:00+00:00'),close_net=2)
    report=daily_trade_report(state([row,{'symbol':'unknown'}]),day='2026-11-07')
    assert report['net_pnl']==2
    assert not report['pnl_complete']
    assert report['trades'][0]['strategy']=='unknown'
    assert report['trades'][0]['leverage'] is None
    assert report['unassigned_legacy_records']==1
    assert 'kısmi' in format_daily_report(report)
    assert report['warnings']


def test_all_totals_survive_bounded_detail_and_snapshot_is_separate():
    rows=[trade(symbol=f'COIN{i}_USDT',opened_at=stamp('2026-11-06T18:00:00+00:00')+i) for i in range(5)]
    rows.append(trade(symbol='LOSER_USDT',gross_pnl=-4))
    data=state(rows,{'OPEN_USDT':dict(unrealized=7)})
    original=deepcopy(data)
    report=daily_trade_report(data,day='2026-11-07',max_rows=2)
    assert report['closed_trades']==6 and report['wins']==5 and report['losses']==1
    assert len(report['trades'])==2 and report['omitted_detail_rows']==4
    assert report['net_pnl']==pytest.approx(59)
    assert report['current_equity']==507 and report['current_unrealized']==7
    assert report['all_time_realized']==12
    assert data==original


def test_malformed_and_nonfinite_records_never_pollute_pnl():
    rows=[trade(gross_pnl=float('nan'),close_net=float('inf')),trade(symbol='SECOND',closed_at=None),None]
    report=daily_trade_report(state(rows),day='2026-11-07')
    assert report['known_pnl_trades']==0 and report['net_pnl']==0
    assert not report['pnl_complete'] and report['unassigned_legacy_records']==2


def test_duplicate_close_ignored_and_mfe_mae_rendered():
    row=trade()
    report=daily_trade_report(state([row,deepcopy(row)]),day='2026-11-07')
    assert report['closed_trades']==1
    text=format_daily_report(report)
    assert '7x' in text and 'sweep_reclaim' in text and 'MFE' in text and 'MAE' in text
    assert any('yinelenen' in warning for warning in report['warnings'])


def test_invalid_selection_raises_and_empty_day_not_fabricated():
    with pytest.raises(ValueError):daily_trade_report(state([]),day='wrong')
    with pytest.raises(ValueError):daily_trade_report(state([]),max_rows=0)
    report=daily_trade_report(state([]),day='2026-11-07')
    assert report['closed_trades']==0 and report['net_pnl']==0 and report['trades']==[]
