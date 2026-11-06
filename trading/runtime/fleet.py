"""One bot, isolated account/version ledgers, shared AI budget, independent price supervision."""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
from uuid import uuid4
import json
import logging
import os
from pathlib import Path
import threading
import time

from trading.agents.anthropic_client import AnthropicClient
from trading.config import Settings
from trading.control import load_accounts, TelegramController
from trading.evaluation.service import ContextEvaluator
from trading.exchange.mexc_public import MexcPublicClient
from trading.exchange.errors import ConnectionIssue
from trading.runtime.budget import Budget
from trading.runtime.protection import PositionProtection
from trading.runtime.runner import PaperRunner
from trading.runtime.store import RuntimeStore

logger = logging.getLogger(__name__)


class Fleet:
    def __init__(self, cfg):
        # pydantic reads .env, registry references are resolved without printing values.
        from dotenv import dotenv_values
        environment = {**dotenv_values('.env'), **os.environ}
        self.cfg = cfg
        self.accounts = load_accounts(cfg.accounts_file, environment)
        self.root = Path(cfg.runtime_db_path).parent
        self.control_store = RuntimeStore(str(self.root/'control.sqlite3'))
        self.controller = TelegramController(cfg.telegram_bot_token, self.accounts, self.control_store)
        if not cfg.telegram_bot_token:
            raise ValueError('TELEGRAM_BOT_TOKEN required for fleet')
        from trading.control.vault import Vault
        self.vault = Vault(self.control_store, cfg.account_vault_key) if cfg.account_vault_key else None
        from trading.control.connections import ConnectionFlow
        self.connections = ConnectionFlow(self.controller,self.vault,self.bind_account,self.disconnect_account)
        original_dispatch = self.controller.dispatch
        self.controller.dispatch = lambda update: None if self.connections.handle(update) else original_dispatch(update)
        self.runners = {}
        self.protections = []
        self.pool = ThreadPoolExecutor(max_workers=len(self.accounts), thread_name_prefix='analysis')
        self.futures = {}
        self.funding_futures = {}
        self.funding_pool = ThreadPoolExecutor(max_workers=len(self.accounts)*2, thread_name_prefix='funding')
        self.last_funding = {}
        self.funding_health = {}
        self.last_status = {}
        self.budget_store = RuntimeStore(cfg.shared_budget_db_path)
        self.last_publish = {}
        for account in self.accounts:
            saved = self.vault.get(account.account_id) if self.vault else None
            if self.control_store.get(f'account:{account.account_id}:disconnected',False):
                account = replace(account,mexc_api_key='',mexc_api_secret='')
            elif saved:
                account = replace(account,mexc_api_key=saved.api_key,mexc_api_secret=saved.api_secret)
            self.create_runners(account)

    def create_runners(self, account):
        cfg=self.cfg
        for version in ('text','vlm'):
            session = self.control_store.get(f'account:{account.account_id}:session')
            directory = self.root/account.account_id/(session or 'initial')/version
            overrides = dict(runtime_db_path=str(directory/'runtime.sqlite3'),
                analysis_chart_dir=str(directory/'charts'), analysis_version=version,
                mexc_api_key=account.mexc_api_key, mexc_api_secret=account.mexc_api_secret,
                telegram_bot_token='', telegram_chat_id='')
            settings = cfg.model_copy(update=overrides)
            llm = AnthropicClient(settings=settings, budget=Budget(self.budget_store,cfg))
            runner = PaperRunner(settings,llm=llm)
            runner.graph = ContextEvaluator(llm, runner.store, runner.public, settings)
            runner.external_control = lambda aid=account.account_id,v=version:self.entry_control(aid,v)
            public = MexcPublicClient(base_url=cfg.mexc_base_url,timeout_seconds=2)
            runner.protection = PositionProtection(lambda r=runner:r.ledger,public,lock=runner.lock,
                poll_seconds=cfg.position_poll_seconds,max_age_seconds=cfg.market_max_age_seconds,
                health_max_age_seconds=cfg.protection_max_age_seconds)
            self.runners[(account.account_id,version)] = runner
            self.protections.append(runner.protection)

    def idle_account(self, ident):
        self.controller.set_control(ident,enabled=False)
        future=self.futures.get(ident)
        if future and not future.done():
            raise ConnectionIssue('Analiz sürüyor; yeni girişler durduruldu. Biraz sonra tekrar dene.')
        for version in ('text','vlm'):
            future=self.funding_futures.get((ident,version))
            if future and not future.done():
                raise ConnectionIssue('Hesap uzlaştırması sürüyor; biraz sonra tekrar dene.')
            runner=self.runners[(ident,version)]
            with runner.lock:
                if runner.ledger and runner.ledger.positions:
                    raise ConnectionIssue('Önce /closeall ile açık paper pozisyonları kapat; sonra bağlantıyı kaldır.')

    def replace_runners(self, ident, key, secret):
        account=next(a for a in self.accounts if a.account_id==ident)
        for version in ('text','vlm'):
            old=self.runners[(ident,version)]
            old.protection.stop()
            self.protections.remove(old.protection)
        self.create_runners(replace(account,mexc_api_key=key,mexc_api_secret=secret))
        for version in ('text','vlm'):
            self.runners[(ident,version)].protection.start()
            self.funding_health[(ident,version)]=False
            self.last_funding[(ident,version)]=0
            self.last_status[(ident,version)]=0
        self.futures.pop(ident,None)
        self.control_store.put(f'account:{ident}:status',{})

    def bind_account(self, ident, key, secret):
        if self.vault is None:
            raise ConnectionIssue('Sunucuda şifreleme anahtarı eksik.')
        self.idle_account(ident)
        if not isinstance(key,str) or not isinstance(secret,str):
            raise ConnectionIssue('Geçersiz anahtar biçimi.')
        from trading.exchange.mexc_readonly import MexcReadOnlyClient
        client=MexcReadOnlyClient(api_key=key,api_secret=secret,base_url=self.cfg.mexc_base_url)
        try:
            client.get_futures_account()
            client.get_open_positions()
        finally:
            client.close()
        self.vault.put(ident,key,secret)
        self.control_store.put(f'account:{ident}:disconnected',False)
        self.control_store.put(f'account:{ident}:session',uuid4().hex)
        self.replace_runners(ident,key,secret)

    def disconnect_account(self, ident):
        self.idle_account(ident)
        self.control_store.put(f'account:{ident}:disconnected',True)
        if self.vault:
            self.vault.delete(ident)
        self.replace_runners(ident,'','')

    def entry_control(self, account_id, version):
        control = self.controller.control(account_id)
        result = dict(control)
        result['enabled'] = (control['enabled'] and not control.get('stopped',False) and not self.control_store.get(f'account:{account_id}:disconnected',False) and control['version']==version
                             and self.funding_health.get((account_id,version),False))
        return result

    def reconcile_funding(self, key, runner):
        public = MexcPublicClient(base_url=self.cfg.mexc_base_url,timeout_seconds=3)
        try:
            with runner.lock:
                positions = list(copy.deepcopy(runner.ledger.positions).values()) if runner.ledger else []
                closed = copy.deepcopy(runner.ledger.state.get('closed_positions', [])) if runner.ledger else []
                positions += closed
            for position in positions:
                symbol = position['symbol']
                rows = public.get_funding_history(symbol)
                if not rows or min(int(r['settleTime']) for r in rows)>position['opened_at']:
                    raise ValueError('incomplete historical funding')
                ticker = public.get_ticker(symbol)
                for row in sorted(rows,key=lambda r:r['settleTime']):
                    stamp=int(row['settleTime'])
                    if position['opened_at'] < stamp <= time.time()*1000:
                        with runner.lock:
                            current=runner.ledger.positions.get(symbol)
                            if current and current['opened_at']==position['opened_at']:
                                runner.ledger.settle_funding(symbol,stamp,float(row['fundingRate']),float(ticker.raw['fairPrice']))
                            elif any(p['symbol']==symbol and p['opened_at']==position['opened_at'] for p in runner.ledger.state.get('closed_positions', [])):
                                runner.ledger.reconcile_closed_funding(symbol,position['opened_at'],stamp,float(row['fundingRate']),float(ticker.raw['fairPrice']))
            self.funding_health[key] = True
        except Exception as exc:
            self.funding_health[key] = False
            logger.warning('funding paused entries account=%s version=%s (%s)',*key,type(exc).__name__)
        finally:
            public.close()

    def publish(self, account):
        selected = self.controller.control(account.account_id)['version']
        runner = self.runners[(account.account_id,selected)]
        with runner.lock:
            snapshot = runner.store.get('status', {})
            if snapshot:
                # Price protection may have changed balances since the last slow analysis.
                snapshot['paper'] = runner.ledger.metrics() if runner.ledger else None
                snapshot['positions'] = copy.deepcopy(runner.ledger.positions) if runner.ledger else {}
                snapshot['balance_observed_at'] = time.time()
        snapshot['variants'] = {}
        for version in ('text','vlm'):
            other=self.runners[(account.account_id,version)]
            with other.lock:
                snapshot['variants'][version] = dict(paper=other.ledger.metrics() if other.ledger else None, positions=copy.deepcopy(other.ledger.positions) if other.ledger else {})
        ai_state=(snapshot.get('ai_status'),snapshot.get('reason'))
        previous=self.control_store.get(f'account:{account.account_id}:ai_notice')
        if snapshot.get('ai_status') in ('error_or_invalid_output','budget_exhausted','budget_or_interval_hold','provider_credit_exhausted') and previous!=list(ai_state):
            prefix='Claude API bakiyesi yetersiz. ' if snapshot.get('ai_status')=='provider_credit_exhausted' else ''
            self.controller.enqueue(account.account_id,prefix+'Claude çağrısı başarısız veya bütçe sınırına ulaşıldı. Yeni AI işlemleri bekliyor; açık pozisyon takibi devam ediyor. Sağlayıcı bakiyesi, erişim ve ağ durumunu kontrol et.',event_id='ai:'+str(time.time()))
        self.control_store.put(f'account:{account.account_id}:ai_notice',list(ai_state))
        snapshot['version'] = selected
        snapshot['control'] = self.controller.control(account.account_id)
        snapshot['protection'] = runner.protection.snapshot()
        snapshot['ai_budget'] = self.budget_store.get('ai_budget',{})
        active=any(v.get('positions') for v in snapshot['variants'].values())
        last_update=self.control_store.get(f'account:{account.account_id}:position_update_at',0)
        if active and time.time()-last_update>=self.cfg.position_update_seconds:
            self.controller.enqueue(account.account_id,self.controller.status_text(account.account_id,True),event_id=f'positions:{time.time()}')
            self.control_store.put(f'account:{account.account_id}:position_update_at',time.time())
        self.control_store.put(f'account:{account.account_id}:status',snapshot)
        for version in ('text','vlm'):
            r = self.runners[(account.account_id,version)]
            with r.lock:
                events=copy.deepcopy(r.ledger.state['events']) if r.ledger else []
            for event in events:
                if event.get('sent'):
                    continue
                with r.lock:
                    memory = r.store.get('memory:'+event['symbol'])
                    if memory is not None and event['kind']=='CLOSE':
                        memory['last_outcome'] = {k:event[k] for k in ('reason','net_pnl','equity')}
                        r.store.put('memory:'+event['symbol'],memory)
                text = f"PAPER | {account.account_id} | {version}\n{event['symbol']}\n"
                if event['kind']=='OPEN':
                    text += (f"İşlem açıldı: {event['side']} | {event['leverage']:g}x\n"
                             f"Giriş {event['entry']:.6g} | SL {event['stop']:.6g} | TP {event['target']:.6g}\n"
                             f"Büyüklük {event['notional']:.4f} USDT\n{event.get('rationale','')}\n")
                elif event['kind']=='CLOSE':
                    text += f"İşlem kapandı: {event['reason']} | Net sonuç {event['net_pnl']:+.4f} USDT\n"
                else:
                    text += f"Funding düzeltmesi: {event.get('charge',0):+.4f} USDT\n"
                with r.lock:
                    metric=r.ledger.metrics()
                text += (f"Olay bakiyesi {event['equity']:.4f} USDT\n"
                         f"Güncel paper {metric['equity']:.4f} | Açık PnL {metric['unrealized']:+.4f} USDT")
                observed=r.store.get('real_account')
                if observed:
                    text += (f"\nSon gözlenen gerçek futures {observed['equity']:.4f} USDT"
                             f" | Veri yaşı {max(0,time.time()-observed['observed_at']):.0f} sn")
                self.controller.enqueue(account.account_id,text[:3500],photos=event.get('charts',()),event_id=f'{version}:{event["id"]}')
                with r.lock:
                    next(e for e in r.ledger.state['events'] if e['id']==event['id'])['sent']=True
                    r.ledger.save()
        if snapshot.get('reason') and time.time()-self.last_publish.get(account.account_id,0)>self.cfg.telegram_status_interval_seconds:
            self.controller.enqueue(account.account_id,runner.status_text(snapshot),event_id=f'status:{int(time.time())}')
            self.last_publish[account.account_id]=time.time()

    def start(self):
        for protection in self.protections:
            protection.start()

    def execute_commands(self, account):
        key=f'account:{account.account_id}:commands'
        commands=self.control_store.get(key,[])
        for command in commands:
            if command.get('status')!='pending':
                continue
            runner=self.runners[(account.account_id,command['version'])]
            try:
                quote=runner.quote(command['symbol'])
                with runner.lock:
                    if not runner.ledger:
                        raise ValueError('position absent')
                    runner.ledger.close_manual(command['symbol'],quote.bid1,quote.ask1,command['opened_at'])
                command['status']='done'
            except Exception:
                # Retain retry for quote outage; stale identities become a safe no-op.
                with runner.lock:
                    position=runner.ledger.positions.get(command['symbol']) if runner.ledger else None
                if not position or position['opened_at']!=command['opened_at']:
                    command['status']='expired'
                else:
                    command['error']='Geçerli fiyat bekleniyor; kapanış henüz gerçekleşmedi.'
                    self.controller.enqueue(account.account_id,command['error'],event_id='close-wait:'+command['id'])
            self.control_store.put(key,commands)

    def tick(self):
        # Only this thread polls Telegram and changes its durable outbox.
        self.controller.tick()
        for account in self.accounts:
            self.execute_commands(account)
            control = self.controller.control(account.account_id)
            for version in ('text','vlm'):
                key=(account.account_id,version)
                runner=self.runners[key]
                future=self.funding_futures.get(key)
                if (future is None or future.done()) and time.time()-self.last_funding.get(key,0)>60:
                    self.last_funding[key]=time.time()
                    self.funding_futures[key]=self.funding_pool.submit(self.reconcile_funding,key,runner)
            key=(account.account_id,control['version'])
            runner=self.runners[key]
            future=self.futures.get(account.account_id)
            if future and future.done():
                # Persistence failure must be visible and fail closed.
                future.result()
            if (future is None or future.done()) and time.time()-self.last_status.get(key,0)>self.cfg.position_poll_seconds:
                runner.cfg.risk_per_trade_pct=control['risk_per_trade_pct']
                self.last_status[key]=time.time()
                self.futures[account.account_id]=self.pool.submit(runner.cycle)
            self.publish(account)

    def stop(self):
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.funding_pool.shutdown(wait=True,cancel_futures=True)
        for protection in self.protections:
            protection.stop()
