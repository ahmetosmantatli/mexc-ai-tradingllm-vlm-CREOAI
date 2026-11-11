"""1h weak swing, 5m trigger. The model decides the first look."""
from copy import deepcopy
import json
import logging
import time
from types import SimpleNamespace

import pytest

from trading.evaluation.service import ContextEvaluator
from trading.runtime.runner3_policy import choose_zone_target
from .test_runner3_llm_vlm_limits import DecisionModel, evaluator, rebase_market
from .test_structure_setups import NOW, htf_zone_context, view


def _rebase(market):
    offset = int(time.time()) - NOW - 1

    def walk(value):
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1000000:
            return value + offset
        return value

    return walk(market)


def _bearish_block(view_row, *, low, high):
    bar = view_row['recent_ohlcv'][1]
    bar[2] = high
    bar[3] = min(bar[3], low)
    view_row['features'].setdefault('order_block_candidates', [])
    view_row['features']['order_block_candidates'].append(dict(
        direction='bearish', open_time=bar[0], low=low, high=high, status='untested'))


def _bullish_block(view_row, *, low, high):
    bar = view_row['recent_ohlcv'][2]
    bar[3] = low
    bar[2] = max(bar[2], high)
    view_row['features'].setdefault('order_block_candidates', [])
    view_row['features']['order_block_candidates'].append(dict(
        direction='bullish', open_time=bar[0], low=low, high=high, status='untested'))


def weak_long_market(*, target_high=120., target_low=112., farther=None):
    """1h weak low, bullish candle there, 5m CHoCH. No sweep and no 15m CHoCH."""
    ctx = htf_zone_context('1h', '5m')
    higher = view(ctx, '1h')
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'low')
    pivot = next(row for row in higher['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[1] = swing['price'] + 1
    pivot[4] = swing['price'] + 4
    pivot[2] = max(pivot[2], pivot[4])
    higher['features']['sweeps'] = []
    higher['features']['structure_shifts'] = []
    higher['features']['breaks'] = []
    _bearish_block(higher, low=target_low, high=target_high)
    if farther is not None:
        book = view(ctx, '4h')
        _bearish_block(book, low=farther - 8, high=farther)
    for name in ('15m', '4h'):
        frame = view(ctx, name)
        frame['features']['structure_shifts'] = []
        frame['features']['sweeps'] = []
    lower = view(ctx, '5m')
    low_swing = lower['features']['last_swing_low']
    origin = next(row for row in lower['recent_ohlcv'] if row[0] == low_swing['pivot_at'])
    origin[3] = 94.
    low_swing['price'] = 94.
    for row in lower['features']['swings']:
        if row.get('kind') == 'low':
            row['price'] = 94.
    return ctx


def _internal(market, price=100.):
    from trading.quant.structure_setups import build_htf_zone_setups

    return [row for row in build_htf_zone_setups(market, current_price=price, price_step=.01)
            if row['evidence'].get('kind') == 'htf_zone_internal']


def test_weak_low_uses_the_5m_trigger_and_does_not_wait_for_a_sweep_or_15m():
    market = weak_long_market()
    card, = _internal(market)
    assert card['context_timeframe'] == '1h'
    assert card['trigger_timeframe'] == '5m'
    assert card['state'] == 'ready'
    assert card['side'] == 'long'
    assert card['evidence']['weak_field'] == 'low'
    assert card['evidence']['shift']['kind'] in ('choch', 'mss')
    assert view(market, '15m')['features']['structure_shifts'] == []
    assert view(market, '4h')['features']['structure_shifts'] == []
    assert view(market, '1h')['features']['sweeps'] == []
    swing = next(row for row in card['swing_lows'] if row['timeframe'] == '5m')
    assert card['stop_reference']['timeframe'] == '5m'
    assert card['stop_reference']['field'] == 'low'
    assert card['stop_loss_price'] < swing['price']
    assert card['target_reference']['timeframe'] == '1h'
    assert card['take_profit_price'] == pytest.approx(120.)
    assert card['evidence']['candidate_block']['timeframe'] == '1h'
    assert card['evidence']['trigger_candle']['timeframe'] == '5m'
    assert card['swing_highs'] and card['swing_lows']
    read = card['algorithm_read'].lower()
    assert 'not swept' in read
    assert 'weak low is valid' not in read
    assert 'bos is confirmed' not in read
    assert 'confirmed' not in read
    assert 'valid' not in read


def test_weak_high_is_the_short_with_the_5m_trigger():
    market = weak_long_market()
    higher = view(market, '1h')
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'high')
    pivot = next(row for row in higher['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[1] = swing['price'] - 1
    pivot[4] = swing['price'] - 4
    pivot[2] = swing['price']
    pivot[3] = min(pivot[3], pivot[4])
    lower = view(market, '5m')
    latest = lower['recent_ohlcv'][-1]
    latest[1:5] = [100., 101., 96., 97.]
    lower['features']['structure_shifts'] = [dict(at=latest[0], direction='bearish', level=99., kind='choch')]
    high_swing = lower['features']['last_swing_high']
    origin = next(row for row in lower['recent_ohlcv'] if row[0] == high_swing['pivot_at'])
    origin[2] = 103.
    high_swing['price'] = 103.
    for row in lower['features']['swings']:
        if row.get('kind') == 'high':
            row['price'] = 103.
    _bullish_block(higher, low=80., high=86.)
    card, = [row for row in _internal(market, 99.) if row['side'] == 'short']
    assert card['trigger_timeframe'] == '5m'
    assert card['evidence']['weak_field'] == 'high'
    assert card['stop_reference']['field'] == 'high'
    assert card['stop_loss_price'] > 103.
    assert card['target_reference']['timeframe'] == '1h'
    assert card['evidence']['alignment'] == 'against_higher_timeframe'
    assert len(card['opposing_target_levels']) == 1
    assert card['take_profit_price'] == pytest.approx(94.)
    assert 'confirmed' not in card['algorithm_read'].lower()


def test_a_sweep_is_not_required_and_taken_liquidity_is_not_a_weak_low():
    untouched = weak_long_market()
    assert _internal(untouched)
    taken = weak_long_market()
    higher = view(taken, '1h')
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'low')
    higher['features']['sweeps'] = [dict(direction='low_sweep', at=higher['recent_ohlcv'][-2][0], level=swing['price'])]
    assert _internal(taken) == []


def test_counter_structure_target_is_only_the_next_block_and_4h_is_the_better_target():
    against = weak_long_market()
    view(against, '4h')['features']['trend'] = 'bearish'
    _bullish_block(view(against, '1h'), low=70., high=74.)
    # The long still targets the next bearish block. The extra bullish block is not a long target.
    card, = _internal(against)
    assert card['evidence']['alignment'] == 'against_higher_timeframe'
    assert len(card['opposing_target_levels']) == 1
    assert card['opposing_target_levels'][0]['timeframe'] == '1h'
    assert card['take_profit_price'] == pytest.approx(120.)

    nearer = weak_long_market(target_high=104., target_low=102., farther=140.)
    card, = _internal(nearer)
    assert card['evidence']['alignment'] == 'with_higher_timeframe'
    assert [level['price'] for level in card['opposing_target_levels']] == [104., 140.]
    assert card['take_profit_price'] == pytest.approx(104.)
    target, ratio, level = choose_zone_target(
        card, entry=100., stop=card['stop_loss_price'], fee=0.0006, slippage=0.0005, price_step=.01)
    assert level['timeframe'] == '4h'
    assert target == pytest.approx(140.)
    assert ratio >= 1.25
    close = weak_long_market(target_high=130., target_low=122., farther=160.)
    rich, = _internal(close)
    target, ratio, level = choose_zone_target(
        rich, entry=100., stop=rich['stop_loss_price'], fee=0.0006, slippage=0.0005, price_step=.01)
    assert level['timeframe'] == '1h'
    assert target == pytest.approx(130.)


def _answer(packet):
    option = next(row for row in packet['account']['setup_selection']['options']
                  if row['evidence']['kind'] == 'htf_zone_internal')
    user = json.dumps(packet)
    assert 'weak low is valid' not in user.lower()
    assert 'bos is confirmed' not in user.lower()
    assert option['evidence']['weak_level']
    assert option['evidence']['trigger_candle']['timeframe'] == '5m'
    assert option['evidence']['candidate_block']['timeframe'] == '1h'
    assert option['swing_highs'] and option['swing_lows']
    assert option['algorithm_read']
    assert 'Early path:' in packet['account']['runner3_entry_gate']
    return dict(
        action='trade', signal_id=packet['candidate']['signal_id'], setup_id=option['setup_id'],
        rationale='1s zayıf dip ve 5m tetik okundu. Model bu planı kabul etti.',
        invalidation='5m swing altı bozulursa biter.', watch_note='',
        sizing_plan={'leverage': 5, 'risk_per_trade_pct': .001},
        price_plan=dict(
            entry_type='market', entry_price=packet['candidate']['entry_price'],
            entry_timeframe='5m', stop_loss_price=option['stop_loss_price'],
            take_profit_price=option['take_profit_price'],
            stop_reference=option['stop_reference'], target_reference=option['target_reference'],
            reason='5m swing altı stop ve sıradaki 1s blok.'))


def test_the_first_look_asks_the_model_and_the_next_look_does_not(tmp_path, monkeypatch, caplog):
    market = _rebase(weak_long_market())
    market['symbol'] = 'BTC_USDT'

    def text(packet):
        return _answer(packet)

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    caplog.set_level(logging.INFO, logger='worker')
    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, market=market)
    first = run()
    assert model.calls
    assert first.action.value == 'trade'
    assert first.proposal is not None
    assert first.proposal.take_profit_price == pytest.approx(120.)
    assert 'BTC_USDT' in caplog.text and 'model agreed' in caplog.text
    from trading.evaluation.context import compact_decision_packet
    compact = json.loads(compact_decision_packet(model.calls[0]['user']))
    assert any(row.get('weak_level') for row in compact['setups'])
    assert any(row.get('trigger_candle') for row in compact['setups'])
    assert any((row.get('candidate_block') or {}).get('timeframe') == '1h' for row in compact['setups'])
    assert any(row.get('read') for row in compact['setups'])
    assert any(row.get('highs') and row.get('lows') for row in compact['setups'])

    def asked(packet):
        raise AssertionError('model was asked again')

    monkeypatch.setattr('trading.analysis.build_bundle', lambda *args, **kwargs: SimpleNamespace(
        fingerprint='lane-2', to_context=lambda **kw: deepcopy(market)))
    second_model = DecisionModel(asked, vision)
    from trading.config import Settings
    reviewed = ContextEvaluator(
        second_model, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        Settings(_env_file=None, analysis_version='text', live_structure_setups_enabled=True, live_price_plan_enabled=True))
    from .test_watch_service import signal
    from trading.models.domain import Side
    seed = signal(Side.LONG)
    seed.symbol = 'BTC_USDT'
    seed.entry_price = 100.
    seed.features.update(htf_zone_seed=True, source_closed_at=time.time() - 1, dedup_key='seed-2')
    second = reviewed.evaluate_candidates(
        [seed], equity=11.58, risk_per_trade_pct=.03, target_leverage=20,
        live_account_context=dict(mode='LIVE', currency='USDT', runner_id='runner3',
                                  current_equity=11.58, available_usdt=11.58),
        live_positions={})
    assert second_model.calls == []
    assert second.metadata['model_not_asked'] is True
    assert second.action.value == 'trade'
    assert 'model was not asked again' in caplog.text


def test_under_one_point_two_five_r_is_vetoed_by_name(tmp_path, monkeypatch):
    market = _rebase(weak_long_market(target_high=103., target_low=101.5))
    market['symbol'] = 'BTC_USDT'

    def text(packet):
        return _answer(packet)

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, _store = evaluator(tmp_path, monkeypatch, model, market=market)
    result = run()
    assert model.calls
    assert result.action.value == 'hold'
    assert result.hold_reason == 'cost_adjusted_r'
    assert result.metadata['veto'] == 'cost_adjusted_r'
    assert result.proposal is None


def test_an_unparsed_review_does_not_requeue(tmp_path, monkeypatch, caplog):
    from trading.evaluation.watch import WatchCoordinator
    from trading.runtime.live_watch import LiveWatchReviewer
    from .test_live_opportunity_flow import reviewer

    market = rebase_market('BTC_USDT')

    def text(packet):
        return None

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    caplog.set_level(logging.INFO, logger='worker')
    model = DecisionModel(text, vision)
    _run, store = evaluator(tmp_path, monkeypatch, model, market=market)
    watcher, _coordinator, runner = reviewer(tmp_path / 'watch')
    runner.store = store
    watcher.r.store = store
    own = WatchCoordinator(store)
    from .test_watch_service import plan, signal
    own.commit(signal(), 'watch', 0, watch_plan=plan(), rationale='Eski plan duruyor, yapı izleniyor.')
    armed = own.card('BTC_USDT')
    armed['created_at'] = time.time() - 30
    store.put('watch:BTC_USDT', armed)
    assert own.request_structure_review('BTC_USDT', dict(
        timeframe='15m', available_at=time.time() - 1, direction='bearish', level=99., kind='confirmed_opposing_BOS'),
        expected_revision=own.card('BTC_USDT')['revision'])
    assert own.card('BTC_USDT')['reason'] == 'structure_changed'

    def text_again(packet):
        return None

    model.text = text_again
    from trading.models.domain import Side
    seed = signal(Side.SHORT)
    seed.entry_price = 100.
    seed.features.update(htf_zone_seed=True, source_closed_at=time.time() - 1,
                         dedup_key='unparsed', review_reason='structure_changed')
    result = ContextEvaluator(
        model, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        __import__('trading.config', fromlist=['Settings']).Settings(
            _env_file=None, analysis_version='text', live_structure_setups_enabled=True, live_price_plan_enabled=True),
    ).evaluate_candidates(
        [seed], equity=11.58, risk_per_trade_pct=.03, target_leverage=20,
        live_account_context=dict(mode='LIVE', currency='USDT', runner_id='runner3',
                                  current_equity=11.58, available_usdt=11.58),
        live_positions={})
    assert result.hold_reason == 'invalid_or_unavailable_model'
    assert 'UNPARSED' in caplog.text and 'BTC_USDT' in caplog.text
    assert own.card('BTC_USDT')['status'] != 'triggered'
    assert own.card('BTC_USDT')['reason'] == 'structure_reviewed'
    monkeypatch.setattr('trading.runtime.live_watch.structure_features', lambda candles: {'breaks': []})
    assert LiveWatchReviewer(SimpleNamespace(
        store=store, cfg=__import__('trading.config', fromlist=['Settings']).Settings(_env_file=None),
        public=watcher.r.public,
        quote=lambda symbol: __import__('trading.exchange.mexc_public', fromlist=['TickerSnapshot']).TickerSnapshot(
            symbol, 100., 99.99, 100., 1e6, raw={'timestamp': time.time() * 1000, 'fairPrice': 100.}))).one('BTC_USDT') is None


def test_no_touch_stores_one_trigger_and_is_not_sent_again(tmp_path, monkeypatch):
    from trading.evaluation.watch import WatchCoordinator
    from trading.quant.structure_setups import build_htf_zone_setups
    from .test_watch_service import plan, signal

    market = rebase_market('BTC_USDT')
    lower = next(row for row in market['timeframes'] if row['timeframe'] == '15m')
    lower['recent_ohlcv'][-3][3] = 98.
    lower['recent_ohlcv'][-1][3] = 98.
    lower['features']['structure_shifts'] = []
    assert not any(row['state'] == 'ready' for row in build_htf_zone_setups(market, current_price=100., price_step=.01))
    assert any(row['evidence']['stage'] == 'awaiting_touch' for row in build_htf_zone_setups(market, current_price=100., price_step=.01))

    def text(packet):
        raise AssertionError('untouched zone was sent to the model')

    def vision(model, packet):
        raise AssertionError('untouched zone was sent to the model')

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, market=market)
    own = WatchCoordinator(store)
    own.commit(signal(), 'watch', 0, watch_plan=plan(), rationale='Temas yok, tek seviye bekleniyor.')
    armed = own.card('BTC_USDT')
    armed['created_at'] = time.time() - 30
    store.put('watch:BTC_USDT', armed)
    assert own.request_structure_review('BTC_USDT', dict(
        timeframe='15m', available_at=time.time() - 1, direction='bearish', level=99., kind='confirmed_opposing_BOS'),
        expected_revision=own.card('BTC_USDT')['revision'])

    def drive():
        seed = signal()
        seed.entry_price = 100.
        seed.features.update(htf_zone_seed=True, source_closed_at=time.time() - 1,
                             dedup_key='touch-' + str(time.time()), review_reason='structure_changed')
        return ContextEvaluator(
            model, store,
            SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
            __import__('trading.config', fromlist=['Settings']).Settings(
                _env_file=None, analysis_version='text', live_structure_setups_enabled=True,
                live_price_plan_enabled=True)).evaluate_candidates(
            [seed], equity=11.58, risk_per_trade_pct=.03, target_leverage=20,
            live_account_context=dict(mode='LIVE', currency='USDT', runner_id='runner3',
                                      current_equity=11.58, available_usdt=11.58),
            live_positions={})

    first = drive()
    assert model.calls == []
    assert first.metadata['model_not_asked'] is True
    held = store.get('price_action_holds')[-1]
    assert held['single_trigger'] is True
    assert len(held['triggers']) == 1
    assert own.card('BTC_USDT')['reason'] == 'structure_reviewed'
    second = drive()
    assert model.calls == []
    assert second.metadata['model_not_asked'] is True
    assert len(store.get('price_action_holds')[-1]['triggers']) == 1
