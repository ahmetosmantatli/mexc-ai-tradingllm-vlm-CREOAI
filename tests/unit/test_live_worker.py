"""Live runner against a fake exchange. No test in this module opens a socket."""
from copy import deepcopy
from time import time
from types import SimpleNamespace

import pytest

from trading.config import BrokerMode, Settings
from trading.exchange.contracts import ContractRules
from trading.models.domain import Side, StopLossSpec, TradeProposal
from trading.runtime.live_permission import LivePermission, LivePermissionError
from trading.runtime.live_runner import LiveRunner
from trading.runtime.store import RuntimeStore
from .test_live_account import Client


class Fake(Client):
    writes_enabled = True

    def __init__(self):
        super().__init__()
        self.submits = []
        self.closes = []
        self.protection_writes = []
        self.lookups = []
        self.history = []
        self.protections = []
        self.order_row = None
        self.timeout = False
        self.partial = False

    def open_positions(self, symbol=None):
        self.calls.append('positions')
        rows = deepcopy(self.positions)
        return [row for row in rows if symbol is None or row.get('symbol') == symbol]

    def submit_market(self, **kw):
        self.submits.append(kw)
        filled = 0.5 if self.partial else kw['contracts']
        self.order_row = dict(externalOid=kw['external_id'], symbol=kw['symbol'], side=1, openType=1,
            positionMode=1, vol=kw['contracts'], dealVol=filled, state=2 if self.partial else 3, positionId=42)
        self.positions = [dict(positionId=42, symbol=kw['symbol'], positionType=1, holdVol=filled, state=1,
            openType=1, autoAddIm=0, leverage=5, holdAvgPrice=100, im=20, unRealizedPnl=0, liquidatePrice=80)]
        self.protections = [] if self.partial else [self.pair(kw)]
        if self.timeout:
            raise RuntimeError('timeout')
        return {'orderId': 'o1'}

    @staticmethod
    def pair(kw):
        return dict(positionId=42, state=1, isFinished=0, lossTrend=2, profitTrend=2, stopLossType=0,
            takeProfitType=0, stopLossReverse=2, takeProfitReverse=2, profitLossVolType='SAME', vol=kw['contracts'],
            stopLossPrice=kw['stop_loss'], takeProfitPrice=kw['take_profit'])

    def order_by_external_id(self, symbol, external_id):
        self.lookups.append(external_id)
        if str(external_id).startswith('close-'):
            return dict(externalOid=external_id, symbol=symbol, side=4, state=3, dealVol=1)
        return deepcopy(self.order_row)

    def protection_orders(self, symbol=None, page_num=1, page_size=100):
        return deepcopy(self.protections)

    def place_protection(self, **kw):
        self.protection_writes.append(kw)
        self.protections = [self.pair(kw)]

    def close_position(self, **kw):
        self.closes.append(kw)
        self.positions = []
        return {'orderId': 'c1'}

    def history_positions(self, symbol=None, **kwargs):
        return [row for row in self.history if symbol is None or row.get('symbol') == symbol]


def settings(**overrides):
    values = dict(_env_file=None, broker_mode='live', live_trading_enabled=True, risk_per_trade_pct=.03,
        telegram_bot_token='', telegram_chat_id='', paper_seed_usdt=700, starting_capital_try=700,
        mexc_api_key='', mexc_api_secret='', live_operator_ids='')
    values.update(overrides)
    return Settings(**values)


def proposal():
    return TradeProposal(symbol='BTC_USDT', side=Side.LONG, leverage=5, notional=100, entry_price=100,
        stop_loss=StopLossSpec(price=98), take_profit_price=104, max_loss_if_stopped=3,
        expected_edge=.8, fee_free_pair=False, rationale='Live path test proposal')


def build(tmp_path, *, command_source=None, store=None, operator_ids=('11',)):
    private = Fake()
    public = SimpleNamespace(
        get_contract_rules=lambda symbol: ContractRules(1, 1, 1, 1000, 10, .005, .01, 1000),
        get_ticker=lambda symbol: SimpleNamespace(bid1=100., ask1=100., raw={'timestamp': time()*1000}))
    cfg = settings()
    runner = LiveRunner(cfg, store=store or RuntimeStore(str(tmp_path/'live.sqlite3')), private=private, public=public,
        account_id='ahmet', binding_id='connection1', operator_ids=operator_ids, command_source=command_source)
    return runner, private, cfg


class Inbox:
    """Fake Telegram getUpdates. No socket and no token in the payload."""

    def __init__(self, batches):
        self.batches = [list(batch) for batch in batches]
        self.sent = []

    def call(self, method, **kwargs):
        assert method == 'getUpdates'
        if not self.batches:
            return []
        return self.batches.pop(0)

    def send(self, text):
        self.sent.append(text)


def telegram_update(text, user_id, *, chat_id=900, update_id=1):
    sender = {} if user_id is None else {'id': user_id, 'is_bot': False}
    return {'update_id': update_id, 'message': {
        'message_id': update_id, 'from': sender, 'chat': {'id': chat_id, 'type': 'private'}, 'text': text}}


def attach(runner, batches, chat_id='900'):
    runner.cfg.telegram_bot_token = 'test-token-not-real'
    runner.cfg.telegram_chat_id = str(chat_id)
    inbox = Inbox(batches)
    runner._telegram = inbox
    return inbox


def test_compose_reads_live_gates_from_env_and_publishes_no_extra_ports():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    text = (root / 'infrastructure' / 'docker-compose.yml').read_text()
    image = (root / 'Dockerfile').read_text()
    assignments = []
    ports = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith(('LIVE_TRADING_ENABLED:', 'BROKER_MODE:')):
            assignments.append(stripped)
        if stripped.startswith('- "') or stripped.startswith("- '"):
            ports.append(stripped)
    assert assignments == []
    assert ports == ['- "127.0.0.1:8080:8080"']
    assert text.count('env_file:') == 2 and '../.env' in text
    assert '["python", "-m", "apps.worker.main"]' in text
    assert 'LIVE_TRADING_ENABLED=false' in image and 'BROKER_MODE=paper' in image


def test_defaults_stay_paper_and_live_runner_refuses_them(tmp_path):
    from apps.worker.main import worker_mode
    plain = Settings(_env_file=None)
    assert plain.live_trading_enabled is False
    assert plain.broker_mode == BrokerMode.PAPER
    assert plain.live_operator_ids == ''
    assert worker_mode(plain) == 'paper'
    assert worker_mode(settings(broker_mode='live', live_trading_enabled=False)) == 'refused'
    assert worker_mode(settings(broker_mode='paper', live_trading_enabled=True)) == 'refused'
    assert worker_mode(settings()) == 'live'
    public = settings(live_operator_ids='11').public_dict()
    assert public['live_operator_configured'] is True
    assert '11' not in public.values()
    with pytest.raises(ValueError, match='BROKER_MODE=live'):
        LiveRunner(Settings(_env_file=None), store=RuntimeStore(str(tmp_path/'paper.sqlite3')), private=Fake(),
            public=SimpleNamespace(), operator_ids=('11',))


def test_first_live_boot_is_disarmed_and_mode_cycle_clears_the_arm(tmp_path):
    store = RuntimeStore(str(tmp_path/'permission.sqlite3'))
    permission = LivePermission(store)
    first = permission.sync(broker_mode='live', live_trading_enabled=True)
    assert first['armed'] is False and first['running'] is False and first['generation'] == 1
    permission.arm('11', allowed=('11',))
    assert permission.view()['armed'] is True and permission.view()['running'] is True
    permission.sync(broker_mode='paper', live_trading_enabled=False)
    assert permission.view()['armed'] is False and permission.view()['running'] is False
    restored = permission.sync(broker_mode='live', live_trading_enabled=True)
    assert restored['armed'] is False and restored['running'] is False and restored['generation'] == 3
    with pytest.raises(LivePermissionError, match='operator_not_allowed'):
        permission.arm('22', allowed=('11',))

    runner, private, cfg = build(tmp_path)
    runner.store.put('account:ahmet:control', dict(enabled=True, stopped=False))
    blocked = runner.consider(proposal())
    assert blocked['reason'] == 'entries_blocked' and private.submits == []
    runner.arm('11')
    assert runner.entries_open() is True
    runner.permission.sync(broker_mode='paper', live_trading_enabled=False)
    runner.permission.sync(broker_mode='live', live_trading_enabled=True)
    assert runner.entries_open() is False
    runner.executor.stage = lambda *args, **kwargs: pytest.fail('disarmed proposal reached the executor')
    assert runner.consider(proposal())['reason'] == 'entries_blocked'
    assert private.submits == []
    assert cfg.paper_seed_usdt == 700


def test_equity_margin_and_positions_come_from_the_exchange_not_paper_cash(tmp_path):
    runner, private, _cfg = build(tmp_path)
    runner.store.put('paper', dict(equity=700, cash=700))
    private.assets[0].update(equity='11.5854', availableBalance='10', positionMargin='1.5854', unrealized='0',
        cashBalance='11.5854')
    private.positions = [dict(positionId=7, symbol='BTC_USDT', positionType=1, openType=1, state=1, holdVol=2,
        leverage=5, holdAvgPrice=100, im=20, unRealizedPnl=0.5, liquidatePrice=80)]
    status = runner.cycle()
    assert status['source'] == 'mexc_exchange'
    assert status['equity'] == 11.5854
    assert status['available'] == 10
    assert status['position_margin'] == 1.5854
    assert status['positions'][0]['position_id'] == '7'
    assert status['positions'][0]['contracts'] == 2
    assert 'paper' not in status
    assert runner.store.get('paper')['equity'] == 700
    assert not hasattr(runner, 'ledger')
    assert status['reason'] == 'LIVE_DISARMED'
    assert status['armed'] is False


def test_stop_blocks_new_entries_but_exchange_close_fees_and_funding_are_booked(tmp_path):
    pending = ['/stop']
    runner, private, _cfg = build(tmp_path, command_source=lambda: [pending.pop(0)] if pending else [])
    runner.arm('11')
    opened = runner.consider(proposal())
    assert opened['accepted'] is True
    assert opened['result']['status'] == 'protected'
    assert len(private.submits) == 1 and private.closes == []
    assert opened['notification'].startswith('Exchange confirmed entry')
    assert 'PAPER' not in opened['notification'] and 'sanal' not in opened['notification'].lower()
    assert '700' not in opened['notification']
    private.positions = []
    private.history = [dict(positionId=42, symbol='BTC_USDT', state=3, closeVol=1, fee=-0.009, holdFee=-0.02)]
    status = runner.cycle()
    intent = opened['result']['intent_id']
    closed = runner.executor.state(intent)
    assert closed['status'] == 'closed'
    assert closed['close_reason'] == 'exchange_confirmed'
    assert closed['exchange_costs']['fee'] == pytest.approx(-0.009)
    assert closed['exchange_costs']['funding'] == pytest.approx(-0.02)
    booked = runner.store.get('live:costs:'+intent)
    assert booked['source'] == 'exchange'
    assert booked['fee'] == pytest.approx(-0.009) and booked['funding'] == pytest.approx(-0.02)
    assert len(private.submits) == 1 and private.closes == []
    assert status['reason'] == 'HOLD_ENTRIES_STOPPED'
    note = status['notifications'][0]
    assert note.startswith('Exchange confirmed close')
    assert 'Exchange fee -0.009' in note and 'Exchange funding -0.02' in note
    assert 'PAPER' not in note and 'sanal' not in note.lower()
    assert runner.consider(proposal())['reason'] == 'entries_blocked'
    assert not runner.store.get('live:halt')
    runner.resume_entries()
    second = runner.consider(proposal())
    assert second['accepted'] is True
    assert len(private.submits) == 2
    assert private.submits[1]['external_id'] != private.submits[0]['external_id']


def test_partial_fill_does_not_latch_the_account_or_send_a_second_entry(tmp_path):
    runner, private, _cfg = build(tmp_path)
    runner.arm('11')
    private.partial = True
    opened = runner.consider(proposal())
    assert opened['result']['halt_reason'] == 'entry_fill_not_fully_confirmed'
    assert opened['result']['status'] == 'partial_protected'
    assert opened['result']['filled_contracts'] == 0.5
    assert len(private.submits) == 1
    assert len(private.protection_writes) == 1
    assert private.protection_writes[0]['contracts'] == 0.5
    assert not runner.store.get('live:halt')
    assert 'no second entry' in opened['notification']
    private.partial = False
    private.order_row.update(dealVol=1, state=3)
    private.positions[0]['holdVol'] = 1
    runner.cycle()
    assert runner.executor.state(opened['result']['intent_id'])['status'] == 'protected'
    assert len(private.submits) == 1
    assert not runner.store.get('live:halt')


def test_restart_reconciles_the_same_external_id_without_a_second_open(tmp_path):
    path = tmp_path/'restart.sqlite3'
    runner, private, _cfg = build(tmp_path, store=RuntimeStore(str(path)))
    runner.arm('11')
    private.timeout = True
    idea = proposal()
    first = runner.consider(idea)
    external = first['result']['external_id']
    assert external.startswith('creo-')
    assert first['result']['halt_reason'] == 'entry_write_outcome_unknown'
    assert len(private.submits) == 1
    assert private.submits[0]['external_id'] == external
    runner.store.db.close()
    restarted, _other, _cfg = build(tmp_path, store=RuntimeStore(str(path)))
    restarted.private = private
    restarted.executor.private = private
    restarted.stop_new_entries()
    status = restarted.cycle()
    assert len(private.submits) == 1
    assert external in private.lookups
    assert restarted.executor.state(first['result']['intent_id'])['status'] == 'protected'
    assert restarted.executor.state(first['result']['intent_id'])['external_id'] == external
    assert status['entries_open'] is False
    assert restarted.consider(idea)['reason'] == 'entries_blocked'
    restarted.resume_entries()
    again = restarted.consider(idea)
    assert again['result']['status'] == 'protected'
    assert again['result']['external_id'] == external
    # The unknown write still latches new entries. It must not open a second order.
    blocked = restarted.consider(proposal())
    assert blocked['reason'] == 'live_halt_latched'
    assert len(private.submits) == 1


def test_telegram_arm_opens_entries_and_stop_still_blocks_them(tmp_path):
    runner, private, _cfg = build(tmp_path)
    runner.store.put('account:ahmet:control', dict(enabled=False, stopped=True))
    inbox = attach(runner, [
        [telegram_update('/arm@LiveBot', 11, update_id=1)],
        [telegram_update('/stop', 11, update_id=2)],
        [telegram_update('/start', 11, update_id=3)],
    ])
    armed = runner.cycle()
    assert armed['reason'] == 'LIVE_ENTRIES_OPEN'
    assert armed['armed'] is True and armed['running'] is True and armed['entries_open'] is True
    assert runner.last_arm_result == 'armed'
    assert runner.permission.view()['armed_by'] == '11'
    assert inbox.sent[-1] == 'Live generation armed. New entries can run. /stop still blocks new entries.'
    assert 'test-token' not in inbox.sent[-1]
    opened = runner.consider(proposal())
    assert opened['accepted'] is True and len(private.submits) == 1
    stopped = runner.cycle()
    assert stopped['reason'] == 'HOLD_ENTRIES_STOPPED'
    assert stopped['armed'] is True and stopped['running'] is False and stopped['entries_open'] is False
    assert runner.consider(proposal())['reason'] == 'entries_blocked'
    assert len(private.submits) == 1
    resumed = runner.cycle()
    assert resumed['reason'] == 'LIVE_ENTRIES_OPEN'
    assert runner.last_arm_result == 'armed'


def test_telegram_arm_rejects_unauthorized_operators(tmp_path):
    runner, private, _cfg = build(tmp_path)
    inbox = attach(runner, [
        [telegram_update('/arm', 22, update_id=1)],
        [telegram_update('/arm 11', 22, update_id=2)],
        [telegram_update('/arm', 11, chat_id=111, update_id=3)],
        [telegram_update('/arm', None, update_id=4)],
    ])
    refused = runner.cycle()
    assert refused['reason'] == 'LIVE_DISARMED' and refused['armed'] is False
    assert runner.last_arm_result == 'operator_not_allowed'
    assert inbox.sent == ['Arm refused. This Telegram user cannot arm live entries.']
    assert private.submits == []
    assert runner.cycle()['armed'] is False
    assert runner.cycle()['armed'] is False and len(inbox.sent) == 1
    missing = runner.cycle()
    assert missing['armed'] is False and runner.last_arm_result == 'operator_not_allowed'
    assert runner.consider(proposal())['reason'] == 'entries_blocked'
    assert private.submits == []

    nobody_dir = tmp_path/'nobody'
    nobody_dir.mkdir()
    nobody, other, _cfg = build(nobody_dir, operator_ids=())
    assert nobody.operator_ids == ()
    attach(nobody, [[telegram_update('/arm', 11, update_id=1)]])
    blocked = nobody.cycle()
    assert blocked['reason'] == 'LIVE_DISARMED' and blocked['armed'] is False
    assert nobody.last_arm_result == 'operator_not_allowed'
    assert other.submits == []


def test_first_boot_and_live_paper_live_stay_disarmed_until_telegram_arm(tmp_path):
    runner, private, _cfg = build(tmp_path)
    attach(runner, [
        [telegram_update('/start', 11, update_id=1)],
        [telegram_update('/arm', 11, update_id=2)],
        [telegram_update('/arm', 11, update_id=3)],
        [],
        [telegram_update('/arm', 11, update_id=4)],
    ])
    first = runner.cycle()
    assert first['reason'] == 'LIVE_DISARMED' and first['armed'] is False
    assert runner.permission.view()['generation'] == 1
    assert runner.last_arm_result is None
    assert runner.consider(proposal())['reason'] == 'entries_blocked'
    assert private.submits == []

    armed = runner.cycle()
    assert armed['reason'] == 'LIVE_ENTRIES_OPEN' and armed['armed'] is True
    assert runner.permission.view()['generation'] == 1

    runner.cfg.broker_mode = BrokerMode.PAPER
    runner.cfg.live_trading_enabled = False
    paper = runner.cycle()
    assert paper['armed'] is False and paper['running'] is False
    assert runner.last_arm_result == 'live_gates_required'
    assert runner.permission.view()['generation'] == 2
    assert runner.entries_open() is False and private.submits == []

    runner.cfg.broker_mode = BrokerMode.LIVE
    runner.cfg.live_trading_enabled = True
    restored = runner.cycle()
    assert restored['reason'] == 'LIVE_DISARMED' and restored['armed'] is False
    assert runner.permission.view()['generation'] == 3
    assert runner.permission.view()['armed_by'] is None
    assert runner.consider(proposal())['reason'] == 'entries_blocked'
    assert private.submits == []

    again = runner.cycle()
    assert again['reason'] == 'LIVE_ENTRIES_OPEN' and again['armed'] is True
    assert runner.permission.view()['armed_generation'] == 3
    opened = runner.consider(proposal())
    assert opened['accepted'] is True and len(private.submits) == 1
