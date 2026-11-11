"""Compact prompts keep current numerical evidence and explicit setup memory."""
import copy
import json
from types import SimpleNamespace

from trading.agents.model_limits import estimate_input_tokens
from trading.evaluation.context import (
    MANAGEMENT_OHLCV_BARS, MANAGEMENT_STRUCTURE_ROWS, build_prompt, change_summary,
    compact_market, management_market)
from trading.evaluation.service import ContextEvaluator, ModelDecision
from trading.quant.signals import CandidateSignal, StrategyName
from trading.models.domain import Side
from trading.config import Settings
from trading.runtime.live_management import POLICY
from trading.runtime.store import RuntimeStore


def candidate(strategy=StrategyName.TREND, side=Side.LONG):
    return CandidateSignal(symbol='BTC_USDT',side=side,strategy=strategy,entry_price=100,
        stop_loss_price=98 if side==Side.LONG else 102,take_profit_price=104 if side==Side.LONG else 96,confidence=.8)


def market(stamp=100):
    views=[]
    for tf in ('4h','15m','5m'):
        records=[dict(direction='bullish',at=i,level=100.123456789+i,swing_at=i-2) for i in range(12)]
        views.append(dict(timeframe=tf,closed_at=stamp,history_bars=120,fingerprint=tf+str(stamp),
            features=dict(trend='bullish',last_swing_high={'price':123.4567890123,'confirmed_at':stamp-1},
                last_swing_low={'price':97.0123456789,'confirmed_at':stamp-2},
                swings=copy.deepcopy(records),breaks=copy.deepcopy(records),sweeps=copy.deepcopy(records),fvgs=copy.deepcopy(records),
                caveat='long repeated observation caveat',definition_version='pa-closed-v1'),
            recent_ohlcv=[[i,100.123456789,102.123456789,98.123456789,101.123456789,123456.123456789] for i in range(12)]))
    return dict(exchange='MEXC',symbol='BTC_USDT',instrument='USDT perpetual',feature_version='pa-closed-v1',
        observed_at=stamp+1,fingerprint='market-hash',timeframes=views)


def test_compact_tables_preserve_all_current_values_and_no_rounding():
    original=market()
    compact=compact_market(original)
    assert compact['observed_at']==101
    for before,after in zip(original['timeframes'],compact['timeframes']):
        assert before['recent_ohlcv']==after['recent_ohlcv']
        assert before['features']['last_swing_high']==after['features']['last_swing_high']
        for kind in ('swings','breaks','sweeps','fvgs'):
            tab=after['features'][kind]
            decoded=[dict(zip(tab['columns'],row)) for row in tab['rows']]
            assert decoded==before['features'][kind]


def test_management_market_keeps_a_short_wick_window_and_drops_padded_history():
    original=market()
    for view in original['timeframes']:
        view['recent_ohlcv']=[[i,100+i,102,98,101,10] for i in range(40)]
        view['features']['swings']=[dict(direction='bullish',at=i,level=100+i) for i in range(20)]
        view['features']['breaks']=[dict(direction='bullish',at=i,level=100+i) for i in range(20)]
    padded=copy.deepcopy(original)
    # Old padded history precedes the live wick window; only the tail is kept.
    padded['timeframes'][0]['recent_ohlcv']=([['PADDED',i,1,2,3,4] for i in range(4000)]
        +[[1_700_000_000+i,100.,101.,99.,100.5,1.] for i in range(MANAGEMENT_OHLCV_BARS)])
    before=json.dumps(dict(task='manage_existing_position',market=compact_market(padded)),
                      separators=(',',':'))
    after_market=management_market(padded)
    after=json.dumps(dict(task='manage_existing_position',market=after_market,
                          position=dict(symbol='BTC_USDT',side='long'),account=dict(equity=80)),
                     separators=(',',':'))
    assert 'PADDED' not in after
    for view in after_market['timeframes']:
        assert len(view['recent_ohlcv'])==MANAGEMENT_OHLCV_BARS
        assert view['history_bars']==MANAGEMENT_OHLCV_BARS
        assert len(view['features']['swings']['rows'])==MANAGEMENT_STRUCTURE_ROWS
        assert len(view['features']['breaks']['rows'])==MANAGEMENT_STRUCTURE_ROWS
        assert view['features']['last_swing_high']['price']==123.4567890123
        assert view['features']['trend']=='bullish'
    assert len(after.encode())<len(before.encode())*.05
    # Management system + compact market stays well under Groq's 8k class with completion reserve.
    assert estimate_input_tokens(POLICY, after)+2048<=8000
    assert estimate_input_tokens(POLICY, after)<4500


def test_prompt_uses_setup_card_not_full_audit_and_is_materially_smaller():
    history=[dict(at=i,action='watch',rationale='Eski dip geri alınmalı.'*20,
        invalidation='Son dip kırılırsa iptal.',charts=['/private/account/secret-path/chart.png']*3,
        fingerprint='longauditfingerprint'*4) for i in range(6)]
    memory=dict(history=history,watch_note='15dk kapanışını bekle.',last_outcome={'reason':'TP','net_pnl':5})
    payload=market()
    c=candidate()
    prompt=build_prompt(c,payload,memory,{},dict(equity=500,risk_per_trade_pct=.03),'text',{'summary':'İlk inceleme'})
    old=json.dumps(dict(candidate=c.model_dump(mode='json'),market=payload,memory=memory),separators=(',',':'))
    decoded=json.loads(prompt)
    assert len(prompt.encode())<len(old.encode())*.75
    assert len(prompt.encode())<18000 # size budget, not an exact tokenizer claim
    assert '/private/' not in prompt and 'longauditfingerprint' not in prompt
    assert decoded['setup_card']['first_reason']==history[0]['rationale']
    assert decoded['setup_card']['watch_note']==memory['watch_note']
    assert decoded['setup_card']['last_outcome']['reason']=='TP'
    assert len(decoded['market']['timeframes'])==3
    assert decoded['policy']['self_contained'] is True


def test_changes_explain_new_timeframe_and_keep_current_baseline():
    before=market()
    after=copy.deepcopy(before)
    after['timeframes'][2]['closed_at']=400
    after['timeframes'][2]['fingerprint']='new5m'
    after['timeframes'][2]['features']['sweeps'].append({'at':399,'direction':'low_sweep','level':98})
    changes=change_summary(before,after,'old','new')
    assert [row['timeframe'] for row in changes['timeframes']]==['5m']
    assert changes['timeframes'][0]['changed_structure']==['sweeps']
    assert '5m' in changes['summary'] and 'korunuyor' in changes['summary']


def test_context_restarts_retains_card_dedup_and_distinguishes_side(tmp_path,monkeypatch):
    active=[market()]
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint=str(active[0]['timeframes'][2]['closed_at']),to_context=lambda:active[0]))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    calls=[]
    llm=SimpleNamespace(budget=None,response_validator=None,decision_schema=None)
    def complete(**kw):
        calls.append(json.loads(kw['user']))
        return dict(action='watch',signal_id=None,rationale='15dk teyidi yok.',invalidation='Dip kırılırsa iptal.',watch_note='Dip üstünde 15dk kapanış bekle.')
    llm.complete_json=complete
    store=RuntimeStore(str(tmp_path/'s'))
    cfg=Settings(_env_file=None)
    kwargs=dict(equity=500,risk_per_trade_pct=.03,target_leverage=10)
    c=candidate()
    first=ContextEvaluator(llm,store,None,cfg).evaluate_candidates([c],**kwargs)
    assert first.metadata['provider_called'] is True
    assert store.get('analysis_trace')['rationale']=='15dk teyidi yok.'
    assert llm.decision_schema['properties']['action']
    assert ContextEvaluator(llm,store,None,cfg).evaluate_candidates([candidate()],**kwargs).metadata['provider_called'] is False
    active[0]=market(400)
    ContextEvaluator(llm,store,None,cfg).evaluate_candidates([candidate()],**kwargs)
    assert calls[-1]['setup_card']['first_reason']=='15dk teyidi yok.'
    assert calls[-1]['setup_card']['watch_note']=='Dip üstünde 15dk kapanış bekle.'
    assert 'Kapanmış mum verisi değişti' in calls[-1]['what_changed']['summary']
    # A separate setup direction is not falsely deduplicated against the long.
    ContextEvaluator(llm,store,None,cfg).evaluate_candidates([candidate(side=Side.SHORT)],**kwargs)
    assert len(calls)==3
    bad=dict(action='trade',signal_id='wrong',rationale='x',invalidation='x',watch_note='x')
    import pytest
    with pytest.raises(ValueError,match='unknown signal'):
        llm.response_validator(bad)
