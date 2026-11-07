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
import sqlite3
import time

from trading.agents.provider_router import ProviderRouter
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
        self.scan_pool = ThreadPoolExecutor(max_workers=len(self.accounts),thread_name_prefix='scan')
        self.scan_futures = {}
        self.last_scan_schedule = {}
        self.funding_futures = {}
        self.funding_pool = ThreadPoolExecutor(max_workers=len(self.accounts)*2, thread_name_prefix='funding')
        self.last_funding = {}
        self.funding_health = {}
        self.last_status = {}
        self.management_futures = {}
        self.management_pool = ThreadPoolExecutor(max_workers=len(self.accounts)*2,thread_name_prefix="management")
        self.last_management = {}
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
            control=self.controller.control(account.account_id)
            demo=control.get('capital_mode')=='demo'
            overrides = dict(require_mexc_balance=not demo, paper_seed_usdt=control.get('capital_amount') or 0 if demo else 0, paper_allocation_usdt=control.get('capital_amount') if not demo else None, runtime_db_path=str(directory/'runtime.sqlite3'),
                analysis_chart_dir=str(directory/'charts'), analysis_version=version,
                mexc_api_key=account.mexc_api_key, mexc_api_secret=account.mexc_api_secret,
                telegram_bot_token='', telegram_chat_id='')
            settings = cfg.model_copy(update=overrides)
            if settings.ai_provider_pool_enabled:
                from trading.agents.provider_pool import ProviderPool
                llm=ProviderPool(settings,Budget(self.budget_store,cfg),self.budget_store)
            else:
                llm = ProviderRouter(settings, Budget(self.budget_store,cfg), self.budget_store)
            runner = PaperRunner(settings,llm=llm)
            runner.external_scan = True
            runner.graph = ContextEvaluator(llm, runner.store, runner.public, settings)
            runner.external_control = lambda aid=account.account_id,v=version:self.entry_control(aid,v)
            runner.external_scan_control = lambda aid=account.account_id,v=version:self.scan_control(aid,v)
            llm.can_call = lambda r=runner: bool(r.external_control().get("enabled"))
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
            scanning=self.scan_futures.get((ident,version))
            if scanning and not scanning.done():
                raise ConnectionIssue('Piyasa taraması tamamlanıyor; yeni analizler durduruldu. Biraz sonra tekrar dene.')
            future=self.funding_futures.get((ident,version))
            if future and not future.done():
                raise ConnectionIssue('Hesap uzlaştırması sürüyor; biraz sonra tekrar dene.')
            management=self.management_futures.get((ident,version))
            if management and not management.done():
                raise ConnectionIssue('Pozisyon yönetimi sürüyor; biraz sonra tekrar dene.')
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
            close=getattr(old.llm,'close',None)
            if close:
                close()
        self.create_runners(replace(account,mexc_api_key=key,mexc_api_secret=secret))
        for version in ('text','vlm'):
            self.runners[(ident,version)].protection.start()
            self.funding_health[(ident,version)]=False
            self.last_funding[(ident,version)]=0
            self.last_status[(ident,version)]=0
            self.last_scan_schedule[(ident,version)]=0
            self.scan_futures.pop((ident,version),None)
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
        self.control_store.put(f'account:{ident}:linked',True)
        self.control_store.put(f'account:{ident}:session',uuid4().hex)
        self.replace_runners(ident,key,secret)

    def disconnect_account(self, ident):
        self.idle_account(ident)
        self.control_store.put(f'account:{ident}:disconnected',True)
        self.control_store.put(f'account:{ident}:linked',False)
        if self.vault:
            self.vault.delete(ident)
        self.replace_runners(ident,'','')

    def scan_control(self,account_id,version):
        control=self.controller.control(account_id)
        return dict(enabled=bool(control.get('enabled') and not control.get('stopped')
            and self.controller.capital_ready(account_id) and control.get('version_chosen')
            and control.get('profile_chosen') and control['version']==version))

    def entry_control(self, account_id, version):
        control = self.controller.control(account_id)
        result = dict(control)
        result['enabled'] = ((control.get('capital_mode')=='demo' or self.control_store.get(f'account:{account_id}:linked',False)) and control.get('capital_chosen',False) and not control.get('capital_pending') and control.get('version_chosen',False) and control.get('profile_chosen',False) and control['enabled'] and not control.get('stopped',False) and (control.get('capital_mode')=='demo' or not self.control_store.get(f'account:{account_id}:disconnected',False)) and control['version']==version
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
        snapshot['scan_summary']=runner.store.get('scan_summary',{})
        snapshot['scan_stage']=runner.store.get('scan_stage','idle')
        queue_rows=[row for row in runner.store.get('candidate_queue',[]) if row.get('expires_at',0)>time.time()
                    and row.get('signal',{}).get('symbol') not in snapshot.get('positions',{})]
        next_row=min(queue_rows,key=lambda row:(row.get('last_considered_at',0),row.get('first_seen',0),-row.get('signal',{}).get('confidence',0),row.get('key',''))) if queue_rows else None
        snapshot['queue_status']=dict(size=len(queue_rows),next_symbol=next_row['signal']['symbol'] if next_row else None)
        snapshot['stage']=runner.store.get('stage','awaiting_scan')
        snapshot['ai_schedule_mode']=getattr(self.cfg,'ai_schedule_mode','interval')
        snapshot['next_evaluation_at']=(runner.store.get('last_evaluation_at',0)+self.cfg.anthropic_min_interval_seconds
            if snapshot['ai_schedule_mode']=='interval' else None)
        snapshot['provider_wait_until']=runner.store.get('provider_defer_until',0)
        snapshot['analysis_trace']=runner.store.get('analysis_trace',{})
        snapshot['analysis_history']=runner.store.get('analysis_history',[])
        snapshot['ai_request_stats']=self.budget_store.get('ai_request_stats',{})
        # Concurrent candidate reviews each retain their own result and notification.
        from trading.agents.telemetry import format_trace
        traces={row['id']:row for row in snapshot['analysis_history']}
        latest=snapshot['analysis_trace']
        if latest.get('id'):
            traces[latest['id']]=latest
        for trace in traces.values():
            if trace.get('completed_at') and trace.get('execution_status') in ('opened','rejected','not_opened'):
                self.controller.enqueue(account.account_id,format_trace(trace),event_id=f"analysis:{selected}:{trace['id']}",buttons=True)
        snapshot['parallel_candidates']=self.cfg.ai_parallel_candidates if self.cfg.ai_provider_pool_enabled else 1
        pool_snapshot=getattr(runner.llm,'snapshot',None)
        snapshot['provider_pool']=pool_snapshot() if pool_snapshot else []
        if snapshot.get('real_account') and not self.control_store.get(f'account:{account.account_id}:disconnected',False):
            self.control_store.put(f'account:{account.account_id}:linked',True)
        from trading.runtime.reports import daily_trade_report
        snapshot['reports']=[]
        snapshot['reports_by_day']={}
        for version in ('text','vlm'):
            r=self.runners[(account.account_id,version)]
            with r.lock:
                if r.ledger:
                    report=daily_trade_report(copy.deepcopy(r.ledger.state),max_rows=5)
                    report['version']=version
                    snapshot['reports'].append(report)
                    from datetime import datetime,timedelta
                    from zoneinfo import ZoneInfo
                    for offset in range(7):
                        day=(datetime.now(ZoneInfo('Europe/Istanbul')).date()-timedelta(days=offset)).isoformat()
                        history=daily_trade_report(copy.deepcopy(r.ledger.state),day=day,max_rows=5)
                        history['version']=version
                        snapshot['reports_by_day'].setdefault(day,[]).append(history)
        snapshot['capital_mode']=self.controller.control(account.account_id).get('capital_mode')
        snapshot['capital_amount']=self.controller.control(account.account_id).get('capital_amount')
        snapshot['version'] = selected
        snapshot['control'] = self.controller.control(account.account_id)
        snapshot['protection'] = runner.protection.snapshot()
        snapshot['ai_budget'] = self.budget_store.get('ai_budget',{})
        snapshot['groq_usage'] = self.budget_store.get('groq_usage',{})
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
                    text += f"Aday algoritması: {event.get('strategy','kayıtlı değil')}\n"
                    if event.get('candidate_score') is not None:
                        text += f"Aday puanı: {event['candidate_score']:.2f} (başarı olasılığı değil)\n"
                    if event.get('stop_loss_estimate') is not None:
                        text += f"Planlanan stop kaybı: %{event['stop_risk_pct']*100:.2f} | {event['stop_loss_estimate']:.4f} USDT (ücret/kayma varsayımlarıyla)\n"
                elif event['kind']=='CLOSE':
                    text += f"İşlem kapandı: {event['reason']} | Net sonuç {event['net_pnl']:+.4f} USDT\n"
                elif event['kind']=='STOP_UPDATED':
                    text += f"Stop güncellendi: {event['old_stop']:.6g} → {event['new_stop']:.6g}\nGiriş {event['entry']:.6g} | TP {event['target']:.6g}\n{event['reason']}\n"
                else:
                    text += f"Funding düzeltmesi: {event.get('charge',0):+.4f} USDT\n"
                if event['kind'] in ('OPEN','CLOSE','STOP_UPDATED'):
                    origin=event.get('decision_ai') or {}
                    provider=origin.get('provider') or 'kayıtlı değil'
                    model=origin.get('model') or 'kayıtlı değil'
                    text += f"Giriş kararını veren AI: {provider} | {model}\n"
                    if origin.get('fallback_reason'):
                        text += f"Yedek geçiş nedeni: {origin['fallback_reason']}\n"
                    if event['kind']=='OPEN':
                        text += 'Uygulama: risk motoru + PAPER broker (simülasyon)\n'
                    elif event['kind']=='STOP_UPDATED':
                        text += 'Stop aksiyonu: kod tabanlı yönetim; yeni AI kararı değil.\n'
                    else:
                        text += 'Kapanış: SL/TP veya manuel kontrol; yeni AI kararı değil.\n'
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

    @staticmethod
    def apply_trader_mode(runner,control):
        from trading.config import RiskProfile
        from trading.risk.engine import RiskEngine
        profile = control.get('risk_profile','balanced')
        limits = {
            'capital_preservation': (.005,3,2,.02,.08),
            'balanced': (.01,5,3,.035,.12),
            'aggressive_growth': (.03,10,4,.055,.225),
        }
        if profile not in limits:
            profile='balanced'
        risk,leverage,positions,daily,drawdown=limits[profile]
        runner.cfg.risk_profile=RiskProfile(profile)
        runner.cfg.risk_per_trade_pct=min(control['risk_per_trade_pct'],risk)
        runner.cfg.paper_max_leverage=leverage
        runner.cfg.default_target_leverage=leverage
        runner.cfg.paper_max_positions=positions
        runner.cfg.daily_loss_limit_pct=daily
        runner.cfg.drawdown_limit_pct=drawdown
        runner.risk=RiskEngine(settings=runner.cfg)

    def apply_capital(self,account,command):
        import math
        self.idle_account(account.account_id)
        mode=command['mode']
        if mode not in ('demo','account'):
            raise ConnectionIssue('Geçersiz kasa türü.')
        amount=command.get('amount')
        selected=self.controller.control(account.account_id)['version']
        old=self.runners[(account.account_id,selected)]
        if mode=='account':
            observed=old.account_client.get_futures_account()
            if command.get('allocation_mode')=='full':
                amount=observed.available
            if amount is None or amount>observed.available:
                raise ConnectionIssue('Seçilen tutar kullanılabilir Futures USDT bakiyesini aşıyor.')
        if amount is None or not math.isfinite(float(amount)) or float(amount)<=0:
            raise ConnectionIssue('Kasa tutarı pozitif USDT olmalı.')
        self.controller.set_control(account.account_id,enabled=False,capital_mode=mode,capital_amount=float(amount),
            allocation_mode=command.get('allocation_mode','fixed'),capital_chosen=True,capital_pending=None,
            version_chosen=False,profile_chosen=False)
        self.control_store.put(f'account:{account.account_id}:session',uuid4().hex)
        self.replace_runners(account.account_id,old.cfg.mexc_api_key,old.cfg.mexc_api_secret)
        self.controller.enqueue(account.account_id,f'PAPER kasası hazır: {amount:g} USDT. Gerçek emir yok.\n'+self.controller.settings_text(account.account_id),event_id='capital-ready:'+command['id'],buttons=True)

    def manage_positions(self,key,runner):
        from trading.runtime.management import propose_management
        public=MexcPublicClient(base_url=self.cfg.mexc_base_url,timeout_seconds=3)
        try:
            with runner.lock:
                positions=copy.deepcopy(runner.ledger.positions) if runner.ledger else {}
            for symbol,p in positions.items():
                try:
                    candles=public.get_klines(symbol,interval='Min15',limit=100)['candles']
                    action=propose_management(p,candles,timeframe_seconds=900)
                    if action.get('action')!='tighten_stop':
                        continue
                    quote=public.get_ticker(symbol)
                    stamp=float(quote.raw['timestamp'])/1000
                    if not -5<=time.time()-stamp<=self.cfg.market_max_age_seconds:
                        continue
                    with runner.lock:
                        runner.ledger.update_stop(symbol,action['new_stop'],quote.bid1,quote.ask1,p['opened_at'],'Kapanmış mumlarda teyitli yapı ve ATR tamponu (deneysel)',action['evidence_at'])
                except sqlite3.Error:
                    runner.management_fatal=True
                    logger.error('management persistence failure; entries blocked account=%s version=%s',*key)
                    break
                except Exception as exc:
                    logger.warning('management skipped symbol=%s (%s)',symbol,type(exc).__name__)
        finally:
            public.close()

    def execute_commands(self, account):
        key=f'account:{account.account_id}:commands'
        commands=self.control_store.get(key,[])
        for command in commands:
            if command.get('status')!='pending':
                continue
            if command.get('kind')=='capital_reset':
                jobs=[self.futures.get(account.account_id)]+[self.scan_futures.get((account.account_id,v)) for v in ('text','vlm')]+[self.funding_futures.get((account.account_id,v)) for v in ('text','vlm')]+[self.management_futures.get((account.account_id,v)) for v in ('text','vlm')]
                if any(job and not job.done() for job in jobs):
                    continue
                try:
                    self.apply_capital(account,command)
                    command['status']='done'
                except Exception as exc:
                    command['status']='failed'
                    self.controller.set_control(account.account_id,enabled=False,capital_pending=None,capital_chosen=False)
                    self.controller.enqueue(account.account_id,str(exc) if isinstance(exc,ConnectionIssue) else 'Kasa seçimi uygulanamadı; hesap ve açık pozisyon durumunu kontrol et.',event_id='capital-error:'+command['id'])
                self.control_store.put(key,commands)
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
            if control.get('capital_pending'):
                self.publish(account)
                continue
            for version in ('text','vlm'):
                key=(account.account_id,version)
                runner=self.runners[key]
                management=self.management_futures.get(key)
                if (management is None or management.done()) and time.time()-self.last_management.get(key,0)>=self.cfg.management_interval_seconds:
                    self.last_management[key]=time.time()
                    self.management_futures[key]=self.management_pool.submit(self.manage_positions,key,runner)
                future=self.funding_futures.get(key)
                if (future is None or future.done()) and time.time()-self.last_funding.get(key,0)>60:
                    self.last_funding[key]=time.time()
                    self.funding_futures[key]=self.funding_pool.submit(self.reconcile_funding,key,runner)
            key=(account.account_id,control['version'])
            runner=self.runners[key]
            scanning=self.scan_futures.get(key)
            if scanning and scanning.done():
                scanning.result()  # Persistence failures must not be hidden.
            if (control.get('enabled') and not control.get('stopped') and self.controller.capital_ready(account.account_id)
                    and control.get('version_chosen') and control.get('profile_chosen') and self.cfg.always_on_scan
                    and (scanning is None or scanning.done())
                    and time.time()-self.last_scan_schedule.get(key,0)>=self.cfg.worker_loop_interval_seconds):
                self.last_scan_schedule[key]=time.time()
                self.scan_futures[key]=self.scan_pool.submit(runner.scan_market)
            future=self.futures.get(account.account_id)
            if future and future.done():
                # Persistence failure must be visible and fail closed.
                future.result()
            if (future is None or future.done()) and time.time()-self.last_status.get(key,0)>self.cfg.position_poll_seconds:
                self.apply_trader_mode(runner,control)
                self.last_status[key]=time.time()
                self.futures[account.account_id]=self.pool.submit(runner.cycle)
            self.publish(account)

    def stop(self):
        self.scan_pool.shutdown(wait=True,cancel_futures=True)
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.funding_pool.shutdown(wait=True,cancel_futures=True)
        self.management_pool.shutdown(wait=True,cancel_futures=True)
        for protection in self.protections:
            protection.stop()
        for runner in self.runners.values():
            close=getattr(runner.llm,"close",None)
            if close:
                close()
