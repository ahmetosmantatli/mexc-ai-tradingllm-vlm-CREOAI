"""A packet that does not fit one model must not close the other free routes."""
import json
import time

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


def _wave(count=24):
    rows=[]
    for index in range(count):
        phase=index % 6
        high=10.+phase
        low=high-1.
        close=(high+low)/2.
        rows.append([1_700_000_000+index*900, close, high, low, close, 1.])
    return rows


def _packet():
    candles=_wave()
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
        account=dict(equity=80, leverage=5, text_model_decision=dict(rationale='Bölge tuttu ve teyit mumu kapandı.'),
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
    setup=compact['setups'][0]
    assert compact['context_policy']=='pa-context-v4-compact'
    assert setup['lane']=='4h/15m' and setup['direction']=='long'
    assert setup['closed_choch_price']==11
    assert setup['stop']==9.5
    assert setup['block_or_flow']['price']==12.25
    assert setup['higher_timeframe_bias']=='bullish'
    assert setup['next_target']==14
    assert setup['farther_with_trend_targets']==[18]
    assert compact['gates']['cost_adjusted_r']==1.25
    assert compact['gates']['equity_stop']=='15%'
    assert compact['gates']['free_margin']==42.5
    assert compact['swings'] and all(point['kind'] in ('high', 'low') for point in compact['swings'])
    assert compact['atr']>0
    assert 'recent_ohlcv' not in compact_decision_packet(raw)
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
    p,store=pool(tmp_path, cfg, transport)
    # Gemini is first whenever it can answer. Hold it only so the wide Groq model can observe a 413.
    gemini=next(slot for slot in p._slots if slot.provider=='gemini')
    store.put(p._quota_key(gemini, None), dict(rows=[], lease=dict(id='busy', until=time.time()+60)))
    assert p.complete_json(system='s', user='medium packet') is None
    assert calls==['api.groq.com']
    groq_attempt=next(row for row in p.last_trace['attempts'] if row.get('http_status')==413)
    assert groq_attempt['provider']=='groq' and groq_attempt['error_category']=='does-not-fit'
    store.put(p._quota_key(gemini, None), dict(rows=[]))
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
        if 'generativelanguage' in request.url.host:
            return httpx.Response(429, headers={'retry-after': '60'}, json={'error': {'message': 'minute'}})
        return _hold()
    cfg=settings(gemini_api_key='gemini-key')
    p,_store=pool(tmp_path, cfg, transport)
    raw=_packet()
    assert p.complete_json(system='full policy', user=raw)=={'action': 'hold'}
    groq=[text for host, text in sent if 'groq' in host]
    gemini=[text for host, text in sent if 'generativelanguage' in host]
    assert [host for host, _body in sent]==['generativelanguage.googleapis.com', 'api.groq.com']
    assert len(groq)==1 and len(gemini)==1
    compact=json.loads(groq[0])
    assert compact['context_policy']=='pa-context-v4-compact'
    assert compact['setups'][0]['closed_choch_price']==11
    assert compact['setups'][0]['stop']==9.5
    assert compact['setups'][0]['block_or_flow']['price']==12.25
    assert compact['setups'][0]['next_target']==14
    assert compact['swings'] and compact['atr']>0
    assert 'recent_ohlcv' not in groq[0]
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


def test_compact_review_stays_under_8k_and_keeps_swings_not_the_prior_paragraph(tmp_path):
    from trading.agents.model_limits import estimate_input_tokens
    from trading.evaluation.service import ModelDecision
    from trading.runtime.runner3_policy import entry_gate_instruction

    paragraph='Fiyat blokta durdu. '+('Eski sonuç işlemi reddeder ve gerekçeyi uzatır. '*40)
    assert len(paragraph)>400
    zone=dict(timeframe='4h', kind='block', direction='bullish', low=10.5, high=12.25, open_time=100, confirmed_at=200)
    shift=dict(kind='choch', at=300, direction='bullish', level=11.11)
    padded=[[1_600_000_000+index*14400, 1, 424242.5, 0.5, 1, 9] for index in range(500)]
    option=dict(setup_id='setup-1', state='ready', side='long', horizon='swing', macro_trend='bullish',
                context_timeframe='4h', trigger_timeframe='15m', entry_reference=11.11, stop_loss_price=9.5,
                take_profit_price=14, opposing_target_levels=[
                    dict(timeframe='4h', open_time=80, field='high', price=14),
                    dict(timeframe='4h', open_time=40, field='high', price=18)],
                evidence=dict(zone=zone, touch_at=280, shift=shift, kind='htf_zone_choch',
                              alignment='with_higher_timeframe'))
    raw=json.dumps(dict(
        context_policy='pa-context-v4',
        candidate=dict(symbol='LAB_USDT', side='long', entry_price=11.5),
        market=dict(symbol='LAB_USDT', funding_rate=0.0001, timeframes=[
            dict(timeframe='4h', recent_ohlcv=padded),
            dict(timeframe='15m', recent_ohlcv=_wave(30)),
        ]),
        account=dict(capital_allocation=dict(available_usdt=42.5), setup_selection=dict(options=[option]),
                     prior_hold=dict(rationale=paragraph, printed_trigger=11.11,
                                     old_market=dict(note='REPEATED-HISTORY'*80, timeframes=[dict(recent_ohlcv=padded)]))),
    ), separators=(',', ':'))
    assert len(raw.encode())>=35000
    compact_text=compact_decision_packet(raw)
    assert estimate_input_tokens('', compact_text)<8000
    compact=json.loads(compact_text)
    assert compact['swings'] and compact['atr']>0
    assert all(set(point)=={'timeframe', 'kind', 'price'} for point in compact['swings'])
    assert '424242.5' not in compact_text and 'REPEATED-HISTORY' not in compact_text
    assert 'recent_ohlcv' not in compact_text
    setup=compact['setups'][0]
    assert setup['closed_choch_price']==11.11 and setup['stop']==9.5
    assert setup['block_or_flow']['kind']=='block' and setup['next_target']==14
    assert setup['farther_with_trend_targets']==[18]
    assert setup['distance_to_block']==abs(11.5-12.25)
    assert setup['higher_timeframe_bias']=='bullish'
    assert compact['funding']==0.0001
    assert compact['prior_clause']=='Fiyat blokta durdu'
    assert len(compact['prior_clause'])<=80
    assert 'Eski sonuç' not in compact_text
    assert compact['trigger']==11.11
    assert compact['gates']==dict(cost_adjusted_r=1.25, equity_stop='15%', free_margin=42.5)
    bare=json.loads(raw)
    bare['market'].pop('funding_rate')
    assert 'funding' not in json.loads(compact_decision_packet(json.dumps(bare)))
    against=json.loads(raw)
    against['account']['setup_selection']['options'][0]['evidence']['alignment']='against_higher_timeframe'
    assert 'farther_with_trend_targets' not in json.loads(compact_decision_packet(json.dumps(against)))['setups'][0]

    gate=entry_gate_instruction(fee=0.0006, slippage=0.0005)
    system='Runner 3 review. '+gate+(' Use the supplied evidence only.'*300)
    calls=[]

    def transport(request):
        calls.append(request)
        body=dict(action='hold', signal_id=None, rationale='Geçerli bekleme kararı.',
                  invalidation='Kurulum bozulursa bekleme biter.', watch_note='')
        return httpx.Response(200, json=dict(choices=[dict(message=dict(content=json.dumps(body)))],
                                              usage=dict(prompt_tokens=100, completion_tokens=20)))

    cfg=settings(gemini_api_key='gemini-key', anthropic_api_key='paid', ai_claude_fallback_enabled=True)
    provider,_store=pool(tmp_path, cfg, transport)
    provider.response_validator=ModelDecision.model_validate
    provider.claude.complete_json=lambda **kwargs: (_ for _ in ()).throw(AssertionError('valid BEKLE must not start fallback'))
    assert provider.complete_json(system=system, user=raw)['action']=='hold'
    assert len(calls)==1 and 'groq' in calls[0].url.host
    sent_system=json.loads(calls[0].content)['messages'][0]['content']
    sent_user=_text(calls[0])
    assert estimate_input_tokens(sent_system, sent_user)<8000
    assert estimate_input_tokens(sent_system, sent_user)+cfg.groq_max_completion_tokens<=8000
    sent=json.loads(sent_user)
    assert sent['swings'] and sent['atr'] and sent['prior_clause']=='Fiyat blokta durdu'
    assert provider.last_trace['attempts'][0].get('status')=='success'
