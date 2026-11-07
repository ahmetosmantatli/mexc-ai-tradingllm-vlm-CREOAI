"""Telegram delivery with durable trade-event retries and allowlisted commands."""
import logging
import time
import httpx

from trading.config import BrokerMode

logger = logging.getLogger(__name__)


def paper_updates_allowed(cfg):
    """Paper getUpdates runs only while the process is not the live worker."""
    return cfg.broker_mode != BrokerMode.LIVE and cfg.live_trading_enabled is not True


class Telegram:
    def __init__(self, settings, store, client=None):
        self.cfg, self.store = settings, store
        self.client = client or httpx.Client(timeout=5)

    @property
    def enabled(self):
        return bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id)

    def call(self, method, **data):
        # Do not log response URLs or raw exceptions: bot token is part of the URL.
        response = self.client.post(f'https://api.telegram.org/bot{self.cfg.telegram_bot_token}/{method}', json=data)
        response.raise_for_status()
        payload = response.json()
        if payload.get('ok') is not True:
            raise ValueError('Telegram request unsuccessful')
        return payload['result']

    def send(self, text, reply_markup=None):
        payload = dict(chat_id=self.cfg.telegram_chat_id, text=text[:4000])
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        self.call('sendMessage', **payload)

    def tick(self, ledger, status_text):
        if not self.enabled:
            return
        if not paper_updates_allowed(self.cfg):
            logger.warning('paper telegram poll skipped; live worker owns getUpdates')
            return
        try:
            for event in ledger.state['events']:
                if event['sent']:
                    continue
                if event['kind'] == 'OPEN':
                    text = (f"PAPER | İşlem açıldı\n{event['symbol']} {event['side']} {event['leverage']:g}x\n"
                            f"Giriş: {event['entry']:.6g} | SL: {event['stop']:.6g} | TP: {event['target']:.6g}\n"
                            f"Büyüklük: {event['notional']:.4f} USDT")
                else:
                    text = (f"PAPER | İşlem kapandı: {event['reason']}\n{event['symbol']}\n"
                            f"Net sonuç: {event['net_pnl']:+.4f} USDT")
                self.send(text+f"\nOlay bakiyesi: {event['equity']:.4f} USDT\n\n"+status_text)
                event['sent'] = True
                ledger.save()
            offset = self.store.get('telegram_offset', 0)
            updates = self.call('getUpdates', offset=offset, timeout=0, allowed_updates=['message'])
            for update in updates:
                message = update.get('message', {})
                chat = message.get('chat', {})
                if str(chat.get('id')) == self.cfg.telegram_chat_id and message.get('text','').split('@')[0].strip() in ('/durum','/status','/start'):
                    self.send(status_text)
                self.store.put('telegram_offset', update['update_id']+1)
            last = self.store.get('telegram_status_at', 0)
            if time.time()-last >= self.cfg.telegram_status_interval_seconds:
                self.send(status_text)
                self.store.put('telegram_status_at', time.time())
            self.store.put('telegram_health', dict(ok=True, observed_at=time.time()))
        except Exception as exc:
            logger.warning('Telegram delivery delayed (%s)', type(exc).__name__)
            self.store.put('telegram_health', dict(ok=False, observed_at=time.time()))
