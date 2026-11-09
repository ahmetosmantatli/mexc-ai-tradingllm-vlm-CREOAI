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


def evaluator(tmp_path, monkeypatch, model, market=None):
    market = rebase_market() if market is None else market
    monkeypatch.setattr('trading.analysis.build_bundle', lambda *args, **kwargs:
                        SimpleNamespace(fingerprint='lane', to_context=lambda **kw: deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle', lambda *args, **kwargs:
                        [SimpleNamespace(path=tmp_path / 'chart.png')])
    settings = Settings(_env_file=None, analysis_version='text', live_structure_setups_enabled=True,
                        live_price_plan_enabled=True)
    store = RuntimeStore(str(tmp_path / 'state'))
    reviewed = ContextEvaluator(model, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        settings)
    seed = signal(Side.SHORT)
    seed.entry_price = 100.
    seed.features.update(htf_zone_seed=True, source_closed_at=time.time() - 1, dedup_key='seed')

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
        model.gate.wait(2)
        model.vision_answer = ready_answer(packet, rationale='Grafik modeli geç kaldı.')

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model)
    try:
        result = run()
    finally:
        model.gate.set()
    assert result.action.value == 'trade'
    assert result.metadata['decision_ai']['review_mode'] == 'text'
    assert result.metadata['decision_ai']['model'] == 'text-model'
    assert result.metadata['rationale'].startswith('Metin modeli')
    assert '4h zone then 15m' in model.calls[0]['system']
    assert '1h zone then 5m' in model.calls[0]['system']
    assert 'Touch alone is not an entry' in model.calls[0]['system']
    trace = store.get('analysis_trace')
    assert trace['decision_mode'] == 'text'
    assert trace['attempts'][-1] == {'review_mode': 'vlm', 'status': 'timeout', 'generation_requests': 0}


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
    def text(packet):
        allocation = packet['account']['capital_allocation']
        assert allocation['small_account'] is True
        assert allocation['open_positions'][0]['symbol'] == 'ETH_USDT'
        assert allocation['open_positions'][0]['thesis'].startswith('Eski')
        assert allocation['queued_confirmed_plans'][0]['symbol'] == 'SOL_USDT'
        return ready_answer(packet, release_symbol='ETH_USDT',
                            rationale='ETH tezi zayıf; marjini güçlü plana bırak.')

    def vision(model, packet):
        model.vision_answer = ready_answer(
            packet, release_symbol='ETH_USDT',
            rationale='Grafik de ETH kapanışını ve bu planı seçiyor.')
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'success'}]}

    model = DecisionModel(text, vision)
    run, store = evaluator(tmp_path, monkeypatch, model)
    result = run(open_position_briefs=[dict(symbol='ETH_USDT', side='long', unrealized_pnl=-1.2,
                                            thesis='Eski tez zayıf kaldı.',
                                            remaining_room={'to_target_price': 2, 'to_target_r': .4},
                                            leverage=5, entry=100, stop=98, target=104)],
                 queued_confirmed_plans=[dict(symbol='BTC_USDT', capital_rank=1, grade='standard'),
                                         dict(symbol='SOL_USDT', capital_rank=4, grade='strong',
                                              evidence_kind='htf_zone_mss')])
    assert result.action.value == 'trade'
    assert result.metadata['release_symbol'] == 'ETH_USDT'
    assert result.metadata['decision_ai']['review_mode'] == 'vlm'
    assert store.get('local_entry_limits', []) == []


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


def test_unconfirmed_close_does_not_open_the_replacement(tmp_path, monkeypatch):
    from trading.runtime.live_runner import LiveRunner
    from .test_live_runner import candidate, prepare, result

    runner, _, executor, control = prepare(tmp_path)
    seed = candidate()
    runner.candidate_queue().refresh([seed])
    trade = result(runner, seed)
    trade.metadata['release_symbol'] = 'ETH_USDT'
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
    monkeypatch.setattr(runner, '_release_confirmed', lambda symbol: True)
    assert runner._apply(seed, trade, control)['reason'] == 'LIVE_PROTECTED'
    assert [row[0] for row in executor.calls] == ['authorize', 'execute']


def test_zone_alert_names_touch_and_confirmation_without_calling_either_an_order():
    from trading.control.activity import zone_alert_text

    touch = zone_alert_text(dict(symbol='BTC_USDT', context_timeframe='4h', trigger_timeframe='15m',
                                 evidence=dict(stage='awaiting_confirmation')))
    confirmed = zone_alert_text(dict(symbol='ETH_USDT', context_timeframe='1h', trigger_timeframe='5m',
                                     evidence=dict(stage='confirmed')))
    assert 'temas tek başına emir değil' in touch
    assert 'emir yok' in confirmed
    assert zone_alert_text(dict(evidence=dict(stage='awaiting_touch'))) is None


def test_confirmed_plans_rank_by_evidence_and_payoff():
    from trading.runtime.runner3_policy import rank_confirmed_plans

    ranked = rank_confirmed_plans([
        dict(symbol='BBB', capital_rank=9, grade='standard'),
        dict(symbol='AAA', capital_rank=1, grade='strong', evidence_kind='htf_zone_mss'),
        dict(symbol='CCC', capital_rank=3, grade='standard'),
    ])
    assert [row['symbol'] for row in ranked] == ['AAA', 'BBB', 'CCC']
