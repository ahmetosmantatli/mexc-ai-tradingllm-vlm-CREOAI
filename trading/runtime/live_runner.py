"""Live account runner. Separate from PaperRunner and from the paper ledger.

Constructed only when BROKER_MODE=live and LIVE_TRADING_ENABLED=true.
The first boot of a live generation is disarmed: equity, margin, positions,
and already-sent orders are still reconciled, but a model TradeProposal is
not handed to the executor until an allowlisted operator arms it.

Arm is the Arm button or /arm. The sender is message.from.id (callback
from.id for the button). Durdur and /stop block new entries only.
/start only resumes entries for a generation that is already armed.
Reconciliation and native protection of an order that was already sent keep
running. live → paper → live clears the arm until a new arm.

This process is the only getUpdates poller while the live gates are on.
Equity, available margin, and positions come from the authenticated exchange
snapshot. Paper cash and STARTING_CAPITAL_TRY are not read.
"""
import hashlib
import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

from trading.brokers.live.execution import ApprovedLiveExecutor, LiveExecutionError, LiveExecutionGates
from trading.config import BrokerMode
from trading.exchange.mexc_private import MexcPrivateClient
from trading.exchange.mexc_public import MexcPublicClient
from trading.models.domain import TradeProposal
from trading.risk.engine import RiskEngine
from trading.runtime.live_account import LiveAccountError
from trading.runtime.live_permission import LivePermission, LivePermissionError
from trading.runtime.store import RuntimeStore


class LiveRunner:
    def __init__(self, settings, store=None, private=None, public=None, *, account_id='primary',
                 binding_id=None, operator_ids=None, command_source=None):
        if settings.broker_mode != BrokerMode.LIVE or settings.live_trading_enabled is not True:
            raise ValueError('live runner requires BROKER_MODE=live and LIVE_TRADING_ENABLED=true')
        self.cfg = settings
        self.store = store or RuntimeStore(settings.runtime_db_path)
        self.account_id = account_id
        if binding_id is None:
            material = settings.mexc_api_key or 'unconfigured'
            binding_id = hashlib.sha256(material.encode()).hexdigest()[:32]
        self.binding_id = binding_id
        if operator_ids is None:
            operator_ids = tuple(part.strip() for part in (settings.live_operator_ids or '').split(',') if part.strip())
        self.operator_ids = tuple(str(item) for item in operator_ids)
        self.command_source = command_source
        self.private = private or MexcPrivateClient(settings.mexc_api_key, settings.mexc_api_secret, writes_enabled=True)
        self.public = public or MexcPublicClient()
        self.risk = RiskEngine(settings=settings)
        self.permission = LivePermission(self.store)
        self.permission.sync(broker_mode=settings.broker_mode, live_trading_enabled=settings.live_trading_enabled is True)
        self.executor = ApprovedLiveExecutor(
            LiveExecutionGates(True, 'live', self.operator_ids, True), self.store, self.private, self.public, self.risk,
            account_id=account_id, binding_id=self.binding_id, can_execute=self.entries_open)
        self.executor.entries_armed = self.entries_open
        self._telegram = None
        self.last_arm_result = None

    CONTROLS = {'inline_keyboard': [[
        {'text': 'Arm', 'callback_data': 'live:arm'},
        {'text': 'Durdur', 'callback_data': 'live:stop'},
    ]]}

    def _control_key(self):
        return f'account:{self.account_id}:control'

    def entries_open(self):
        if self.cfg.live_trading_enabled is not True or self.cfg.broker_mode != BrokerMode.LIVE:
            return False
        view = self.permission.view()
        if not (view.get('armed') and view.get('running') and view.get('effective_live') is True):
            return False
        control = self.store.get(self._control_key())
        if isinstance(control, dict) and (control.get('stopped') or control.get('enabled') is False):
            return False
        return True

    def arm(self, operator_id):
        if self.cfg.live_trading_enabled is not True or self.cfg.broker_mode != BrokerMode.LIVE:
            raise LivePermissionError('live_gates_required')
        return self.permission.arm(str(operator_id), allowed=self.operator_ids)

    def stop_new_entries(self):
        self.permission.stop_entries()
        control = dict(self.store.get(self._control_key()) or {})
        control.update(enabled=False, stopped=True)
        self.store.put(self._control_key(), control)
        return self.permission.view()

    def resume_entries(self):
        view = self.permission.resume_entries()
        if view.get('armed') and view.get('running'):
            control = dict(self.store.get(self._control_key()) or {})
            control.update(enabled=True, stopped=False)
            self.store.put(self._control_key(), control)
        return self.permission.view()

    _COMMANDS = frozenset({'/stop', '/pause', '/start', '/arm'})
    _BUTTONS = {'live:arm': '/arm', 'live:stop': '/stop'}

    @staticmethod
    def _command_text(text):
        return str(text or '').split('@', 1)[0].strip().lower()

    def _commands(self):
        if self.command_source is not None:
            return list(self.command_source())
        if not (self.cfg.telegram_bot_token and self.cfg.telegram_chat_id):
            return []
        try:
            from trading.runtime.telegram import Telegram
            if self._telegram is None:
                self._telegram = Telegram(self.cfg, self.store)
            offset = self.store.get('live_telegram_offset', 0)
            updates = self._telegram.call(
                'getUpdates', offset=offset, timeout=0, allowed_updates=['message', 'callback_query'])
            commands = []
            for update in updates:
                parsed = self._parse_update(update)
                if parsed:
                    commands.append(parsed)
                if 'update_id' in update:
                    self.store.put('live_telegram_offset', update['update_id']+1)
            return commands
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status == 409:
                logger.warning('live telegram poll conflict; another process is calling getUpdates')
            else:
                logger.warning('live telegram poll failed (%s)', type(exc).__name__)
            return []

    def _parse_update(self, update):
        callback = update.get('callback_query')
        if isinstance(callback, dict):
            message = callback.get('message') or {}
            chat = message.get('chat') or {}
            sender = callback.get('from') or {}
            callback_id = callback.get('id')
            if str(chat.get('id')) != str(self.cfg.telegram_chat_id):
                self._answer_callback(callback_id, 'Yetkisiz.')
                return None
            command = self._BUTTONS.get(str(callback.get('data') or ''))
            if command is None:
                self._answer_callback(callback_id, 'Silahsız. Bilinmeyen düğme.')
                return None
            # Button actor is callback_query.from.id. message.from is the bot.
            return {'command': command, 'operator_id': sender.get('id'), 'callback_id': callback_id}
        message = update.get('message') or {}
        chat = message.get('chat') or {}
        if str(chat.get('id')) != str(self.cfg.telegram_chat_id):
            return None
        text = self._command_text(message.get('text'))
        if text not in self._COMMANDS:
            return None
        sender = message.get('from') or {}
        return {'command': text, 'operator_id': sender.get('id')}

    def _apply_commands(self):
        for raw in self._commands():
            callback_id = None
            if isinstance(raw, str):
                command, operator_id = self._command_text(raw), None
            elif isinstance(raw, dict):
                command = self._command_text(raw.get('command'))
                operator_id = raw.get('operator_id')
                callback_id = raw.get('callback_id')
            else:
                continue
            if command in ('/stop', '/pause', 'stop', 'pause'):
                self.stop_new_entries()
                reply, result = self._stopped_text(), 'entries_stopped'
            elif command in ('/start', 'start'):
                self.resume_entries()
                reply, result = self._state_text(), 'resume'
            elif command in ('/arm', 'arm'):
                reply = self._arm_command(operator_id)
                result = self.last_arm_result
            else:
                continue
            logger.info('live telegram command=%s result=%s', command, result)
            if reply:
                self._control_reply(reply, callback_id)

    def _arm_command(self, operator_id):
        """Arm only from the Telegram sender id. The message text is not an identity."""
        if operator_id is None or str(operator_id).strip() == '':
            self.last_arm_result = 'operator_not_allowed'
            return self._arm_text('operator_not_allowed')
        try:
            self.arm(str(operator_id).strip())
        except LivePermissionError as exc:
            self.last_arm_result = str(exc)
            return self._arm_text(str(exc))
        control = dict(self.store.get(self._control_key()) or {})
        control.update(enabled=True, stopped=False)
        self.store.put(self._control_key(), control)
        self.last_arm_result = 'armed'
        return self._arm_text('armed')

    @staticmethod
    def _arm_text(code):
        return {
            'armed': 'Silahlı. İzinli operatör, canlı kapılar açık.',
            'operator_not_allowed': 'Silahsız. Bu Telegram kimliği izinli değil.',
            'live_gates_required': 'Silahsız. BROKER_MODE=live ve LIVE_TRADING_ENABLED=true gerekli.',
        }.get(code, 'Silahsız. Arm reddedildi.')

    def _stopped_text(self):
        if self.permission.view().get('armed'):
            return 'Silahlı, yeni girişler kapalı. Durdur /stop ile aynı.'
        return 'Silahsız. Yeni giriş zaten kapalı.'

    def _state_text(self):
        view = self.permission.view()
        if self.cfg.live_trading_enabled is not True or self.cfg.broker_mode != BrokerMode.LIVE:
            return 'Silahsız. BROKER_MODE=live ve LIVE_TRADING_ENABLED=true gerekli.'
        if view.get('armed') and self.entries_open():
            return 'Silahlı. Yeni canlı girişler açık.'
        if view.get('armed'):
            return 'Silahlı, yeni girişler kapalı. Durdur /stop ile aynı.'
        return 'Silahsız. İzinli operatör Arm veya /arm göndermeden yeni giriş yok.'

    def present_controls(self):
        """One startup message with Arm and Durdur. Not sent on every cycle."""
        self._control_reply(self._state_text())

    def _control_reply(self, text, callback_id=None):
        self._deliver(text, reply_markup=self.CONTROLS)
        if callback_id:
            self._answer_callback(callback_id, text)

    def _answer_callback(self, callback_id, text):
        if not callback_id or self._telegram is None:
            return
        try:
            self._telegram.call('answerCallbackQuery', callback_query_id=callback_id, text=str(text)[:180])
        except Exception as exc:
            logger.warning('live telegram callback ack failed (%s)', type(exc).__name__)

    def _intents(self):
        with self.store.lock:
            rows = self.store.db.execute("SELECT value FROM state WHERE key LIKE 'live:intent:%'").fetchall()
        found = []
        for (payload,) in rows:
            value = json.loads(payload)
            if isinstance(value, dict):
                found.append(value)
        return found

    def _symbols(self):
        symbols = []
        for row in self._intents():
            symbol = (row.get('proposal') or {}).get('symbol')
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols[:40]

    def consider(self, proposal):
        """Hand a model proposal to the live executor only when entries are armed."""
        if not isinstance(proposal, TradeProposal):
            return dict(accepted=False, reason='proposal_required')
        if not self.entries_open():
            return dict(accepted=False, reason='entries_blocked')
        try:
            row = self.executor.stage(proposal)
            if row.get('status') == 'awaiting_manual_approval':
                operator = self.permission.view().get('armed_by')
                row = self.executor.approve(row['intent_id'], row['proposal_fingerprint'], operator,
                    allow_emergency_close=True)
            result = self.executor.execute(row['intent_id'])
        except LiveExecutionError as exc:
            return dict(accepted=False, reason=str(exc))
        note = self._notify(result, self._last_snapshot())
        return dict(accepted=result.get('status') in ('protected', 'partial_protected', 'partial_fill', 'closed'),
            result=result, notification=note)

    def _last_snapshot(self):
        try:
            row = self.executor.reconciler.snapshot()
        except LiveAccountError:
            return None
        return row if row.get('ready') else None

    def _notify(self, row, snapshot):
        status = row.get('status')
        if status not in ('protected', 'partial_protected', 'partial_fill', 'closed'):
            return None
        marker = 'live:notified:'+row['intent_id']
        if self.store.get(marker) == status:
            return None
        text = self._text(row, snapshot)
        self.store.put(marker, status)
        events = list(self.store.get('live:notifications', []))
        events.append(dict(intent_id=row['intent_id'], status=status, text=text))
        self.store.put('live:notifications', events[-50:])
        self._deliver(text)
        return text

    @staticmethod
    def _text(row, snapshot):
        symbol = row['proposal']['symbol']
        costs = row.get('exchange_costs') or {}
        balance = {} if not snapshot else snapshot.get('balance') or {}
        tail = ''
        if balance.get('equity') is not None and balance.get('available') is not None:
            tail = f" Exchange equity {balance['equity']} USDT, available margin {balance['available']} USDT."
        if row['status'] == 'closed':
            parts = [f"Exchange confirmed close {symbol} position {row.get('position_id')}."]
            if row.get('halt_reason') == 'filled_position_risk_exceeds_plan':
                parts.append(
                    f"Review: fill {row.get('fill_price')} passed worst allowed {row.get('worst_allowed_price')}. "
                    f"Computed stop loss {row.get('computed_loss')} exceeded the approved cap. "
                    f"This confirmed close does not halt entries on other symbols.")
            if 'fee' in costs:
                parts.append(f"Exchange fee {costs['fee']}.")
            if 'funding' in costs:
                parts.append(f"Exchange funding {costs['funding']}.")
            return ' '.join(parts)+tail
        if row['status'] == 'protected':
            return (f"Exchange confirmed entry {symbol} position {row.get('position_id')} "
                    f"filled {row.get('contracts')}. Native protection read back from the exchange."+tail)
        return (f"Exchange reported partial fill {symbol} filled {row.get('filled_contracts')} of {row.get('contracts')}. "
                f"The original order id was kept and no second entry was sent."+tail)

    def _deliver(self, text, reply_markup=None):
        if not text or not (self.cfg.telegram_bot_token and self.cfg.telegram_chat_id):
            return
        try:
            from trading.runtime.telegram import Telegram
            if self._telegram is None:
                self._telegram = Telegram(self.cfg, self.store)
            self._telegram.send(text, reply_markup=reply_markup)
        except Exception as exc:
            logger.warning('live telegram send failed (%s)', type(exc).__name__)
            return

    def cycle(self):
        self.permission.sync(broker_mode=self.cfg.broker_mode, live_trading_enabled=self.cfg.live_trading_enabled is True)
        self._apply_commands()
        snapshot = None
        try:
            snapshot = self.executor.reconciler.refresh(self.private, symbols=self._symbols())
        except sqlite3.Error:
            raise
        except LiveAccountError:
            snapshot = None
        notes = []
        for intent in self._intents():
            if not intent.get('entry_write_attempted') or intent.get('status') in ('closed', 'entry_rejected'):
                continue
            try:
                result = self.executor.reconcile(intent['intent_id'])
            except sqlite3.Error:
                raise
            except LiveExecutionError:
                continue
            note = self._notify(result, snapshot)
            if note:
                notes.append(note)
        view = self.permission.view()
        balance = {} if not snapshot else snapshot.get('balance') or {}
        if snapshot is None:
            reason = 'HOLD_ACCOUNT_UNAVAILABLE'
        elif not view.get('armed'):
            reason = 'LIVE_DISARMED'
        elif not self.entries_open():
            reason = 'HOLD_ENTRIES_STOPPED'
        else:
            reason = 'LIVE_ENTRIES_OPEN'
        return dict(mode='LIVE', reason=reason, armed=bool(view.get('armed')), running=bool(view.get('running')),
            entries_open=self.entries_open(), equity=balance.get('equity'), available=balance.get('available'),
            position_margin=balance.get('position_margin'),
            positions=list(snapshot.get('positions') or []) if snapshot else [],
            source='mexc_exchange', notifications=notes)
