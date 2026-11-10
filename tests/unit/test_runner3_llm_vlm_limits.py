"""LLM decides first. VLM may replace it. Limits stay off the exchange."""
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

from trading.agents.schemas import EvaluationResult, GraphAction
from trading.config import Settings
from trading.evaluation.service import ContextEvaluator
from trading.models.domain import Side
from trading.runtime.store import RuntimeStore
from .test_structure_setups import NOW, htf_zone_context, view
from .test_watch_service import signal


def rebase_market(symbol='BTC_USDT'):
    offset = int(time.time()) - NOW - 1

    def rebase(value):
        if isinstance(value, dict):
            return {key: rebase(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1000000:
            return value + offset
        return value

    market = rebase(htf_zone_context('4h', '15m'))
    market['symbol'] = symbol
    return market


def ready_answer(data, **extra):
    option = next(row for row in data['account']['setup_selection']['options']
                  if row['family'] == 'pullback' and row['state'] == 'ready')
    answer = dict(action='trade', signal_id=data['candidate']['signal_id'], setup_id=option['setup_id'],
                  rationale='Kapanmış bölge ve CHoCH/MSS teyidine göre işlem.',
                  invalidation='Yapısal dip altı.', watch_note='',
                  sizing_plan={'leverage': 5, 'risk_per_trade_pct': .001},
                  price_plan=dict(entry_type='market', entry_price=data['candidate']['entry_price'],
                                  entry_timeframe=option['trigger_timeframe'],
                                  stop_loss_price=option['stop_loss_price'],
                                  take_profit_price=option['take_profit_price'],
                                  stop_reference=option['stop_reference'],
                                  target_reference=option['target_reference'],
                                  reason='Teyitli fitil altı stop ve gözlenen üst likidite.'))
    answer.update(extra)
    return answer


def evaluator(tmp_path, monkeypatch, model, market=None, version='text', prepare=None):
    market = rebase_market() if market is None else market
    monkeypatch.setattr('trading.analysis.build_bundle', lambda *args, **kwargs:
                        SimpleNamespace(fingerprint='lane', to_context=lambda **kw: deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle', lambda *args, **kwargs:
                        [SimpleNamespace(path=tmp_path / 'chart.png')])
    settings = Settings(_env_file=None, analysis_version=version, live_structure_setups_enabled=True,
                        live_price_plan_enabled=True)
    store = RuntimeStore(str(tmp_path / 'state'))
    reviewed = ContextEvaluator(model, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        settings)
    seed = signal(Side.SHORT)
    seed.symbol = market.get('symbol') or 'BTC_USDT'
    seed.entry_price = 100.
    seed.features.update(htf_zone_seed=True, source_closed_at=time.time() - 1, dedup_key='seed')
    if prepare is not None:
        prepare(seed, market)

    def run(**account):
        context = dict(mode='LIVE', currency='USDT', runner_id='runner3', current_equity=11.58,
                       available_usdt=11.58)
        context.update(account)
        return reviewed.evaluate_candidates([seed], equity=11.58, risk_per_trade_pct=.03, target_leverage=20,
                                            live_account_context=context, live_positions={})

    return run, store


class DecisionModel:
    budget = None
    last_status = 'success'
    last_provider = 'mock'
    last_model = 'text-model'
    last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}
    deadline = None

    def __init__(self, text, vision):
        self.text = text
        self.vision = vision
        self.calls = []
        self.gate = threading.Event()

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        packet = json.loads(kwargs['user'])
        if kwargs.get('images'):
            self.last_model = 'vision-model'
            self.vision(self, packet)
            return self.vision_answer
        self.last_model = 'text-model'
        self.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}
        self.text_answer = self.text(packet)
        return self.text_answer


def test_vlm_timeout_keeps_the_text_decision_and_labels_it_text(tmp_path, monkeypatch):
    monkeypatch.setattr('trading.evaluation.service.VLM_DECISION_TIMEOUT_SECONDS', 0.05)

    def text(packet):
        return ready_answer(packet, rationale='Metin modeli hazır kurulumu şimdi alıyor.')

    def vision(model, packet):
        assert packet['account']['text_model_decision']['action'] == 'trade'
        assert model.calls[-1].get('images')
        option = next(row for row in packet['account']['setup_selection']['options']
                      if row['family'] == 'pullback' and row['state'] == 'ready')
        entry = packet['candidate']['entry_price']
        model.late_limit = entry - 1 if option['side'] == 'long' else entry + 1
        model.gate.wait(2)
        model.vision_answer = ready_answer(packet, limit_price=model.late_limit,
                                           rationale='Grafik modeli geç kaldı ve limiti yazdı.')
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model)
    started = time.time()
    try:
        result = run()
    finally:
        model.gate.set()
    assert time.time() - started < 1.5
    assert result.action.value == 'trade'
    assert result.metadata['decision_ai']['review_mode'] == 'text'
    assert result.metadata['decision_ai']['model'] == 'text-model'
    assert result.metadata['rationale'].startswith('Metin modeli')
    assert '15m CHoCH' in model.calls[0]['system']
    assert '5m CHoCH' in model.calls[0]['system']
    assert 'Do not open a hedge' in model.calls[0]['system']
    assert 'Touch alone is not an entry' in model.calls[0]['system']
    trace = store.get('analysis_trace')
    assert trace['decision_mode'] == 'text'
    assert trace['attempts'][-1] == {'review_mode': 'vlm', 'status': 'timeout', 'generation_requests': 0}
    assert result.metadata['pending_vlm'].settled.wait(2)
    assert result.metadata['pending_vlm'].late_action == 'local_plan'
    assert result.action.value == 'trade'
    assert result.metadata['decision_ai']['review_mode'] == 'text'
    stored = store.get('local_plan_updates')
    assert stored and stored[-1]['status'] == 'next_trigger'
    limits = store.get('local_entry_limits')
    assert limits[-1]['limit_price'] == model.late_limit
    assert limits[-1]['status'] == 'resting'
    assert limits[-1]['decision_mode'] == 'vlm'


def hold_answer(rationale):
    return dict(action='hold', signal_id=None, rationale=rationale,
                invalidation='Kurulum bozulursa bekleme biter.', watch_note='')


def test_silent_or_invalid_vlm_keeps_the_text_decision_without_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr('trading.evaluation.service.VLM_DECISION_TIMEOUT_SECONDS', 2)

    def text(packet):
        return ready_answer(packet, rationale='Metin modeli hazır kurulumu şimdi alıyor.')

    def vision(model, packet):
        model.vision_answer = None if silent else {'action': 'trade', 'rationale': 'bozuk'}
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    for silent in (True, False):
        model = DecisionModel(text, vision)
        run, store = evaluator(tmp_path / ('silent' if silent else 'invalid'), monkeypatch, model)
        started = time.time()
        result = run()
        assert time.time() - started < 1
        assert result.action.value == 'trade'
        assert result.proposal is not None
        assert result.metadata['decision_ai']['review_mode'] == 'text'
        assert result.metadata['rationale'].startswith('Metin modeli')
        assert store.get('local_entry_limits', []) == []
        assert store.get('analysis_trace')['decision_mode'] == 'text'


def test_text_and_vlm_silence_or_invalid_output_places_no_order(tmp_path, monkeypatch):
    def text(packet):
        if boom:
            raise RuntimeError('provider down')
        return None if silent else {'action': 'trade'}

    def vision(model, packet):
        assert packet['account']['text_model_decision']['action'] is None
        model.vision_answer = None if silent or boom else {'no': 'decision'}
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    for silent, boom in ((True, False), (False, False), (False, True)):
        model = DecisionModel(text, vision)
        run, store = evaluator(tmp_path / f'{silent}-{boom}', monkeypatch, model)
        result = run()
        assert result.action.value == 'hold'
        assert result.proposal is None
        assert result.hold_reason == 'invalid_or_unavailable_model'
        assert store.get('analysis_trace')['action'] == 'hold'
        assert store.get('analysis_trace')['execution_status'] == 'not_opened'
        assert store.get('local_entry_limits', []) == []


def test_valid_bekle_places_no_order_and_does_not_chase_another_route(tmp_path, monkeypatch):
    def text(packet):
        return hold_answer('Metin modeli bekliyor.') if text_holds else ready_answer(
            packet, rationale='Metin modeli hazır kurulumu şimdi alıyor.')

    def vision(model, packet):
        model.vision_answer = hold_answer('Grafik modeli bekliyor.')
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

    for text_holds in (True, False):
        model = DecisionModel(text, vision)
        run, _store = evaluator(tmp_path / str(text_holds), monkeypatch, model)
        result = run()
        assert result.action.value == 'hold'
        assert result.proposal is None
        assert result.hold_reason == 'hold'
        assert len(model.calls) == 2
        assert model.calls[1].get('images')
        assert result.metadata['decision_ai']['review_mode'] == 'vlm'


def test_missing_chart_does_not_block_the_text_decision(tmp_path, monkeypatch):
    def text(packet):
        return ready_answer(packet, rationale='Metin modeli grafiksiz kurulumu alıyor.')

    def vision(model, packet):
        raise AssertionError('chart failure must not call VLM')

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, version='vlm')
    monkeypatch.setattr('trading.analysis.render_bundle', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('chart')))
    started = time.time()
    result = run()
    assert time.time() - started < 1
    assert result.action.value == 'trade'
    assert result.metadata['decision_ai']['review_mode'] == 'text'
    assert result.metadata['rationale'].startswith('Metin modeli')
    assert len(model.calls) == 1
    assert model.calls[0].get('images') is None
    assert store.get('analysis_trace')['decision_mode'] == 'text'


def test_valid_vlm_decision_replaces_the_text_decision(tmp_path, monkeypatch):
    def text(packet):
        return dict(action='hold', signal_id=None, rationale='Metin modeli bu kurulumu bekletiyor.',
                    invalidation='Bölge bozulursa biter.', watch_note='')

    def vision(model, packet):
        assert packet['account']['text_model_decision']['rationale'].startswith('Metin modeli')
        model.vision_answer = ready_answer(packet, rationale='Grafik modeli kurulumu şimdi alıyor.')
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model)
    result = run()
    assert result.action.value == 'trade'
    assert result.metadata['decision_ai']['review_mode'] == 'vlm'
    assert result.metadata['decision_ai']['model'] == 'vision-model'
    assert result.metadata['rationale'].startswith('Grafik modeli')
    assert [row['review_mode'] for row in store.get('analysis_trace')['attempts']] == ['text', 'vlm']


def test_ranked_plans_and_open_positions_reach_the_model(tmp_path, monkeypatch):
    briefs = [dict(symbol='ETH_USDT', side='long', unrealized_pnl=-1.2,
                   thesis='Eski tez zayıf kaldı.',
                   remaining_room={'to_target_price': 2, 'to_target_r': .4},
                   leverage=5, entry=100, stop=98, target=104, mark=97)]
    queue = [dict(symbol='BTC_USDT', capital_rank=1, grade='standard'),
             dict(symbol='SOL_USDT', capital_rank=4, grade='strong', evidence_kind='htf_zone_mss')]

    def decide(flag, path):
        def text(packet):
            allocation = packet['account']['capital_allocation']
            assert allocation['small_account'] is True
            assert allocation['open_positions'][0]['symbol'] == 'ETH_USDT'
            assert allocation['open_positions'][0]['thesis'].startswith('Eski')
            assert allocation['queued_confirmed_plans'][0]['symbol'] == 'SOL_USDT'
            if flag:
                return ready_answer(packet, release_symbol='ETH_USDT',
                                    rationale='Yeni plan daha kârlı göründüğü için ETH kapatılsın.')
            return ready_answer(packet, release_symbol='ETH_USDT', thesis_invalidated=True,
                                rationale='ETH tezi fiyatla bozuldu ve serbest marjin yetmiyor.')

        def vision(model, packet):
            model.vision_answer = text(packet)
            model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

        model = DecisionModel(text, vision)
        run, store = evaluator(path, monkeypatch, model)
        return run(open_position_briefs=briefs, queued_confirmed_plans=queue, available_usdt=1), store

    better, better_store = decide(True, tmp_path / 'better')
    assert better.action.value == 'trade'
    assert better.metadata.get('release_symbol') is None
    both, store = decide(False, tmp_path / 'both')
    assert both.action.value == 'trade'
    assert both.metadata['release_symbol'] == 'ETH_USDT'
    assert both.metadata['thesis_invalidated'] is True
    assert both.metadata['decision_ai']['review_mode'] == 'vlm'
    assert store.get('local_entry_limits', []) == []
    assert better_store.get('local_entry_limits', []) == []


def test_zone_touch_trade_is_rejected_before_any_order(tmp_path, monkeypatch):
    market = rebase_market()
    lower = view(market, '15m')
    lower['features'].pop('structure_shifts', None)
    lower['recent_ohlcv'][-3][3] = 96.
    lower['recent_ohlcv'][-1][4] = 99.

    def text(packet):
        option = next(row for row in packet['account']['setup_selection']['options'] if row['state'] == 'watch')
        assert option['evidence']['stage'] == 'awaiting_confirmation'
        return dict(action='trade', signal_id=packet['candidate']['signal_id'], setup_id=option['setup_id'],
                    rationale='Temas oldu diye işlem açıyorum.', invalidation='Bölge altı.', watch_note='',
                    sizing_plan={'leverage': 5, 'risk_per_trade_pct': .001},
                    price_plan=dict(entry_type='market', entry_price=packet['candidate']['entry_price'],
                                    entry_timeframe=option['trigger_timeframe'],
                                    stop_loss_price=option['stop_loss_price'],
                                    take_profit_price=option['take_profit_price'],
                                    stop_reference=option['stop_reference'],
                                    target_reference=option['target_reference'],
                                    reason='Temas tek başına yeterli sanıldı.'))

    def vision(model, packet):
        model.vision_answer = text(packet)
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, market)
    result = run()
    assert result.action.value == 'hold'
    assert result.hold_reason == 'invalid_or_unavailable_model'
    assert store.get('local_entry_limits', []) == []


def test_local_limit_is_not_an_exchange_order_and_a_later_market_plan_can_enter(tmp_path, monkeypatch):
    from trading.brokers.live.automatic import AutomaticLiveExecutor, AutomaticLivePolicy
    from trading.brokers.live.execution import LiveExecutionGates
    from trading.risk.engine import RiskEngine
    from trading.runtime.live_runner import LiveRunner
    from .test_live_execution import Client
    from .test_live_pipeline import QuotePublic

    template = rebase_market()

    def build(public, symbol, **kwargs):
        market = deepcopy(template)
        market['symbol'] = symbol
        return SimpleNamespace(fingerprint='lane-' + symbol, to_context=lambda **kw: deepcopy(market))

    monkeypatch.setattr('trading.analysis.build_bundle', build)
    monkeypatch.setattr('trading.analysis.render_bundle', lambda *args, **kwargs:
                        [SimpleNamespace(path=tmp_path / 'chart.png')])

    class Model:
        budget = None
        last_status = 'success'
        last_provider = 'mock'
        last_model = 'text-model'
        last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

        def complete_json(self, **kwargs):
            packet = json.loads(kwargs['user'])
            if kwargs.get('images'):
                self.last_model = 'vision-model'
            else:
                self.last_model = 'text-model'
            if packet['candidate']['symbol'] == 'ETH_USDT':
                return ready_answer(packet, rationale='ETH hazır; bakiye market girişe kullanılabilir.')
            if kwargs.get('images'):
                assert packet['account']['text_model_decision']['limit_price'] == 97
            return ready_answer(packet, limit_price=97,
                                rationale='BTC için 97 yerel limit. Borsaya emir gitmesin.')

    private, public = Client(), QuotePublic()
    private.assets = [dict(currency='USDT', equity=11., availableBalance=11., positionMargin=0., unrealized=0.)]
    private.ioc_prices = []

    def submit_ioc(*, limit_price, **kwargs):
        private.ioc_prices.append(limit_price)
        private.submit_market(entry_price=limit_price, **kwargs)
        private.order_row['orderType'] = 3
        private.stops = [dict(positionId=11, positionType=1, orderId=22, errorCode=0, state=1, isFinished=0,
                              lossTrend=2, profitTrend=2, stopLossType=0, takeProfitType=0, stopLossReverse=2,
                              takeProfitReverse=2, profitLossVolType='SAME', vol=kwargs['contracts'],
                              stopLossPrice=kwargs['stop_loss'], takeProfitPrice=kwargs['take_profit'])]

    private.submit_ioc = submit_ioc
    cfg = Settings(_env_file=None, broker_mode='live', live_trading_enabled=True, live_automatic_enabled=True,
                   live_structure_setups_enabled=True, live_price_plan_enabled=True,
                   live_allowed_accounts=('ahmet',), risk_profile='balanced', risk_per_trade_pct=.01,
                   default_target_leverage=5, kill_switch_path=str(tmp_path / 'STOP'),
                   analysis_chart_dir=str(tmp_path / 'charts'))
    store = RuntimeStore(str(tmp_path / 'runtime.sqlite3'))
    control = dict(runner_id='runner3', enabled=True, stopped=False, updated_at=1, version='text',
                   risk_profile='balanced', risk_per_trade_pct=.01, max_leverage=5, max_positions=2,
                   allocation_mode='full')
    executor = AutomaticLiveExecutor(AutomaticLivePolicy(True, ('ahmet',), ('7',), 2),
                                     LiveExecutionGates(True, 'live', ('7',), True), store, private, public,
                                     RiskEngine(settings=cfg), control_provider=lambda: dict(control),
                                     account_id='ahmet', binding_id='binding')
    runner = LiveRunner(cfg, store=store, account_id='ahmet', binding_id='binding', private=private, public=public,
                        llm=Model(), external_control=lambda: dict(control), executor=executor)
    executor.grant_start('7', 1)
    btc = signal(Side.SHORT)
    btc.features.update(source_closed_at=time.time(), dedup_key='btc-limit', htf_zone_seed=True, capital_rank=2)
    runner.candidate_queue().refresh([btc])
    first = runner.cycle()
    assert first['reason'] == 'HOLD_LIVE_MODEL', (first, store.get('analysis_trace'), store.get('live_candidate_error:BTC_USDT'))
    assert private.submits == [] and private.ioc_prices == []
    stored = store.get('local_entry_limits')
    assert stored[0]['symbol'] == 'BTC_USDT' and stored[0]['limit_price'] == 97
    assert stored[0]['status'] == 'resting' and stored[0]['decision_mode'] == 'vlm'
    assert store.get('analysis_trace')['action'] == 'local_limit'
    eth = signal(Side.SHORT)
    eth.symbol = 'ETH_USDT'
    eth.features.update(source_closed_at=time.time(), dedup_key='eth-market', htf_zone_seed=True, capital_rank=3)
    runner.candidate_queue().refresh([eth])
    second = runner.cycle()
    assert second['reason'] == 'LIVE_PROTECTED', (second, store.get('analysis_trace'), store.get('live_candidate_error:ETH_USDT'))
    assert private.ioc_prices == [100.]
    assert private.order_row['symbol'] == 'ETH_USDT'
    assert store.get('local_entry_limits')[0]['status'] == 'resting'
    assert store.get('local_entry_limits')[0]['limit_price'] == 97


def test_price_reaching_a_local_limit_reopens_analysis_without_an_order(tmp_path):
    from trading.evaluation.local_limits import save_local_limit
    from .test_live_runner import prepare

    runner, private, executor, control = prepare(tmp_path)
    control['runner_id'] = 'runner3'
    save_local_limit(runner.store, symbol='BTC_USDT', side='long', limit_price=100, rationale='97 gelince yeniden bak')
    seen = []

    def evaluate(candidate, snapshot, control):
        seen.append(candidate.features.get('local_limit_review'))
        return EvaluationResult(action=GraphAction.HOLD, hold_reason='hold', metadata={'provider_called': True})

    runner._evaluate = evaluate
    status = runner.cycle()
    assert seen == [True]
    assert status['reason'] == 'HOLD_LIVE_MODEL'
    assert executor.calls == []
    assert runner.store.get('local_entry_limits')[0]['status'] == 'review_queued'
    assert private.orders == []


def test_stronger_parallel_plan_is_applied_first(tmp_path):
    from .test_live_runner import candidate, prepare

    runner, _, executor, control = prepare(tmp_path)
    control['runner_id'] = 'runner3'
    runner.llm.fork = lambda: runner.llm
    weak = candidate('BTC_USDT')
    weak.features['htf_zone_seed'] = True
    weak.features['capital_rank'] = 1
    strong = candidate('ETH_USDT')
    strong.features['htf_zone_seed'] = True
    strong.features['capital_rank'] = 5
    runner.candidate_queue().refresh([weak, strong])
    order = []

    def evaluate(candidate, snapshot, control):
        if candidate.symbol == 'ETH_USDT':
            time.sleep(.15)
        return EvaluationResult(action=GraphAction.HOLD, hold_reason='hold', metadata={'provider_called': True})

    def apply(candidate, result, control):
        order.append(candidate.symbol)
        return runner.status('HOLD_LIVE_MODEL')

    runner._evaluate = evaluate
    runner._apply = apply
    assert runner.cycle()['reason'] == 'HOLD_LIVE_MODEL'
    assert order == ['ETH_USDT', 'BTC_USDT']
    assert executor.calls == []


def test_late_vlm_after_the_order_does_not_cancel_or_resize_it(tmp_path):
    from copy import deepcopy
    from trading.evaluation.pending_vlm import PendingVlm
    from trading.evaluation.service import ModelDecision
    from .test_live_runner import candidate, prepare, result

    runner, _, executor, control = prepare(tmp_path)
    seed = candidate()
    runner.candidate_queue().refresh([seed])
    trade = result(runner, seed)
    pending = PendingVlm(runner.store, fee=0.0006, slippage=0.0005)
    trade.metadata['pending_vlm'] = pending
    status = runner._apply(seed, trade, control)
    assert status['reason'] == 'LIVE_PROTECTED'
    sent = deepcopy(executor.proposal)
    late = ModelDecision.model_validate(dict(
        action='trade', signal_id=seed.signal_id, release_symbol='ETH_USDT', thesis_invalidated=True,
        limit_price=90, rationale='Geç gelen grafik emri küçültüp diğerini kapatsın.',
        invalidation='Stop altı.', watch_note='',
        sizing_plan={'leverage': 20, 'risk_per_trade_pct': .02},
        price_plan=dict(entry_type='market', entry_price=100, entry_timeframe='15m',
                        stop_loss_price=98, take_profit_price=106,
                        stop_reference=dict(timeframe='15m', open_time=1, field='low'),
                        target_reference=dict(timeframe='15m', open_time=1, field='high'),
                        reason='Bu plan yalnızca sonraki tetik için yazılır.')))
    pending.complete(dict(decision=late, symbol='BTC_USDT'))
    assert [row[0] for row in executor.calls] == ['authorize', 'execute']
    assert executor.proposal.notional == sent.notional
    assert executor.proposal.leverage == sent.leverage
    assert executor.proposal.stop_loss.price == sent.stop_loss.price
    assert pending.late_action == 'local_plan'
    assert runner.store.get('local_plan_updates')[-1]['status'] == 'next_trigger'


def test_profitable_plan_alone_does_not_close_an_open_trade(tmp_path, monkeypatch):
    from .test_live_runner import candidate, prepare, result

    runner, _, executor, control = prepare(tmp_path)
    seed = candidate()
    runner.candidate_queue().refresh([seed])
    trade = result(runner, seed)
    trade.metadata['release_symbol'] = 'ETH_USDT'
    closed = []
    monkeypatch.setattr(runner, '_release_confirmed', lambda symbol: closed.append(symbol) or False)
    status = runner._apply(seed, trade, control)
    assert closed == []
    assert status['reason'] == 'LIVE_PROTECTED'
    assert [row[0] for row in executor.calls] == ['authorize', 'execute']


def test_unconfirmed_close_does_not_open_the_replacement(tmp_path, monkeypatch):
    from trading.runtime.live_runner import LiveRunner
    from .test_live_runner import candidate, prepare, result

    runner, private, executor, control = prepare(tmp_path)
    seed = candidate()
    runner.candidate_queue().refresh([seed])
    trade = result(runner, seed)
    trade.metadata['release_symbol'] = 'ETH_USDT'
    trade.metadata['thesis_invalidated'] = True
    private.available = 1
    private.positions = [dict(positionId=45, symbol='ETH_USDT', positionType=1, openType=1, state=1,
                              holdVol=1, leverage=3, holdAvgPrice=100, im=10, unRealizedPnl=-1, liquidatePrice=70)]
    executor.rows = [dict(intent_id='eth', position_id='45', status='protected',
                          proposal={'symbol': 'ETH_USDT', 'stop_loss': {'price': 100}, 'take_profit_price': 110})]
    monkeypatch.setattr(runner, '_release_confirmed', lambda symbol: False)
    status = runner._apply(seed, trade, control)
    assert status['reason'] == 'HOLD_LIVE_CLOSE_UNCONFIRMED'
    assert executor.calls == []

    class Unconfirmed:
        def request_close(self, symbol):
            return {'status': 'close_write_unknown', 'intent_id': 'abc'}

        def _refresh(self):
            return {'positions': [{'symbol': symbol}]}

        executor = SimpleNamespace(reconcile=lambda intent: {'status': 'close_write_unknown'})

    class Confirmed(Unconfirmed):
        def _refresh(self):
            return {'positions': []}

        executor = SimpleNamespace(reconcile=lambda intent: {'status': 'closed'})

    assert LiveRunner._release_confirmed(Unconfirmed(), 'ETH_USDT') is False
    assert LiveRunner._release_confirmed(Confirmed(), 'ETH_USDT') is True

    def confirmed_close(symbol):
        private.available = 400
        return True

    monkeypatch.setattr(runner, '_release_confirmed', confirmed_close)
    assert runner._apply(seed, trade, control)['reason'] == 'LIVE_PROTECTED'
    assert [row[0] for row in executor.calls] == ['authorize', 'execute']


def test_confirmed_plans_rank_by_evidence_and_payoff():
    from trading.runtime.runner3_policy import rank_confirmed_plans

    ranked = rank_confirmed_plans([
        dict(symbol='BBB', capital_rank=9, grade='standard'),
        dict(symbol='AAA', capital_rank=1, grade='strong', evidence_kind='htf_zone_mss'),
        dict(symbol='CCC', capital_rank=3, grade='standard'),
    ])
    assert [row['symbol'] for row in ranked] == ['AAA', 'BBB', 'CCC']


def test_local_limits_rank_by_cost_adjusted_r_not_by_preference(tmp_path):
    from trading.evaluation.local_limits import rank_local_limits, save_local_limit
    from .test_live_runner import prepare

    ranked = rank_local_limits([
        dict(symbol='LOW', cost_adjusted_r=0.4, id='a'),
        dict(symbol='MISS', cost_adjusted_r=None, id='b'),
        dict(symbol='HIGH', cost_adjusted_r=2.5, id='c'),
        dict(symbol='NAN', cost_adjusted_r=float('nan'), id='d'),
    ])
    assert [row['symbol'] for row in ranked] == ['HIGH', 'LOW', 'MISS', 'NAN']
    runner, _, _, control = prepare(tmp_path)
    control['runner_id'] = 'runner3'
    save_local_limit(runner.store, symbol='BTC_USDT', side='long', limit_price=100,
                     rationale='düşük R', cost_adjusted_r=0.4)
    save_local_limit(runner.store, symbol='ETH_USDT', side='long', limit_price=100,
                     rationale='yüksek R', cost_adjusted_r=2.5)
    runner._queue_reached_limits()
    first = runner.candidate_queue().next(htf_zone_entries=True, lease_seconds=30)
    second = runner.candidate_queue().next(htf_zone_entries=True, lease_seconds=30)
    assert first.symbol == 'ETH_USDT' and first.features['capital_rank'] == 2.5
    assert second.symbol == 'BTC_USDT' and second.features['capital_rank'] == 0.4


def test_a_tighter_stop_is_rejected_for_a_small_account():
    import pytest
    from trading.evaluation.price_plan import CandleReference, MarketPricePlan
    from trading.runtime.runner3_policy import validate_evidence

    swing_stop = CandleReference(timeframe='15m', open_time=1, field='low')
    target = CandleReference(timeframe='4h', open_time=2, field='high')
    swing = dict(side='long', state='ready', context_timeframe='4h', trigger_timeframe='15m',
                 evidence=dict(kind='htf_zone_mss', stage='confirmed', expires_at=time.time() + 60),
                 stop_loss_price=90., stop_reference=swing_stop.model_dump(), target_reference=target.model_dump())
    tight = MarketPricePlan(entry_type='market', entry_price=100, stop_loss_price=92, take_profit_price=110,
                            entry_timeframe='15m', stop_reference=swing_stop, target_reference=target,
                            reason='Küçük hesap için stop daraltıldı.')
    with pytest.raises(ValueError, match='runner3_structural_stop_too_tight'):
        validate_evidence(swing, tight)
    validate_evidence(swing, tight.model_copy(update={'stop_loss_price': 90}))
    intraday_stop = CandleReference(timeframe='5m', open_time=1, field='low')
    intraday = dict(swing, context_timeframe='1h', trigger_timeframe='5m', stop_reference=intraday_stop.model_dump())
    exact = MarketPricePlan(entry_type='market', entry_price=100, stop_loss_price=90, take_profit_price=110,
                            entry_timeframe='5m', stop_reference=intraday_stop, target_reference=target,
                            reason='5m fitil stopu aynen duruyor.')
    validate_evidence(intraday, exact)
    with pytest.raises(ValueError, match='runner3_structural_stop_too_tight'):
        validate_evidence(intraday, exact.model_copy(update={'stop_loss_price': 92}))


def test_text_model_receives_the_guclu_packet_and_keeps_its_share(tmp_path, monkeypatch):
    import trading.quant.structure_setups as setups

    real = setups.build_htf_zone_setups

    def wrapped(*args, **kwargs):
        options = real(*args, **kwargs)
        for option in options:
            if option.get('state') != 'ready':
                continue
            option['macro_trend'] = 'bullish' if option['side'] == 'long' else 'bearish'
            option['reward_risk'] = 2.5
            option['evidence'] = dict(option['evidence'], context_last_break=dict(direction=option['macro_trend']))
        return options

    monkeypatch.setattr(setups, 'build_htf_zone_setups', wrapped)

    def text(packet):
        option = next(row for row in packet['account']['setup_selection']['options']
                      if row['family'] == 'pullback' and row['state'] == 'ready')
        choice = option['guclu_margin_choice']
        assert choice['label'] == 'Güçlü'
        assert choice['evidence'] == 'with-trend closed break and at least 2R'
        assert choice['equity_usdt'] == 11.58
        assert abs(choice['free_margin_usdt'] - 11.58 * .85) < 1e-9
        assert choice['open_positions'] == []
        assert choice['account_goal'] == 'grow the cash'
        assert choice['time_horizon'] == '4h swing'
        ceiling = .03 * (.5 if option['family'] == 'countertrend_sweep_reclaim' else 1.)
        assert abs(choice['choch_loss_ceiling_usdt'] - 11.58 * ceiling) < 1e-9
        assert option['allocation']['margin_fraction_role'] == 'silent_fallback'
        plan = dict(leverage=5, risk_per_trade_pct=.001)
        if text.share is not None:
            plan['margin_share'] = text.share
        return ready_answer(packet, sizing_plan=plan, rationale='Metin modeli Güçlü payı seçiyor.')

    def vision(model, packet):
        seen = packet['account']['text_model_decision']['sizing_plan']
        if text.share is None:
            assert 'margin_share' not in (seen or {})
        else:
            assert seen['margin_share'] == text.share
        model.vision_answer = ready_answer(
            packet, sizing_plan=dict(leverage=5, risk_per_trade_pct=.001, margin_share=.8),
            rationale='Grafik modeli payı büyütmek istiyor.')
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

    text.share = .22
    model = DecisionModel(text, vision)
    run, _store = evaluator(tmp_path / 'chosen', monkeypatch, model)
    chosen = run()
    assert 'Güçlü is not a fixed percent' in model.calls[0]['system']
    assert '35% for Güçlü' not in model.calls[0]['system']
    assert 'One writer does.' in model.calls[0]['system']
    assert chosen.metadata['decision_ai']['review_mode'] == 'vlm'
    assert chosen.metadata['guclu_margin_share'] == .22
    assert chosen.metadata['sizing_plan']['margin_share'] == .8

    text.share = None
    model = DecisionModel(text, vision)
    run, _store = evaluator(tmp_path / 'silent', monkeypatch, model)
    silent = run()
    assert silent.metadata['guclu_margin_share'] is None
    assert silent.metadata['sizing_plan']['margin_share'] == .8


def test_live_entry_prompt_states_the_cost_gate_before_a_proposal(tmp_path, monkeypatch):
    def text(packet):
        gate = packet['account']['runner3_entry_gate']
        assert 'Runner 3 minimum cost-adjusted R is 1.25.' in gate
        assert 'cost_adjusted_R = net / loss.' in gate
        assert 'The continuous equity stop cap is 15% of equity.' in gate
        return hold_answer('Hedef maliyet sonrası 1.25 R altında, bekliyorum.')

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, _store = evaluator(tmp_path, monkeypatch, model)
    result = run()
    system = model.calls[0]['system']
    for line in (
        'Price-action playbook, before you answer:',
        'A hold is not the default when the CHoCH, the block or flow, and a target exist.',
        'Do not hold that trade toward a far higher-timeframe target.',
        'Runner 3 minimum cost-adjusted R is 1.25.',
        'Fee assumption is 0.0006 per side. Exit-slippage assumption is 0.0005 (5 bps), applied to the stop and the target.',
        'stop_fill = stop * (1 - direction * slippage).',
        'target_fill = target * (1 - direction * slippage).',
        'loss = abs(entry - stop_fill) + fee * (entry + stop_fill).',
        'net = direction * (target_fill - entry) - fee * (entry + target_fill).',
        'cost_adjusted_R = net / loss.',
        'The engine rejects the proposal when loss <= 0, and when gross <= 0 or (gross - net) / gross > 0.30.',
        'The continuous equity stop cap is 15% of equity. One stop cannot lose more than that fraction of equity.',
        'When the setup is real (zone, touch, and a closed CHoCH/MSS) and at least one supplied opposing target has cost-adjusted R that clears 1.25, you must ENTER (action trade) at that supplied target.',
        'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands.',
        'If your action is trade and the target you name is one of the supplied opposing levels, the engine does not reject that choice for cost-adjusted R.',
        'Balance, free margin, and leverage (the multiplier) stay in the packet so you can size. They are not BEKLE reasons.',
        'These are not BEKLE reasons: account balance, free margin, leverage, open positions, a higher-timeframe trend that disagrees, consolidation or a correction, or "watch already exists".',
        'When the supplied setup already has a closed CHoCH/MSS, an unbroken higher timeframe is not missing confirmation.',
        'The 15% equity stop cap still applies. Do not move the stop.',
        'A silent or invalid answer opens nothing. When the selected setup is real and you hold, the engine enters the farthest supplied opposing target that clears 1.25 and keeps the stop. A hold with no such level stays a hold.',
        'Do not move the stop to manufacture R. Do not invent a level that was not supplied.',
        'Do not invent a trade when you are silent.',
        'A real setup (zone, touch, and a closed CHoCH/MSS) with at least one supplied opposing target whose cost-adjusted R clears 1.25 must be ENTER at that target.',
        'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands.',
        'A valid hold on that selected setup is entered at the farthest supplied level that clears 1.25. Silence still opens nothing.',
        'Do not move the stop to manufacture R. Do not invent a level. Do not invent a trade if you are silent.',
    ):
        assert line in system
    assert 'first choose a farther target' not in system
    assert 'Equity under 100 USDT is not a reason to enter.' not in system
    assert 'You may drop the trade' not in system
    assert 'BEKLE only when confirmation is missing or none of the supplied levels clears 1.25.' not in system
    assert 'cost_adjusted_R < 1.25' not in system
    assert system.index('you must ENTER (action trade)') < system.index('would otherwise block')
    assert system.index('would otherwise block') < system.index('That choice stands')
    assert system.index('Price-action playbook, before you answer:') < system.index('Runner 3 minimum cost-adjusted R is 1.25.')
    assert system.index('Runner 3 minimum cost-adjusted R is 1.25.') < system.index('Runner 3 requires the selected READY')
    assert system.index('Runner 3 requires the selected READY') < system.index('must be ENTER at that target')
    assert system.index('must be ENTER at that target') < system.index('A valid hold on that selected setup is entered')
    assert 'must be BEKLE (action hold) or a farther target' not in system
    for call in model.calls:
        assert 'you must ENTER (action trade)' in call['system']
        assert 'You may drop the trade' not in call['system']
        if call.get('images'):
            assert 'Do not drop or refuse a real setup when a supplied opposing target clears cost-adjusted R 1.25.' in call['system']
            assert 'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands.' in call['system']
            assert 'Balance, free margin, and leverage (the multiplier) stay in the packet so you can size.' in call['system']
            assert 'are not reasons to drop it.' in call['system']
    packet = json.loads(model.calls[0]['user'])
    account = packet['account']
    option = next(row for row in account['setup_selection']['options'] if row['state'] == 'ready')
    assert option['evidence']['zone']
    assert option['evidence']['touch_at']
    assert option['evidence']['shift']['kind'] in ('choch', 'mss')
    assert option['entry_reference'] > 0
    assert option['stop_loss_price'] > 0
    assert option['opposing_target_levels']
    assert option['target_reference'] == {
        key: option['opposing_target_levels'][0][key] for key in ('timeframe', 'open_time', 'field')}
    assert account['equity'] == 11.58
    assert account['leverage'] == 20
    assert account['capital_allocation']['available_usdt'] == 11.58
    assert account['capital_allocation']['equity_usdt'] == 11.58
    note = account['capital_allocation']['note']
    assert 'Equity under 100 USDT is not a reason to enter' not in note
    assert 'It is not a BEKLE reason when a supplied target clears cost-adjusted R 1.25.' in note
    assert 'Balance, free margin, and leverage (the multiplier) stay in this packet so you can size. They are not BEKLE reasons.' in note
    assert 'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands.' in note
    assert result.action.value == 'hold'
    assert result.proposal is None


def _ready_market():
    market = rebase_market()
    higher = next(view for view in market['timeframes'] if view['timeframe'] == '4h')
    rows = higher['recent_ohlcv']
    rows[0][2] = 101.
    rows[1][2] = 120.
    higher['features']['swings'].extend([
        dict(kind='high', price=101., pivot_at=rows[0][0], confirmed_at=rows[3][0]),
        dict(kind='high', price=120., pivot_at=rows[1][0], confirmed_at=rows[4][0]),
    ])
    return market


def _select_ready(seed, market):
    from trading.quant.structure_setups import build_htf_zone_setups

    ready = next(row for row in build_htf_zone_setups(market, current_price=100., price_step=.01)
                 if row['state'] == 'ready')
    seed.side = Side(ready['side'])
    seed.features['setup_id'] = ready['setup_id']
    _select_ready.setup = ready


def test_valid_hold_on_the_selected_ready_setup_enters_the_farthest_clearing_target(tmp_path, monkeypatch):
    from trading.runtime.runner3_policy import MIN_NET_TARGET_R, clearing_supplied_targets, cost_adjusted_r

    market = _ready_market()

    def text(packet):
        return hold_answer('Üst zaman dilimi düşüşte ve fiyat bölgeye değmedi.')

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, market=market, prepare=_select_ready)
    result = run()
    ready = _select_ready.setup
    levels = clearing_supplied_targets(
        ready, entry=100., fee=0.0006, slippage=0.0005, price_step=.01)
    assert levels[0]['price'] == 120.
    assert cost_adjusted_r(
        side='long', entry=100., stop=ready['stop_loss_price'], target=101.,
        fee=0.0006, slippage=0.0005) < MIN_NET_TARGET_R
    assert result.action.value == 'trade'
    assert result.proposal is not None
    assert abs(result.proposal.stop_loss.price - ready['stop_loss_price']) < 1e-9
    assert abs(result.proposal.take_profit_price - levels[0]['take_profit_price']) < 1e-9
    assert abs(result.proposal.take_profit_price - 120.) < 1e-9
    assert result.metadata['ready_setup_after_hold'] is True
    assert result.metadata['rationale'].startswith('Üst zaman dilimi')
    trace = store.get('analysis_trace')
    assert trace['action'] == 'trade'
    assert trace['model_action'] == 'hold'
    assert trace['ready_setup_after_hold'] is True
    assert trace['execution_status'] == 'awaiting_risk'

    def trading(packet):
        return ready_answer(packet, rationale='Model verilen yakın hedefi seçti.')

    trade_model = DecisionModel(trading, vision)
    trade_run, _trade_store = evaluator(
        tmp_path / 'trade', monkeypatch, trade_model, market=deepcopy(market), prepare=_select_ready)
    traded = trade_run()
    named = _select_ready.setup['take_profit_price']
    assert abs(named - 120.) > 1
    assert cost_adjusted_r(
        side='long', entry=100., stop=_select_ready.setup['stop_loss_price'], target=named,
        fee=0.0006, slippage=0.0005) < MIN_NET_TARGET_R
    assert traded.action.value == 'hold'
    assert traded.hold_reason == 'cost_adjusted_r'
    assert traded.metadata['veto'] == 'cost_adjusted_r'
    assert traded.proposal is None

    def silent(packet):
        return None

    silent_model = DecisionModel(silent, vision)
    silent_run, silent_store = evaluator(
        tmp_path / 'silent', monkeypatch, silent_model, market=deepcopy(market), prepare=_select_ready)
    missed = silent_run()
    assert missed.action.value == 'hold'
    assert missed.proposal is None
    assert missed.hold_reason == 'invalid_or_unavailable_model'
    assert silent_store.get('analysis_trace').get('ready_setup_after_hold') is not True

    monkeypatch.setattr(
        'trading.runtime.runner3_policy.clearing_supplied_targets', lambda *args, **kwargs: [])
    held_model = DecisionModel(text, vision)
    held_run, _held_store = evaluator(
        tmp_path / 'below', monkeypatch, held_model, market=deepcopy(market), prepare=_select_ready)
    held = held_run()
    assert held.action.value == 'hold'
    assert held.proposal is None
    assert held.hold_reason == 'hold'


def test_hold_stores_triggers_and_a_hit_sends_the_pair_back(tmp_path, monkeypatch, caplog):
    import logging

    from trading.config import Settings
    from trading.evaluation.hold_triggers import claim_printed, review_signal
    from trading.evaluation.service import ContextEvaluator

    market = rebase_market('USELESS_USDT')
    rationale = 'USELESS için bölge kenarı ve CHoCH basınca yeniden bak.'

    def text(packet):
        return hold_answer(rationale)

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    caplog.set_level(logging.INFO, logger='worker')
    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, market=market)
    result = run()
    assert result.action.value == 'hold'
    raw = store.db.execute("SELECT value FROM state WHERE key='price_action_holds'").fetchone()
    assert raw and 'USELESS_USDT' in raw[0]
    row = store.get('price_action_holds')[-1]
    assert row['status'] == 'armed' and row['rationale'] == rationale
    kinds = {item['kind'] for item in row['triggers']}
    assert 'choch' in kinds
    assert any(item['kind'] in ('block', 'flow', 'zone_edge') for item in row['triggers'])
    assert 'USELESS_USDT' in caplog.text and rationale in caplog.text
    assert all(f"{item['price']:.8g}" in caplog.text for item in row['triggers'])

    old_observed = market['observed_at']
    market['observed_at'] = old_observed + 30
    market['timeframes'][0]['recent_ohlcv'][-1][4] = 96.
    hit = next(item['price'] for item in row['triggers'] if item['kind'] in ('zone_edge', 'block', 'flow'))
    due = claim_printed(store, lambda symbol: hit)
    assert due and due[0]['symbol'] == 'USELESS_USDT' and due[0]['trigger_price'] == hit
    review = review_signal(due[0])

    def again(packet):
        prior = packet['account']['prior_hold']
        assert prior['rationale'] == rationale
        assert prior['old_market']['observed_at'] == old_observed
        assert packet['market']['observed_at'] == old_observed + 30
        assert prior['old_market']['observed_at'] != packet['market']['observed_at']
        return hold_answer('USELESS yeni tetik bekleniyor.')

    def again_vision(model, packet):
        assert packet['account']['prior_hold']['rationale'] == rationale
        assert model.calls[-1].get('images')
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    second = DecisionModel(again, again_vision)
    settings = Settings(_env_file=None, analysis_version='text', live_structure_setups_enabled=True,
                        live_price_plan_enabled=True)
    reviewed = ContextEvaluator(
        second, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        settings)
    outcome = reviewed.evaluate_candidates(
        [review], equity=11.58, risk_per_trade_pct=.03, target_leverage=20,
        live_account_context=dict(mode='LIVE', currency='USDT', runner_id='runner3',
                                  current_equity=11.58, available_usdt=11.58),
        live_positions={})
    assert outcome.action.value == 'hold'
    assert any(call.get('images') for call in second.calls)
    assert 'Answer briefly whether the trade is taken now.' in second.calls[0]['system']
    refreshed = store.get('price_action_holds')[-1]
    assert refreshed['status'] == 'armed'
    assert refreshed['rationale'] == 'USELESS yeni tetik bekleniyor.'
    assert refreshed['triggers']


def test_printed_hold_is_queued_ahead_of_an_idle_symbol(tmp_path):
    from trading.evaluation.hold_triggers import save_hold
    from .test_live_runner import prepare

    runner, _private, executor, control = prepare(tmp_path)
    control['runner_id'] = 'runner3'
    save_hold(runner.store, symbol='BTC_USDT', side='long', price=90,
              rationale='USELESS beklerken 100 basınca dön.',
              levels=[dict(kind='choch', price=100.)])
    seen = []

    def evaluate(candidate, snapshot, control):
        seen.append(candidate)
        return EvaluationResult(action=GraphAction.HOLD, hold_reason='hold', metadata={'provider_called': True})

    runner._evaluate = evaluate
    status = runner.cycle()
    assert status['reason'] == 'HOLD_LIVE_MODEL'
    assert seen[0].features['hold_review'] is True
    assert seen[0].features['prior_hold']['rationale'] == 'USELESS beklerken 100 basınca dön.'
    assert executor.calls == []
    assert runner.store.get('price_action_holds')[-1]['status'] == 'review_queued'


def test_veto_after_the_model_names_the_failed_check(tmp_path, monkeypatch):
    from inspect import getsource

    from trading.evaluation.service import ContextEvaluator
    from trading.runtime.runner import PaperRunner
    from trading.runtime.runner3_policy import entry_veto

    assert entry_veto(side='long', entry=100, stop=98, target=100.3, equity=100, free_margin=50,
                      fee=0.0006, slippage=0.0005, notional=10, leverage=5) == 'cost_adjusted_r'
    live = getsource(ContextEvaluator.evaluate_candidates)
    paper = getsource(PaperRunner._apply_evaluation)
    assert 'entry_veto' in live and 'entry_veto' in paper

    def text(packet):
        return ready_answer(packet, rationale='Model işlemi istedi, serbest marjin yok.')

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, _store = evaluator(tmp_path, monkeypatch, model)
    result = run(available_usdt=0)
    assert result.action.value == 'hold'
    assert result.hold_reason == 'free_margin'
    assert result.metadata['veto'] == 'free_margin'
    assert result.proposal is None


def test_summary_packet_sends_closed_choch_and_the_fast_path_reads_it(tmp_path, monkeypatch):
    from trading.evaluation.context import compact_decision_packet
    from .test_provider_pool import pool, settings

    market = rebase_market('LAB_USDT')
    frame = next(view for view in market['timeframes'] if view['timeframe'] == '4h')
    first = frame['recent_ohlcv'][0][0]
    pad = [[first - 14400 * (500 - index), 1, 424242.5, 0.5, 1.1, 3] for index in range(500)]
    frame['recent_ohlcv'] = pad + frame['recent_ohlcv']

    def text(packet):
        options = packet['account']['setup_selection']['options']
        assert options
        assert all(row['state'] == 'ready' for row in options)
        assert all((row['evidence'].get('zone') or {}).get('kind') in ('block', 'flow') for row in options)
        assert all((row['evidence'].get('shift') or {}).get('kind') in ('choch', 'mss') for row in options)
        assert all(len(item['recent_ohlcv']) <= 8 for item in packet['market']['timeframes'])
        assert packet['market']['summary']['kind'] == 'closed_choch_at_block_or_flow'
        return ready_answer(packet, rationale='Özet paketteki kapalı CHoCH ile işlem.')

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, _store = evaluator(tmp_path, monkeypatch, model, market=market)
    result = run()
    assert result.action.value == 'trade'
    user = model.calls[0]['user']
    assert '424242.5' not in user
    compact = json.loads(compact_decision_packet(user))
    assert compact['setups']
    assert any(row.get('closed_choch_price') for row in compact['setups'])
    assert any(row.get('stop') for row in compact['setups'])
    assert any((row.get('block_or_flow') or {}).get('kind') in ('block', 'flow') for row in compact['setups'])
    assert any(row.get('next_target') for row in compact['setups'])
    assert compact['atr'] and isinstance(compact['swings'], list)
    assert '424242.5' not in json.dumps(compact)
    assert 'recent_ohlcv' not in json.dumps(compact)
    routed, _routed_store = pool(tmp_path / 'pool', settings())
    _system, outbound = routed._prepared(routed._slots[0], model.calls[0]['system'], user, 700, None)
    assert json.loads(outbound)['context_policy'] == 'pa-context-v4-compact'
    assert '424242.5' not in outbound

    other = rebase_market('ETH_USDT')
    other_model = DecisionModel(text, vision)
    other_run, _other_store = evaluator(tmp_path / 'other', monkeypatch, other_model, market=other)
    assert other_run().action.value == 'trade'
    assert other_model.calls[0]['system'] == model.calls[0]['system']
    assert 'LAB_USDT' not in model.calls[0]['system']
    assert 'ETH_USDT' not in model.calls[0]['system']


def test_printed_hold_reenters_at_the_current_price(tmp_path, monkeypatch):
    from trading.evaluation.hold_triggers import claim_printed, review_signal
    from trading.evaluation.service import ContextEvaluator
    from trading.quant.structure_setups import build_htf_zone_setups

    market = rebase_market('USELESS_USDT')
    ready = next(row for row in build_htf_zone_setups(market, current_price=100., price_step=.01)
                 if row['family'] == 'pullback' and row['state'] == 'ready')
    current = round(ready['entry_reference'] + (-0.01 if ready['side'] == 'long' else 0.01), 2)
    if ready['side'] == 'long':
        assert ready['stop_loss_price'] < current < ready['take_profit_price']
    else:
        assert ready['take_profit_price'] < current < ready['stop_loss_price']

    def text(packet):
        return hold_answer('USELESS ilk bekleme, tetik basınca yeniden bak.')

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model, market=market)
    assert run().action.value == 'hold'
    row = store.get('price_action_holds')[-1]
    row['triggers'] = [dict(kind='choch', price=current)]
    row['latched'] = []
    store.put('price_action_holds', [row])
    due = claim_printed(store, lambda symbol: current)
    review = review_signal(due[0])
    assert review.entry_price == current

    def again(packet):
        assert packet['account']['prior_hold']['rationale'].startswith('USELESS ilk bekleme')
        answer = ready_answer(packet, rationale='USELESS güncel fiyattan girilir.')
        answer['price_plan']['entry_price'] = current + 5
        return answer

    second = DecisionModel(again, vision)
    settings = Settings(_env_file=None, analysis_version='text', live_structure_setups_enabled=True,
                        live_price_plan_enabled=True)
    reviewed = ContextEvaluator(
        second, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        settings)
    outcome = reviewed.evaluate_candidates(
        [review], equity=1000, risk_per_trade_pct=.03, target_leverage=20,
        live_account_context=dict(mode='LIVE', currency='USDT', runner_id='runner3',
                                  current_equity=1000, available_usdt=1000),
        live_positions={})
    assert outcome.action.value == 'trade'
    chosen = outcome.metadata['setup_selection']
    assert abs(outcome.proposal.entry_price - current) < 1e-9
    assert abs(outcome.proposal.stop_loss.price - chosen['stop_loss_price']) < 1e-9
    assert abs(outcome.proposal.take_profit_price - chosen['take_profit_price']) < 1e-9
    assert outcome.proposal.entry_price != current + 5
    level = chosen['opposing_target_levels'][0]
    assert chosen['target_reference']['open_time'] == level['open_time']


def test_short_margin_reports_a_swap_and_does_not_close(tmp_path, monkeypatch):
    briefs = [dict(symbol='ETH_USDT', side='long', entry=100, stop=90, target=101,
                   unrealized_pnl=0, thesis='eski tez', leverage=2, mark=100)]

    def text(packet):
        report = packet['account']['swap_candidate']
        assert report['close'] is False
        assert report['waiting']['expected_r'] > report['open_positions'][0]['expected_r']
        assert report['open_positions'][0]['symbol'] == 'ETH_USDT'
        return ready_answer(packet, rationale='Yeni kurulum daha yüksek R taşır, açık pozisyon durur.')

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model)
    result = run(open_position_briefs=briefs, available_usdt=1)
    assert result.action.value == 'trade'
    assert result.metadata.get('release_symbol') is None
    assert result.metadata['swap_candidate']['close'] is False
    assert store.get('swap_candidate:BTC_USDT')['close'] is False
