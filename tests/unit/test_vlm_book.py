"""VLM is asked at useful moments. Silence and a wider stop leave the live book alone."""
import json
from time import time
from types import SimpleNamespace

from trading.runtime.vlm_book import four_hour_close_blocked, structure_break_outcome
from .test_live_management_runtime import prepare


def test_4h_hold_ignores_a_lone_15m_break(tmp_path, monkeypatch):
    held = structure_break_outcome(context_timeframe='4h', side='long', broken_timeframe='15m',
                                   higher_breaks=[{'direction': 'bullish', 'at': 1}])
    assert held['action'] == 'hold' and held['close'] is False and held['write'] is False
    assert held['reason'] == '4h_structure_holds'
    assert four_hour_close_blocked(action='close', context_timeframe='4h', review_timeframe='15m',
                                   side='long', higher_breaks=[{'direction': 'bullish', 'at': 1}])
    # The 4h break itself is not ignored by this rule.
    assert not four_hour_close_blocked(action='close', context_timeframe='4h', review_timeframe='15m',
                                       side='long', higher_breaks=[{'direction': 'bearish', 'at': 2}])
    protect = structure_break_outcome(context_timeframe='1h', side='long', broken_timeframe='5m',
                                      higher_breaks=[{'direction': 'bullish', 'at': 1}])
    assert protect['action'] == 'protect' and protect['may_tighten'] is True and protect['close'] is False

    runner, private, executor, control, llm, market, row = prepare(tmp_path, monkeypatch)
    row['proposal']['metadata']['htf_setup'] = dict(context_timeframe='4h', trigger_timeframe='15m')
    executor._save(row)
    next(view for view in market['timeframes'] if view['timeframe'] == '4h')['features'] = dict(
        breaks=[dict(direction='bullish', at=1)])
    next(view for view in market['timeframes'] if view['timeframe'] == '15m')['features'] = dict(
        breaks=[dict(direction='bearish', at=1)])
    llm.action = 'close'
    runner.manage_positions()
    assert private.closes == [] and private.modifies == []
    kept = executor.state(row['intent_id'])
    assert kept['status'] == 'protected'
    assert kept.get('current_protection', {}).get('stop', row['proposal']['stop_loss']['price']) == row['proposal']['stop_loss']['price']
    assert runner.store.get('management_trace')['execution_reason'] == '4h_structure_holds'
    assert runner.store.get('management_status')['status'] == 'hold'


class BookModel:
    def __init__(self, root=None):
        self.root = root or self
        if root is None:
            self.calls = []
            self.answer = lambda packet: None
        self.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success', 'input_tokens': 20, 'output_tokens': 20}]}
        self.last_provider = 'gemini'
        self.last_model = 'vision'
        self.last_status = 'success'

    def fork(self):
        return BookModel(self.root)

    def close(self):
        pass

    def complete_json(self, **kwargs):
        packet = json.loads(kwargs['user'])
        self.root.calls.append(kwargs)
        self.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success', 'input_tokens': 20, 'output_tokens': 20}]}
        if packet.get('task') != 'vlm_book_review':
            return dict(action='hold', position_id=packet['position']['position_id'],
                        reason='Mevcut koruma duruyor; yeni kapanış yok.', review_timeframe='15m')
        answer = self.root.answer
        return answer(packet) if callable(answer) else answer


def _arm(executor, row):
    proposal = row['proposal']
    proposal['rationale'] = '4s bölge teması ve 15d kapanmış CHoCH ile girildi.'
    proposal['metadata'] = dict(proposal.get('metadata') or {}, choch_extreme=98.2,
                                htf_setup=dict(context_timeframe='4h', trigger_timeframe='15m'),
                                price_plan=dict(stop_reference=dict(timeframe='15m', open_time=1, field='low')))
    executor._save(row)


def _book(tmp_path, monkeypatch):
    runner, private, executor, control, _llm, market, row = prepare(tmp_path, monkeypatch)
    model = BookModel()
    runner.llm = model
    _arm(executor, row)
    return runner, private, executor, model, row


def _move(runner, bid):
    runner.public.get_ticker = lambda symbol: SimpleNamespace(
        bid1=bid, ask1=bid + .05, last_price=bid, quote_volume_24h=1e6,
        raw={'timestamp': time() * 1000, 'fairPrice': bid + .02})


def _book_calls(model):
    return [call for call in model.calls if json.loads(call['user']).get('task') == 'vlm_book_review']


def _levels(executor, row):
    state = executor.state(row['intent_id'])
    stop, target = executor.protection_levels(state)
    return state, stop, target


def test_vlm_silence_leaves_stop_and_target(tmp_path, monkeypatch):
    runner, private, executor, model, row = _book(tmp_path, monkeypatch)
    model.answer = None
    _move(runner, 101)
    runner.manage_positions()
    runner.manage_positions()
    calls = _book_calls(model)
    assert len(calls) == 1
    state, stop, target = _levels(executor, row)
    assert stop == row['proposal']['stop_loss']['price']
    assert target == row['proposal']['take_profit_price']
    assert private.modifies == [] and private.closes == []
    assert state['status'] == 'protected'
    assert runner.store.get('vlm_book_status')['status'] == 'silent'


def test_vlm_tighten_is_applied(tmp_path, monkeypatch):
    runner, private, executor, model, row = _book(tmp_path, monkeypatch)

    def answer(packet):
        return dict(action='move_stop', position_id=packet['position']['position_id'],
                    reason='Kâr lehine stop teyitli fitilin üstüne çekildi.', stop_price=99.)

    model.answer = answer
    _move(runner, 101)
    runner.manage_positions()
    state, stop, target = _levels(executor, row)
    assert stop == 99
    assert target == row['proposal']['take_profit_price']
    assert len(private.modifies) == 1
    assert state['management_events'][-1]['status'] == 'confirmed'
    assert runner.store.get('vlm_book_status')['status'] == 'confirmed'


def test_vlm_widen_is_rejected(tmp_path, monkeypatch):
    runner, private, executor, model, row = _book(tmp_path, monkeypatch)

    def answer(packet):
        assert packet['choch_stop']['extreme'] == 98.2
        return dict(action='move_stop', position_id=packet['position']['position_id'],
                    reason='Stop teyit fitilinin ötesine genişletilemez.', stop_price=97.)

    model.answer = answer
    _move(runner, 101)
    runner.manage_positions()
    state, stop, target = _levels(executor, row)
    assert stop == row['proposal']['stop_loss']['price']
    assert target == row['proposal']['take_profit_price']
    assert private.modifies == [] and private.closes == []
    assert state['status'] == 'protected'
    status = runner.store.get('vlm_book_status')
    assert status['status'] == 'rejected' and status['past_extreme'] is True


def _watch(runner, symbol, status):
    runner.store.put('watch:' + symbol, dict(
        symbol=symbol, revision=1, status=status, side='long', created_at=time() - 10, triggered_at=time() - 5,
        structure_event=None, last_rationale='Tetiklenen izleme bir kez grafik ile bakılsın.',
        plan=dict(trigger_condition='price_above', trigger_price=100, invalidation_condition='below',
                  invalidation_price=90, expires_at=time() + 3600),
        candidate=dict(side='long', entry_price=100, stop_loss_price=95, take_profit_price=110,
                       rationale='Tetiklenen izleme bir kez grafik ile bakılsın.')))


def test_idle_watch_is_reviewed_once_before_and_once_after(tmp_path, monkeypatch):
    runner, private, executor, _control, _llm, _market, row = prepare(tmp_path, monkeypatch)
    model = BookModel()
    runner.llm = model
    _watch(runner, 'ETH_USDT', 'triggered')
    runner._cycle_lock.acquire()
    try:
        runner.manage_positions()
    finally:
        runner._cycle_lock.release()
    assert _book_calls(model) == []
    runner.manage_positions()
    runner.manage_positions()
    before = _book_calls(model)
    assert len(before) == 1
    assert json.loads(before[0]['user'])['phase'] == 'before_order'
    assert private.modifies == [] and private.closes == []
    _watch(runner, row['proposal']['symbol'], 'position_opened')
    runner.manage_positions()
    runner.manage_positions()
    after = _book_calls(model)
    assert len(after) == 2
    assert json.loads(after[1]['user'])['phase'] == 'after_order'
    assert private.modifies == [] and private.closes == []


def test_favor_reviews_fire_once_at_one_and_two_percent(tmp_path, monkeypatch):
    runner, private, executor, model, row = _book(tmp_path, monkeypatch)

    def answer(packet):
        return dict(action='hold', position_id=packet['position']['position_id'],
                    reason='Eşik incelemesi mevcut stopu ve hedefi bırakıyor.')

    model.answer = answer
    _move(runner, 101)
    runner.manage_positions()
    runner.manage_positions()
    first = _book_calls(model)
    assert len(first) == 1
    packet = json.loads(first[0]['user'])
    assert packet['phase'] == '1pct'
    assert packet['why_we_entered'].startswith('4s bölge')
    assert packet['choch_stop']['extreme'] == 98.2
    assert packet['choch_stop']['price'] == row['proposal']['stop_loss']['price']
    assert packet['target'] == row['proposal']['take_profit_price']
    assert packet['equity'] == 500
    assert packet['position']['side'] == 'long'
    assert packet['charts'] == first[0]['images']
    assert 'CHoCH' in first[0]['system'] and '15m' in first[0]['system'] and '5m' in first[0]['system']

    _move(runner, 102)
    runner.manage_positions()
    runner.manage_positions()
    second = _book_calls(model)
    assert len(second) == 2
    assert json.loads(second[1]['user'])['phase'] == '2pct'
    assert private.modifies == [] and private.closes == []
    _state, stop, target = _levels(executor, row)
    assert stop == row['proposal']['stop_loss']['price']
    assert target == row['proposal']['take_profit_price']
