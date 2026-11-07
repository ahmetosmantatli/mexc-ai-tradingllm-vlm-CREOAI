from copy import deepcopy
from math import nan

import pytest

from trading.runtime.management import propose_management, measure_excursions

TF = 900
START = 1_700_000_000


def position(side='long'):
    return dict(side=side,entry=100.,initial_stop=98. if side=='long' else 102.,
                initial_risk_price=2.,stop=98. if side=='long' else 102.,
                opened_at=START*1000,entry_timeframe_seconds=TF)


def bars(long=True):
    # Strict ascending lows with two local dips after entry, both fully confirmed.
    lows=[100+i*.15 for i in range(24)]
    lows[7]=100.2
    lows[16]=101.2
    result=[]
    for i,low in enumerate(lows):
        op=low+.8; close=low+1.; high=low+1.2
        row=[START+i*TF,op,high,low,close,10]
        if not long:
            row=[row[0],200-op,200-low,200-high,200-close,10]
        result.append(row)
    return result


def test_long_proposal_uses_confirmed_higher_low_and_does_not_mutate():
    p=position(); before=deepcopy(p); candles=bars()
    action=propose_management(p,candles,TF,START+24*TF)
    assert action['action']=='tighten_stop'
    assert 98<action['new_stop']<candles[-1][4]
    assert action['evidence_at']==START+19*TF
    assert action['policy_version']=='paper-structure-trail-v1'
    assert p==before


def test_short_symmetry():
    action=propose_management(position('short'),bars(False),TF,START+24*TF)
    assert action['action']=='tighten_stop'
    assert bars(False)[-1][4]<action['new_stop']<102
    assert action['reason']=='confirmed_lower_high_atr_buffer'


def test_existing_tighter_stop_is_never_widened():
    p=position(); p['stop']=103
    assert propose_management(p,bars(),TF,START+24*TF)['action']=='hold'


def test_same_structure_not_repeated():
    p=position(); p['last_management_evidence_at']=START+19*TF
    assert propose_management(p,bars(),TF,START+24*TF)['reason']=='structure_already_processed'


def test_open_candle_cannot_create_evidence():
    p=position()
    candles=bars()
    # Only the first 17 bars have closed: second pivot has no right confirmation.
    action=propose_management(p,candles,TF,START+17*TF)
    assert action['action']=='hold'
    assert action['reason']=='await_post_entry_structure'


def test_advance_is_required_in_original_r_not_percent():
    candles=bars()
    # Wide original stop -> same close movement is below 1R.
    p=position(); p.update(initial_stop=90.,initial_risk_price=10.,stop=90.)
    assert propose_management(p,candles,TF,START+24*TF)['reason']=='await_confirmed_r_advance'


def test_only_post_entry_pivots_count():
    p=position(); p['opened_at']=(START+15*TF)*1000
    assert propose_management(p,bars(),TF,START+24*TF)['reason']=='await_post_entry_structure'


@pytest.mark.parametrize('change',[{'initial_stop':None},{'entry':nan},{'initial_risk_price':3.},{'entry_timeframe_seconds':3600}])
def test_invalid_or_mismatched_basis_holds(change):
    p=position(); p.update(change)
    assert propose_management(p,bars(),TF,START+24*TF)['action']=='hold'


def test_missing_or_stale_candles_hold():
    candles=bars(); del candles[3]
    assert propose_management(position(),candles,TF,START+24*TF)['action']=='hold'
    assert propose_management(position(),bars(),TF,START+30*TF)['action']=='hold'


def test_observed_excursions_retain_initial_r_after_stop_change():
    p=position(); p['stop']=100.5
    first=measure_excursions(p,104,104.1)
    assert first['current_r']==2 and first['mfe_r']==2
    p.update(first)
    second=measure_excursions(p,99,99.1)
    assert second['current_r']==-.5 and second['mfe_r']==2 and second['mae_r']==.5
    assert p['initial_stop']==98


def test_short_r_uses_executable_ask():
    result=measure_excursions(position('short'),95.9,96)
    assert result['current_r']==2
    with pytest.raises(ValueError): measure_excursions(position(),100,99)
