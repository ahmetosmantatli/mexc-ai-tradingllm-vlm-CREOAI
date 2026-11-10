"""A packet that does not fit one model must not close the other free routes."""
import json

import httpx

from trading.evaluation.context import compact_decision_packet
from .test_provider_pool import pool, response, settings


def _text(request):
    body=json.loads(request.content)
    user=body['messages'][1]['content']
    return user if isinstance(user, str) else user[0]['text']


def _hold():
    return httpx.Response(200, json=dict(choices=[dict(message=dict(content=json.dumps(dict(action='hold'))))],
                                          usage=dict(prompt_tokens=100, completion_tokens=20)))


def _packet():
    candles=[[index, 1.5, 2.5, 0.5, 1.25, 3] for index in range(12)]
    padded=[['PADDED-4H', index, 1, 2, 3, 4] for index in range(4000)]
    zone=dict(timeframe='4h', direction='bullish', low=10.5, high=12.25, open_time=100, confirmed_at=200)
    shift=dict(kind='choch', at=300, direction='bullish', level=11)
    return json.dumps(dict(
        context_policy='pa-context-v4',
        candidate=dict(symbol='PLTRSTOCK_USDT'),
        market=dict(symbol='PLTRSTOCK_USDT', timeframes=[
            dict(timeframe='4h', recent_ohlcv=padded),
            dict(timeframe='15m', closed_at=400, recent_ohlcv=candles),
        ], benchmarks={
            'BTC_USDT': {'4h': {'trend': 'bullish', 'return_6_bars': 0.02, 'relative_return_6_bars': -0.01,
                                'last_break': {'direction': 'bullish'}, 'structure_shifts': [{'kind': 'mss'}]}},
            'ETH_USDT': {'4h': {'trend': 'bearish', 'return_6_bars': -0.03, 'relative_return_6_bars': 0.04}},
        }),
        account=dict(equity=80, text_model_decision=dict(rationale='Bölge tuttu ve teyit mumu kapandı.'),
                     capital_allocation=dict(equity_usdt=80, available_usdt=42.5,
                                             open_positions=[{'symbol': 'BNB_USDT', 'side': 'long'}]),
                     setup_selection=dict(options=[dict(
                         setup_id='setup-1', state='ready', side='long', trigger_timeframe='15m',
                         context_timeframe='4h', entry_reference=11, stop_loss_price=9.5, take_profit_price=14,
                         opposing_target_levels=[
                             dict(timeframe='4h', open_time=80, field='high', price=14),
                             dict(timeframe='4h', open_time=40, field='high', price=18)],
                         evidence=dict(zone=zone, touch_at=280, shift=shift))])),
        open_positions=[{'symbol': 'BNB_USDT', 'side': 'long', 'margin': 5}],
        what_changed=dict(summary='algoritma nedeni'),
    ), separators=(',', ':'))


def test_compact_packet_keeps_the_decision_fields_and_drops_the_padded_history():
    raw=_packet()
    compact=json.loads(compact_decision_packet(raw))
    assert compact['context_policy']=='pa-context-v4-compact'
    assert compact['zone']==[dict(timeframe='4h', direction='bullish', low=10.5, high=12.25, open_time=100, confirmed_at=200)]
    assert compact['touch']==[280]
    assert compact['closed_choch_mss']==[dict(kind='choch', at=300, direction='bullish', level=11)]
    assert compact['entry']==[11]
    assert compact['stop']==[9.5]
    assert compact['opposing_target_levels']==[[
        dict(timeframe='4h', open_time=80, field='high', price=14),
        dict(timeframe='4h', open_time=40, field='high', price=18)]]
    assert compact['setups'][0]['opposing_target_levels']==compact['opposing_target_levels'][0]
    assert compact['equity']==80 and compact['free_margin']==42.5
    assert compact['why']=='Bölge tuttu ve teyit mumu kapandı.'
    assert compact['text_decision_why']==compact['why']
    assert compact['benchmarks']['BTC_USDT']['4h']['trend']=='bullish'
    assert compact['benchmarks']['BTC_USDT']['4h']['return_6_bars']==0.02
    assert compact['benchmarks']['ETH_USDT']['4h']['relative_return_6_bars']==0.04
    assert compact['equity']==80
    assert compact['free_margin']==42.5
    assert compact['open_positions'][0]['symbol']=='BNB_USDT'
    assert compact['confirming_candles'][0]['timeframe']=='15m'
    assert compact['confirming_candles'][0]['recent_ohlcv'][0]==[0, 1.5, 2.5, 0.5, 1.25, 3]
    assert len(compact['confirming_candles'][0]['recent_ohlcv'])==12
    assert 'PADDED-4H' not in compact_decision_packet(raw)
    assert len(compact_decision_packet(raw))<len(raw)


def test_known_groq_tpm_stays_8k_when_env_is_wider(tmp_path):
    cfg=settings(groq_tpm=200000)
    p,_store=pool(tmp_path, cfg, lambda request: (_ for _ in ()).throw(AssertionError('oversized request was sent')))
    assert p._limits(p._slots[0], False)==(30, 8000, 1000, 200000)
    assert p.complete_json(system='s', user='x'*38400) is None
    assert {row['error_category'] for row in p.last_trace['attempts']}=={'does-not-fit'}
    assert all(row['generation_requests']==0 for row in p.last_trace['attempts'])


def test_24k_packet_skips_8k_groq_without_zeroing_gemini(tmp_path):
    calls=[]
    def transport(request):
        calls.append(request)
        assert 'groq' not in request.url.host
        return _hold()
    cfg=settings(gemini_api_key='gemini-key', ai_primary_provider='groq')
    p,_store=pool(tmp_path, cfg, transport)
    user='x'*38400
    assert p.complete_json(system='s', user=user)=={'action': 'hold'}
    assert len(calls)==1
    assert _text(calls[0])==user
    assert p.last_provider=='gemini'
    capacity=p.analysis_capacity(prompt_bytes=len(user.encode()))
    assert capacity['free_slots']>=1
    assert any(row['provider']=='gemini' and row['available'] for row in capacity['groups'])
    groq=next(row for row in p.snapshot()['groups'] if row['provider']=='groq')
    assert groq['requests_daily_remaining']==1000
    assert groq['cooldown_seconds']==0


def test_second_groq_key_is_a_separate_pool_and_cannot_make_16k(tmp_path):
    calls=[]
    cfg=settings(groq_api_key_2='private-groq-key-2', groq_quota_group='groq-main', groq_quota_group_2='groq-main', groq_rpm=1)
    p,_store=pool(tmp_path, cfg, lambda request: calls.append(request.headers['authorization']) or response())
    assert len({p._pool_id(slot, False) for slot in p._slots})==2
    assert p.complete_json(system='s', user='u')['action']=='hold'
    assert p.fork().complete_json(system='s', user='u')['action']=='hold'
    assert calls==['Bearer private-groq-key-1', 'Bearer private-groq-key-2']
    labels={row['quota_group'] for row in p.snapshot()['groups'] if row['model']==p.settings.groq_text_model}
    assert len(labels)==2 and '33b29a46fc66' not in labels
    calls.clear()
    assert p.complete_json(system='s', user='y'*int(16000*1.6)) is None
    assert calls==[]
    assert {row['error_category'] for row in p.last_trace['attempts']}=={'does-not-fit'}
    assert all(row.get('generation_requests', 0)==0 for row in p.last_trace['attempts'])


def test_413_does_not_set_free_capacity_to_zero(tmp_path):
    calls=[]
    def transport(request):
        calls.append(request.url.host)
        if 'groq' in request.url.host:
            return httpx.Response(413, json={'error': {
                'message': 'Request too large for token budget', 'code': 'rate_limit_exceeded',
                'type': 'request_token_budget'}})
        return _hold()
    cfg=settings(gemini_api_key='gemini-key', groq_text_model='custom-free-model', groq_tpm=100000)
    p,_store=pool(tmp_path, cfg, transport)
    assert p.complete_json(system='s', user='medium packet')=={'action': 'hold'}
    assert calls==['api.groq.com', 'generativelanguage.googleapis.com']
    assert p.last_trace['attempts'][0]['error_category']=='does-not-fit'
    assert p.last_trace['attempts'][0]['http_status']==413
    capacity=p.analysis_capacity(prompt_bytes=100)
    assert capacity['free_slots']>=1
    groq=next(row for row in p.snapshot()['groups'] if row['provider']=='groq')
    assert groq['cooldown_seconds']==0
    assert groq['requests_daily_remaining']==1000
    calls.clear()
    assert p.complete_json(system='s', user='medium packet')=={'action': 'hold'}
    assert calls==['generativelanguage.googleapis.com']


def test_fitting_model_receives_the_full_packet_and_groq_receives_the_compact_one(tmp_path):
    sent=[]
    def transport(request):
        sent.append((request.url.host, _text(request)))
        if 'groq' in request.url.host:
            return httpx.Response(429, headers={'retry-after': '60'}, json={'error': {'message': 'minute'}})
        return _hold()
    cfg=settings(gemini_api_key='gemini-key')
    p,_store=pool(tmp_path, cfg, transport)
    raw=_packet()
    assert p.complete_json(system='full policy', user=raw)=={'action': 'hold'}
    groq=[text for host, text in sent if 'groq' in host]
    gemini=[text for host, text in sent if 'generativelanguage' in host]
    assert len(groq)==1 and len(gemini)==1
    compact=json.loads(groq[0])
    assert compact['context_policy']=='pa-context-v4-compact'
    assert compact['zone'][0]['low']==10.5
    assert compact['touch']==[280]
    assert compact['closed_choch_mss'][0]['kind']=='choch'
    assert compact['why']=='Bölge tuttu ve teyit mumu kapandı.'
    assert 'BTC_USDT' in compact['benchmarks'] and 'ETH_USDT' in compact['benchmarks']
    assert compact['equity']==80 and compact['free_margin']==42.5
    assert compact['open_positions'][0]['symbol']=='BNB_USDT'
    assert compact['confirming_candles'][0]['recent_ohlcv']
    assert 'PADDED-4H' not in groq[0]
    assert gemini[0]==raw
    assert 'PADDED-4H' in gemini[0]


def test_skip_trace_names_the_reason_instead_of_a_blank_token_pair(tmp_path):
    from trading.control.telegram import TelegramController
    cfg=settings()
    p,_store=pool(tmp_path, cfg, lambda request: (_ for _ in ()).throw(AssertionError('not sent')))
    assert p.complete_json(system='s', user='z'*40000) is None
    attempt=p.last_trace['attempts'][0]
    assert attempt['status']=='quota_skipped'
    assert attempt['error_category']=='does-not-fit'
    assert attempt['generation_requests']==0
    assert attempt['requests_daily_remaining']==1000
    fake=type('T', (), {})()
    fake.snapshot_provider=lambda _account: dict(analysis_trace=dict(
        id='1', symbol='BNB_USDT', strategy='trend', status='pending', attempts=p.last_trace['attempts']))
    fake.progress_text=lambda _snapshot: ''
    fake.control=lambda _account: {}
    text=TelegramController.diagnostics_text(fake, 'ahmet')
    assert 'does-not-fit' in text
    assert '? / ?' not in text
