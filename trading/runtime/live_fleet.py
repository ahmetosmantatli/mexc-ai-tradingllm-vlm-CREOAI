"""Opt-in real-account orchestration. No paper ledger or simulated fills.

Construction is gated; tests inject clients/runners and never contact an exchange.
Entry permission and ongoing reconciliation deliberately have separate schedules.
"""
from trading.runtime.live_capabilities import STRUCTURAL_IOC_RUNNERS
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import os
from pathlib import Path
import time
from uuid import uuid4

from trading.agents.telemetry import human_reason
from trading.control.accounts import load_accounts
from trading.control.vault import Vault
from trading.control.connections import ConnectionFlow
from trading.exchange.errors import ConnectionIssue
from trading.runtime.store import RuntimeStore


EMERGENCY_REASONS={
    'ioc_fill_outside_entry_plan':'Dolum, giriş planı kontrolünden geçmedi.',
    'native_protection_not_confirmed':'Borsadaki SL/TP koruması doğrulanamadı.',
    'filled_position_risk_exceeds_plan':'Gerçek dolum sonrası risk/likidasyon kontrolü geçmedi.',
    'management_target_protection_failed':'Hedef revizyonu sonrası borsa koruması doğrulanamadı.',
    'management_stop_protection_failed':'Stop revizyonu sonrası borsa koruması doğrulanamadı.',
    'management_remaining_protection_failed':'Kısmi kapanış sonrası kalan pozisyonun koruması doğrulanamadı.',
}


ENTRY_REJECTION_REASONS = {
    'HOLD_LIVE_POSITION_LIMIT': 'Açık pozisyon sayısı sınırı dolu.',
    'HOLD_LIVE_SYMBOL_OPEN': 'Bu paritede zaten açık pozisyon var.',
    'HOLD_LIVE_CANDIDATE_STALE': 'Aday analiz sırasında güncelliğini yitirdi.',
    'HOLD_LIVE_WATCH_CHANGED': 'Takip planı analiz sırasında değişti.',
    'HOLD_LIVE_DECISION_EXPIRED': 'Model kararının geçerlilik süresi doldu.',
    'HOLD_LIVE_CONTROL_CHANGED': 'İşlem arayışı veya ayarlar analiz sırasında değişti.',
    'HOLD_LIVE_HALT': 'Hesapta teknik giriş engeli var.',
    'HOLD_LIVE_EXECUTOR_DISABLED': 'Canlı emir yürütücüsü etkin değil.',
    'HOLD_LIVE_UNVALIDATED_MODEL': 'Model önerisi giriş doğrulamasını geçmedi.',
    'HOLD_LIVE_EXECUTION_POSITION_LIMIT': 'Açık pozisyon sayısı sınırı dolu.',
    'HOLD_LIVE_EXECUTION_RUNNER3_BOOST_CONCENTRATION': 'Son kontrolde aynı yönde açık pozisyon bulundu; artırılmış risk planı kullanılmadı.',
    'HOLD_LIVE_EXECUTION_CONTRACT_RISK_TIER': 'Kontrat adedi desteklenen risk kademesini aşıyor.',
    'HOLD_LIVE_EXECUTION_INSUFFICIENT_MARGIN': 'Kullanılabilir teminat işlem için yetersiz.',
    'HOLD_LIVE_EXECUTION_STALE_QUOTE': 'Güncel emir fiyatı doğrulanamadı.',
    'HOLD_LIVE_EXECUTION_COSTS_EXCEED_RISK': 'Tahmini ücret ve kayma işlem risk bütçesini aşıyor.',
}
ENTRY_REJECTION_DETAILS = {
    'ALLOCATION_EXCEEDS_CURRENT_EQUITY': 'Ayrılan sermaye güncel özkaynağı aşıyor.',
    'INSUFFICIENT_FREE_MARGIN': 'Kullanılabilir teminat yetersiz.',
    'EXCHANGE_RISK_VOLUME_CAP': 'Kontrat adedi hesabın borsa sınırını aşıyor.',
    'LEVERAGE_SETTING_MISMATCH': 'Borsadaki kaldıraç öneriyle eşleşmiyor.',
    'EXCHANGE_LEVERAGE_CAP': 'Önerilen kaldıraç borsa sınırını aşıyor.',
    'UNRECONCILED_EXTERNAL_POSITIONS': 'Borsada uzlaştırılmamış açık pozisyon var.',
    'UNRECONCILED_EXTERNAL_ORDERS': 'Borsada uzlaştırılmamış açık emir var.',
    'TOTAL_OPEN_RISK_CAP': 'Toplam açık risk sınırı dolu.',
    'MAX_LOSS_EXCEEDS_CAP': 'Öneri işlem başına risk sınırını aşıyor.',
    'MAX_CONCURRENT_POSITIONS': 'Açık pozisyon sayısı sınırı dolu.',
    'DAILY_LOSS_HALT': 'Günlük kayıp sınırına ulaşıldı.',
    'WEEKLY_LOSS_HALT': 'Haftalık kayıp sınırına ulaşıldı.',
    'MAX_DRAWDOWN_KILL': 'Zirveden düşüş sınırına ulaşıldı.',
}


class LiveFleet:
    def __init__(self, cfg, *, accounts=None, controller_factory=None, runner_factory=None,
                 private_factory=None, public_factory=None, llm_factory=None, executor_factory=None):
        mode=getattr(cfg.broker_mode,'value',cfg.broker_mode)
        if (mode!='live' or cfg.live_trading_enabled is not True
                or getattr(cfg,'live_automatic_enabled',False) is not True
                or getattr(cfg,'live_readiness_ack','')!='I_UNDERSTAND_REAL_ORDERS'
                or not getattr(cfg,'live_allowed_accounts',())):
            raise ValueError('Explicit automatic live configuration and account allowlist required')
        if not cfg.telegram_bot_token or not cfg.account_vault_key:
            raise ValueError('Live fleet requires Telegram and encrypted account vault')
        self.cfg=cfg
        if accounts is None:
            from dotenv import dotenv_values
            accounts=load_accounts(cfg.accounts_file,{**dotenv_values('.env'),**os.environ})
        self.accounts=list(accounts)
        self.allowed=frozenset(cfg.live_allowed_accounts)
        if not self.allowed.issubset({a.account_id for a in self.accounts}):
            raise ValueError('Live allowlist contains an unregistered account')
        self.root=Path(cfg.runtime_db_path).parent
        self.control_store=RuntimeStore(str(self.root/'control.sqlite3'))
        self.budget_store=RuntimeStore(cfg.shared_budget_db_path)
        if controller_factory is None:
            from trading.control.live_telegram import LiveTelegramController
            controller_factory=LiveTelegramController
        self.controller=controller_factory(cfg.telegram_bot_token,self.accounts,self.control_store)
        self.vault=Vault(self.control_store,cfg.account_vault_key)
        self.runner_factory=runner_factory
        self.private_factory=private_factory
        self.public_factory=public_factory
        self.llm_factory=llm_factory
        self.executor_factory=executor_factory
        self.runners={}
        self.jobs={}
        self.last_schedule={}
        self.started=False
        self.closed=False
        self.pool=ThreadPoolExecutor(max_workers=max(1,len(self.accounts)*4),thread_name_prefix='live')
        self.connections=ConnectionFlow(self.controller,self.vault,self.bind_account,self.disconnect_account)
        original=self.controller.dispatch
        self.controller.dispatch=lambda update: None if self.connections.handle(update) else original(update)
        for account in self.accounts:
            ident=account.account_id
            # Paper controls are never an automatic live authorization on migration.
            previous_mode=self.control_store.get(f'account:{ident}:worker_mode')
            if previous_mode!='live':
                self.controller.set_control(ident,enabled=False,stopped=True,capital_chosen=False,
                    capital_pending=None,version_chosen=False,profile_chosen=False)
                self.control_store.put(f'account:{ident}:live_initialized',True)
                commands=self.control_store.get(f'account:{ident}:commands',[])
                for command in commands:
                    if command.get('status')=='pending':command.update(status='cancelled_mode_change')
                self.control_store.put(f'account:{ident}:commands',commands)
                self.control_store.put(f'account:{ident}:live_grant_revision',None)
            self.control_store.put(f'account:{ident}:worker_mode','live')
            saved=self.vault.get(ident)
            if self.control_store.get(f'account:{ident}:disconnected',False):
                account=replace(account,mexc_api_key='',mexc_api_secret='')
            elif saved:
                account=replace(account,mexc_api_key=saved.api_key,mexc_api_secret=saved.api_secret)
            if account.mexc_api_key and account.mexc_api_secret:
                # Freeze initial provisioned credentials into the authenticated vault;
                # editing env later must not silently retarget an existing live ledger.
                if saved is None:
                    self.vault.put(ident,account.mexc_api_key,account.mexc_api_secret)
                self._create_runner(account)

    def available_runner_ids(self, ident):
        executor=getattr(self.runners.get(ident),'executor',None)
        adapter=getattr(executor,'private',None)
        if (ident not in self.allowed or not self.cfg.live_price_plan_enabled
                or not self.cfg.live_position_management_enabled
                or not callable(getattr(adapter,'submit_ioc',None))):
            return ('baseline-v1',)
        return ('baseline-v1',) + tuple(runner for runner,enabled in (
            ('runner2',self.cfg.live_runner2_enabled),
            ('runner3',self.cfg.live_runner3_enabled and self.cfg.live_structure_setups_enabled),
        ) if enabled)

    def entry_control(self, ident):
        control=dict(self.controller.control(ident))
        control.setdefault('runner_id','baseline-v1')
        control['enabled']=bool(ident in self.allowed and control.get('enabled')
            and not control.get('stopped') and control.get('capital_chosen')
            and not control.get('capital_pending') and control.get('capital_mode')=='account'
            and control.get('version_chosen') and control.get('profile_chosen')
            and not control.get('runner_selection_pending')
            and control['runner_id'] in self.available_runner_ids(ident)
            and self.control_store.get(f'account:{ident}:linked',False)
            and not self.control_store.get(f'account:{ident}:disconnected',False)
            and not self.closed)
        control['operator_id']=next(a.telegram_user_id for a in self.accounts if a.account_id==ident)
        return control

    def _create_runner(self, account):
        ident=account.account_id
        binding=self.control_store.get(f'account:{ident}:live_binding')
        if not binding:
            binding=uuid4().hex
            self.control_store.put(f'account:{ident}:live_binding',binding)
        directory=self.root/ident/'live'
        settings=self.cfg.model_copy(update=dict(runtime_db_path=str(directory/'runtime.sqlite3'),
            analysis_chart_dir=str(directory/'charts'),mexc_api_key=account.mexc_api_key,
            mexc_api_secret=account.mexc_api_secret,telegram_bot_token='',telegram_chat_id=''))
        store=RuntimeStore(settings.runtime_db_path)
        if self.private_factory is None:
            from trading.exchange.mexc_private import MexcPrivateClient
            private=MexcPrivateClient(account.mexc_api_key,account.mexc_api_secret,writes_enabled=ident in self.allowed)
        else:
            private=self.private_factory(account,ident in self.allowed)
        if self.public_factory is None:
            from trading.exchange.mexc_public import MexcPublicClient
            public=MexcPublicClient(base_url=settings.mexc_base_url,timeout_seconds=3)
        else:
            public=self.public_factory(account)
        if self.llm_factory is None:
            from trading.agents.provider_pool import ProviderPool
            from trading.agents.provider_router import ProviderRouter
            from trading.runtime.budget import Budget
            cls=ProviderPool if settings.ai_provider_pool_enabled else ProviderRouter
            llm=cls(settings,Budget(self.budget_store,settings),self.budget_store)
        else:
            llm=self.llm_factory(settings)
        factory=self.runner_factory
        if factory is None:
            from trading.runtime.live_runner import LiveRunner
            factory=LiveRunner
        executor=None
        if self.executor_factory is not None or self.runner_factory is None:
            from trading.brokers.live.automatic import AutomaticLiveExecutor, AutomaticLivePolicy
            from trading.brokers.live.execution import LiveExecutionGates
            from trading.risk.engine import RiskEngine
            executor_cls=self.executor_factory or AutomaticLiveExecutor
            executor=executor_cls(AutomaticLivePolicy(enabled=True,allowed_accounts=tuple(self.allowed),
                operator_ids=(str(account.telegram_user_id),),max_positions=4),
                LiveExecutionGates(live_trading_enabled=True,broker_mode='live',
                    operator_ids=(str(account.telegram_user_id),),reviewed=True),
                store,private,public,RiskEngine(settings=settings),
                control_provider=lambda:self.entry_control(ident),account_id=ident,binding_id=binding,
                can_execute=lambda:self.entry_control(ident)['enabled'],
                allowed_capital=self._allowed_capital(ident))
        runner=factory(settings,store=store,account_id=ident,binding_id=binding,private=private,
            public=public,llm=llm,external_control=lambda:self.entry_control(ident),executor=executor)
        llm.can_call=lambda: self.entry_control(ident)['enabled']
        self.runners[ident]=runner
        self.control_store.put(f'account:{ident}:linked',True)
        return runner

    def _allowed_capital(self, ident):
        control=self.controller.control(ident)
        return None if control.get('allocation_mode')=='full' else control.get('capital_amount')

    def sync_grant(self, account):
        ident=account.account_id
        runner=self.runners.get(ident)
        executor=getattr(runner,'executor',None)
        if executor is None:
            return
        control=self.entry_control(ident)
        if not control['enabled']:
            executor.revoke_start()
            return
        revision=control.get('updated_at')
        key=f'account:{ident}:live_grant_revision'
        if revision is None or self.control_store.get(key)==revision:
            return
        # Exactly one grant attempt per authenticated Start revision, never a loop
        # extending expiry. Changing settings/keys requires another explicit Start.
        self.control_store.put(key,revision)
        executor.grant_start(account.telegram_user_id,revision)

    def _busy(self, ident):
        return any(aid==ident and not future.done() for (aid,_),future in self.jobs.items())

    def idle_account(self, ident):
        self.controller.set_control(ident,enabled=False)
        if self._busy(ident):
            raise ConnectionIssue('Canlı hesap işlemi sürüyor; tamamlanınca yeniden dene.')
        runner=self.runners.get(ident)
        if runner:
            runner.monitor()  # Refresh exchange state even while entries are paused.
            snapshot=runner.status('ACCOUNT_CHANGE_CHECK')
            if snapshot.get('positions'):
                raise ConnectionIssue('Canlı pozisyonlar kapanmadan hesap değiştirilemez.')
            # Unknown state must never authorize deleting the credential used for recovery.
            checker=getattr(runner,'can_disconnect',None)
            if checker is None or checker() is not True:
                raise ConnectionIssue('Canlı emir uzlaştırması tamamlanmadan hesap değiştirilemez.')

    def bind_account(self, ident, key, secret):
        if ident not in self.allowed:
            raise ConnectionIssue('Bu hesap sunucunun canlı işlem izin listesinde değil.')
        self.idle_account(ident)
        from trading.exchange.mexc_private import MexcPrivateClient
        from trading.runtime.live_account import LiveAccountReconciler
        client=MexcPrivateClient(key,secret,writes_enabled=False)
        try:
            probe=LiveAccountReconciler(ident,RuntimeStore(':memory:'),binding_id='connection-check')
            snapshot=probe.refresh(client)
            if not snapshot.get('ready'):
                raise ConnectionIssue('MEXC canlı hesap okuması doğrulanamadı.')
        finally:
            client.close()
        self.vault.put(ident,key,secret)
        old=self.runners.pop(ident,None)
        if old:
            old.close()
        self.control_store.put(f'account:{ident}:live_binding',uuid4().hex)
        self.control_store.put(f'account:{ident}:disconnected',False)
        self.controller.set_control(ident,enabled=False,stopped=True,capital_chosen=False)
        account=next(a for a in self.accounts if a.account_id==ident)
        self._create_runner(replace(account,mexc_api_key=key,mexc_api_secret=secret))

    def disconnect_account(self, ident):
        self.idle_account(ident)
        old=self.runners.pop(ident,None)
        if old:
            old.close()
        self.vault.delete(ident)
        self.control_store.put(f'account:{ident}:disconnected',True)
        self.control_store.put(f'account:{ident}:linked',False)
        self.control_store.put(f'account:{ident}:live_binding',uuid4().hex)
        self.controller.set_control(ident,enabled=False,stopped=True,capital_chosen=False)

    def apply_capital(self, account, command):
        ident=account.account_id
        self.idle_account(ident)
        if ident not in self.allowed or command.get('mode')!='account':
            raise ConnectionIssue('Canlı modda sanal kasa kullanılamaz.')
        runner=self.runners.get(ident)
        if runner is None:
            raise ConnectionIssue('Önce MEXC hesabını bağla.')
        snap=runner.status('ALLOCATION_CHECK')
        actual=snap.get('live_account') or snap.get('account') or snap.get('real_account') or {}
        balance=actual.get('balance') or actual
        available=balance.get('available',snap.get('available'))
        import math
        if not actual.get('ready') or not isinstance(available,(int,float)) or not math.isfinite(available) or available<=0:
            raise ConnectionIssue('Güncel kullanılabilir futures bakiyesi doğrulanamadı.')
        full=command.get('allocation_mode')=='full'
        amount=None if full else command.get('amount')
        if not full and (isinstance(amount,bool) or not isinstance(amount,(int,float)) or not math.isfinite(amount) or not 0<amount<=available):
            raise ConnectionIssue('Ayrılacak USDT tutarı kullanılabilir bakiyeyi aşamaz.')
        self.controller.set_control(ident,enabled=False,stopped=True,capital_mode='account',
            allocation_mode='full' if full else 'fixed',capital_amount=amount,
            capital_chosen=True,capital_pending=None,capital_input=False)
        self.controller.enqueue(ident,'CANLI hesap limiti seçildi. Analiz sürümü ve trader ayarlarını seçip Başlat ile onayla.',
            event_id='live-capital:'+command['id'],buttons=True)

    def review_halt(self,ident,command):
        from trading.control.live_rearm import acknowledge
        from trading.exchange.mexc_private import MexcPrivateClient

        runner=self.runners.get(ident)
        control=self.controller.control(ident)
        if (ident not in self.allowed or runner is None or control.get('enabled')
                or not control.get('stopped') or control.get('updated_at')!=command.get('control_revision')):
            raise ConnectionIssue('Hesap veya ayarlar değişti. Durum ekranından kontrolü yeniden başlat.')
        credentials=self.vault.get(ident)
        if not credentials or self.control_store.get(f'account:{ident}:live_binding')!=runner.binding_id:
            raise ConnectionIssue('MEXC hesap bağlantısı doğrulanamadı. Yeni giriş engeli korunuyor.')
        client=MexcPrivateClient(credentials.api_key,credentials.api_secret,writes_enabled=False)
        try:
            result=acknowledge(runner.store,self.control_store,client,ident,allow_protected_positions=True,
                               expected_intent_id=command['intent_id'])
        except ValueError as exc:
            labels={
                'halt_changed':'Acil kapanış kaydı değişti.',
                'emergency_close_not_final':'MEXC kapanış sonucu henüz kesinleşmedi.',
                'unresolved_intent':'Sonuçlanmamış emir veya işlem kaydı var.',
                'exchange_orders_present':'MEXC’de bekleyen emir var.',
                'exchange_positions_not_owned':'MEXC’de yerel kayıtla eşleşmeyen pozisyon var.',
                'protected_position_not_verified':'Açık pozisyonun borsa koruması doğrulanamadı.',
            }
            raise ConnectionIssue(labels.get(str(exc),'MEXC uzlaştırma kontrolü geçmedi.')
                                  +' Yeni giriş engeli korunuyor.') from None
        finally:
            client.close()
        self.controller.enqueue(ident,
            f"MEXC kontrolü tamamlandı. Doğrulanmış korumalı açık pozisyon: {result['verified_protected_positions']}. "
            "Giriş engeli kaldırıldı. Canlı arayışı tekrar başlatmak için Başlat düğmesine bas.",
            event_id='live-review-halt:'+command['id'],buttons=True)

    def execute_commands(self, account):
        ident=account.account_id
        key=f'account:{ident}:commands'
        commands=self.control_store.get(key,[])
        for command in commands:
            if command.get('status')!='pending' or self._busy(ident):
                continue
            try:
                if command.get('kind')=='capital_reset':
                    self.apply_capital(account,command)
                elif command.get('kind')=='review_halt':
                    self.review_halt(ident,command)
                elif command.get('kind')=='close':
                    runner=self.runners.get(ident)
                    if runner is None or not callable(getattr(runner,'request_close',None)):
                        raise ConnectionIssue('Canlı kapatma yürütücüsü hazır değil; emir gönderilmedi.')
                    runner.request_close(command)
                else:
                    raise ConnectionIssue('Desteklenmeyen canlı hesap komutu.')
                command['status']='done'
            except Exception as exc:
                command['status']='failed'
                self.controller.set_control(ident,enabled=False,capital_pending=None)
                self.controller.enqueue(ident,str(exc) if isinstance(exc,ConnectionIssue) else 'Canlı komut doğrulanamadı; yeni girişler durduruldu.',event_id='live-command:'+command['id'],buttons=command.get('kind')=='review_halt')
            self.control_store.put(key,commands)

    def publish(self, account):
        ident=account.account_id
        runner=self.runners.get(ident)
        snapshot=copy.deepcopy(runner.status(runner.store.get('status',{}).get('reason','LIVE_STATUS')) if runner else dict(mode='LIVE',reason='ACCOUNT_UNAVAILABLE',positions={},account={'ready':False}))
        snapshot['mode']='LIVE'
        snapshot['price_planning_enabled']=self.cfg.live_price_plan_enabled
        snapshot['position_management_enabled']=self.cfg.live_position_management_enabled
        snapshot['control']=self.controller.control(ident)
        snapshot['available_runner_ids']=self.available_runner_ids(ident)
        snapshot['version']=snapshot['control'].get('version','text')
        snapshot['ai_budget']=self.budget_store.get('ai_budget',{})
        snapshot['ai_request_stats']=self.budget_store.get('ai_request_stats',{})
        llm=getattr(runner,'llm',None)
        if callable(getattr(llm,'snapshot',None)):snapshot['provider_pool']=llm.snapshot()
        if runner is not None:
            grant=runner.store.get('live:automatic_grant:'+ident,{})
            snapshot['automatic_grant']={key:grant.get(key) for key in ('expires_at','revoked','control_revision')}
        now=time.time()
        delayed=[]
        for kind in ('analysis','scan','monitor','management'):
            job=self.jobs.get((ident,kind))
            began=self.last_schedule.get((ident,kind))
            limit=max(180,3*self.cfg.worker_loop_interval_seconds,2*self.cfg.ai_provider_timeout_seconds+30)
            if job is not None and not job.done() and began is not None and now-began>limit:
                delayed.append(dict(kind=kind,age_seconds=now-began))
        snapshot['worker_health']=dict(observed_at=now,delayed_jobs=delayed)
        self.control_store.put(f'account:{ident}:status',snapshot)
        from trading.control.activity import publish_activity
        publish_activity(self.controller,self.control_store,ident,snapshot,self.cfg,now)
        if runner is not None:
            self._publish_entry_rejections(ident, runner)
        executor=getattr(runner,'executor',None)
        if executor is not None and callable(getattr(executor,'intents',None)):
            halt=snapshot.get('live_halt') or {}
            for row in executor.intents():
                self._publish_intent(ident,row)
                self._publish_management(ident,row)
                if halt.get('requires_review') is True and halt.get('intent_id')==row.get('intent_id'):
                    self.controller.enqueue(ident,
                        f"CANLI | {row['proposal']['symbol']} | Yeni girişler durduruldu.\n"
                        +EMERGENCY_REASONS.get(row.get('halt_reason'),'Acil kapanış teknik kontrol gerektiriyor.')
                        +"\nMEXC durumunu aşağıdaki düğmeyle kontrol et. Yeniden Başlat kararı sende.",
                        event_id=f"live-halt-review:{row['binding_id']}:{row['intent_id']}",buttons=True)
        return snapshot

    def _publish_entry_rejections(self, ident, runner):
        key = 'live:entry_rejection_notices'
        with runner.store.lock:
            notices = runner.store.get(key, [])
            for notice in notices:
                if notice['binding_id'] != getattr(runner, 'binding_id', None):
                    continue
                reason = notice['reason']
                details = notice['details']
                codes = tuple(details.get('reason_codes', ()))
                explanation = ' '.join(ENTRY_REJECTION_DETAILS[code] for code in codes
                                       if code in ENTRY_REJECTION_DETAILS)
                if not explanation:
                    explanation = ENTRY_REJECTION_REASONS.get(reason) or human_reason(reason)
                drift = details.get('price_drift')
                if drift:
                    stage = 'Plan kontrolü' if drift['stage'] == 'planning' else 'Emir öncesi'
                    explanation += (f"\n{stage} | Plan {drift['planned']:.10g} | Güncel {drift['observed']:.10g}"
                                    f" | Sapma {drift['deviation']:+.3%} | Sınır {drift['tolerance']:.3%}")
                contracts = details.get('contracts', {})
                if {'quantity', 'minimum', 'maximum'} <= contracts.keys():
                    explanation += (f" Hesaplanan {contracts['quantity']:g} kontrat, "
                                    f"izin verilen {contracts['minimum']:g}–{contracts['maximum']:g}.")
                symbol = notice['symbol']
                bucket = int(notice['at'] // 300)
                event_id = f"live-entry-rejection:{notice['binding_id']}:{symbol}:{reason}:{','.join(codes)}:{bucket}"
                self.controller.enqueue(ident, f"CANLI | {symbol} | İşlem önerisi reddedildi.\n{explanation}",
                                        event_id=event_id)
            if notices:
                runner.store.put(key, [])

    def _publish_intent(self, ident, row):
        labels={'protected':'İşlem açıldı; borsada SL/TP koruması doğrulandı.',
            'closed':'İşlem kapandı; MEXC kapanışı doğrulandı.',
            'entry_rejected':'MEXC işlem isteğini reddetti; işlem açıldığı doğrulanmadı.',
            'close_write_unknown':'Kapatma isteği gönderildi; MEXC sonucu uzlaştırılıyor.',
            'manual_close_unknown':'Manuel kapatma isteği gönderildi; MEXC sonucu uzlaştırılıyor.',
            'entry_write_unknown':'Emir sonucu belirsiz; hesap uzlaştırılıyor, yeni girişler bekliyor.',
            'emergency_close_unknown':'Koruma doğrulanamadı; acil kapatma sonucu uzlaştırılıyor.',
            'entry_filled_unprotected':'Dolum tespit edildi; yerel borsa koruması henüz doğrulanmadı.'}
        state=row.get('status')
        if state not in labels:
            return
        proposal=row.get('proposal') or {}
        metadata=proposal.get('metadata') or {}
        text=f"CANLI | {ident} | {proposal.get('symbol','?')}\n{labels[state]}\n"
        if metadata.get('runner_id') in STRUCTURAL_IOC_RUNNERS:
            text+=f"Runner: {metadata['runner_id'][-1]} · Fiyat sınırlı IOC\n"
            horizon=row.get('trade_horizon') or metadata.get('trade_horizon') or {}
            labels_by_horizon={'scalp':'Kısa vade','intraday':'Gün içi','swing':'Swing'}
            if horizon.get('style') in labels_by_horizon:
                text+=f"İşlem ufku: {labels_by_horizon[horizon['style']]} | İnceleme: {horizon['review_timeframe']}\n"
        if state=='entry_rejected':
            rejection=row.get('entry_rejection') or {}
            stages={'submit_market':'Emir gönderimi','submit_ioc':'Fiyat sınırlı IOC emir gönderimi','order_reconciliation':'Emir sonucu kontrolü'}
            text+='Ret aşaması: '+stages.get(rejection.get('stage'),'Eski kayıtta belirtilmemiş')+'\n'
            for label,key in [('MEXC hata kodu','exchange_code'),('HTTP','http_status'),('Borsa emir durumu','order_state')]:
                value=rejection.get(key)
                if type(value) is int:text+=f'{label}: {value}\n'
            if not rejection:text+='Kesin ret nedeni eski kayıtta saklanmamış.\n'
        if state in ('protected','entry_filled_unprotected'):
            stop=proposal.get('stop_loss') or {}
            text+=(f"Yön: {proposal.get('side','?')} | Kaldıraç: {proposal.get('leverage','?')}x\n"
                f"Gerçekleşen giriş: {row.get('entry_fill_price','doğrulanmadı')} | SL: {stop.get('price','?')} | TP: {proposal.get('take_profit_price','?')}\n")
            metrics=row.get('entry_metrics') or {}
            if metrics:
                text+=(f"Dolan pozisyon: {metrics['filled_notional_usdt']:.4f} USDT | "
                       f"Tahmini başlangıç teminatı: {metrics['estimated_initial_margin_usdt']:.4f} USDT\n"
                       f"İlk stop riski (tahmini): {metrics['initial_stop_risk_usdt']:.4f} USDT\n")
                target_r=metrics.get('estimated_target_r')
                if target_r is not None:
                    text+=(f"TP net tahmini: {metrics['estimated_target_net_usdt']:+.4f} USDT "
                           f"({target_r:+.2f}R, ücret ve kayma varsayımıyla)\n")
            allocation=metadata.get('runner3_allocation')
            if allocation:
                factor={'model_stop_budget':'modelin stop bütçesi','stop_budget_with_costs':'maliyet dahil stop bütçesi',
                        'available_margin':'boş marjin','runner3_margin_policy':'teminat politikası'}[allocation['limiting_factor']]
                quality='Güçlü' if allocation['grade']=='strong' else 'Standart'
                text+=f"Teminat hedefi: {allocation['target_margin_usdt']:.4f} USDT | Boyutu sınırlayan: {factor}\n"
                text+=f"Kanıt kalitesi: {quality} | Kalibre olasılık: ölçülmedi\n"
                if 'model_risk_fraction' in allocation:
                    text+=(f"Model riski: {allocation['model_risk_fraction']:.2%} | "
                           f"Uygulanan stop bütçesi: {allocation['effective_risk_fraction']:.2%} | "
                           f"İzin verilen tavan: {allocation['risk_ceiling']:.2%}\n")
                    if allocation.get('risk_boost_applied') and allocation.get('small_account_policy'):
                        text+='100 USDT altı kasa politikası stop bütçesini profil sınırı içinde yükseltti.\n'
            entry_timeframe=(metadata.get('price_plan') or {}).get('entry_timeframe')
            if entry_timeframe in ('5m','15m','4h'):
                text+=f"Giriş zaman dilimi: {entry_timeframe}\n"
            source='AI mum/yapı referanslı planı' if metadata.get('price_source')=='model' else 'algoritma seviyeleri'
            text+=f'Giriş/SL/TP kaynağı: {source}\n'
            if metadata.get('price_plan'):
                text+='Plan gerekçesi: '+str(metadata['price_plan'].get('reason',''))[:600]+'\n'
            ai=metadata.get('decision_ai') or {}
            text+=f"Kararı veren AI: {ai.get('provider','kayıtlı değil')} / {ai.get('model','kayıtlı değil')}\n"
        if state=='closed':
            text+=f"Kapanış nedeni: {row.get('close_reason','borsa kaydı')}\n"
            if row.get('close_reason')=='EMERGENCY':
                text+=EMERGENCY_REASONS.get(row.get('halt_reason'),'Acil kapanışın teknik ayrıntıları denetim kaydında.')+'\n'
                text+='Acil kapanış sonrası yeni giriş engeli kaldırılmaz; teknik inceleme gerekir.\n'
            if row.get('realized_complete') is True and isinstance(row.get('net_pnl'),(int,float)):
                text+=f"MEXC net sonuç: {row['net_pnl']:+.4f} USDT\n"
                initial_risk=(row.get('entry_metrics') or {}).get('initial_stop_risk_usdt')
                if isinstance(initial_risk,(int,float)) and initial_risk>0:
                    text+=f"Gerçekleşen sonuç: {row['net_pnl']/initial_risk:+.2f}R (ilk dolum riskine göre)\n"
            else:
                text+='Net sonuç henüz doğrulanmadı.\n'
        snapshot=self.control_store.get(f'account:{ident}:status',{})
        account=snapshot.get('live_account') or {}
        balance=account.get('balance') or snapshot.get('balance') or {}
        for label,key in [('Güncel equity','equity'),('Kullanılabilir marjin','available')]:
            value=balance.get(key)
            text+=f'{label}: {value:.4f} USDT\n' if isinstance(value,(int,float)) else f'{label}: doğrulanmadı\n'
        photos=[]
        if state=='protected':
            runner=self.runners.get(ident)
            chart_root=Path(runner.cfg.analysis_chart_dir).resolve() if runner else None
            for chart in metadata.get('charts',[])[:3]:
                if not isinstance(chart,str) or chart_root is None:
                    continue
                path=Path(chart).resolve()
                if path.is_relative_to(chart_root) and path.is_file():
                    photos.append(str(path))
        self.controller.enqueue(ident,text,photos=photos,event_id=f"live:{row.get('binding_id')}:{row.get('intent_id')}:{state}")

    def _publish_management(self,ident,row):
        proposal=row.get('proposal',{})
        for event in row.get('management_events',[]):
            labels={'move_stop':'AI stop değişikliği','move_target':'AI hedef değişikliği','reduce':'AI kısmi kapatma','close':'AI kapatma'}
            confirmed=event.get('status')=='confirmed'
            text=(f"CANLI | {proposal.get('symbol','?')} | {labels.get(event['action'],event['action'])}\n"
                +('Borsa sonucu doğrulandı.\n' if confirmed else 'Yönetim sırasında pozisyon borsada kapandı; değişiklik uygulanmış sayılmadı.\n' if event.get('status')=='position_closed' else 'Pozisyon koruması doğrulanamadı; acil kapatma sonucu uzlaştırılıyor.\n' if event.get('status')=='protection_failed' else 'İstek reddedildi; değişiklik doğrulanmadı.\n'))
            if event['action']=='move_stop':text+=f"SL: {event['old_stop']} → {event.get('new_stop','doğrulanmadı')} | TP: {event['target']}\n"
            elif event['action']=='move_target':text+=f"TP: {event['old_target']} → {event.get('new_target','doğrulanmadı')} | SL: {event['old_stop']}\n"
            else:
                text+=f"Kapanan kontrat: {event.get('filled_contracts','doğrulanmadı')} | Kalan: {event.get('remaining_contracts','doğrulanmadı')}\n"
                if event['action']=='reduce' and confirmed:text+='Kısmi kapanışın ayrı net kârı henüz hesaplanmadı; kasa borsadan okunur.\n'
            ai=event.get('ai',{})
            text+=f"Gerekçe: {event['reason']}\nModel: {ai.get('provider','?')} / {ai.get('model','?')}\n"
            snapshot=self.control_store.get(f'account:{ident}:status',{})
            balance=(snapshot.get('live_account') or {}).get('balance',{})
            if isinstance(balance.get('equity'),(int,float)):text+=f"Son gözlenen kasa: {balance['equity']:.4f} USDT\n"
            positions=snapshot.get('positions',{})
            observed=next((p for p in positions.values() if str(p.get('position_id'))==row.get('position_id')),None)
            if observed is not None:text+=f"Son gözlenen açık PnL: {observed.get('unrealized','doğrulanmadı')} USDT\n"
            self.controller.enqueue(ident,text,event_id=f"live-management:{row.get('binding_id')}:{row['intent_id']}:{event['decision_id']}")
        pending=row.get('management_pending')
        if pending:
            self.controller.enqueue(ident,f"CANLI | {proposal.get('symbol','?')}\nAI yönetim isteğinin borsa sonucu henüz doğrulanmadı. "
                'Aynı emir tekrar gönderilmiyor; yeni girişler sonuç uzlaştırılana kadar bekliyor.',
                event_id=f"live-management-pending:{row.get('binding_id')}:{row['intent_id']}:{pending['decision_id']}")

    def start(self):
        self.started=True

    def tick(self):
        if self.closed:
            return
        self.controller.tick()
        now=time.time()
        for account in self.accounts:
            ident=account.account_id
            for kind in ('analysis','scan','monitor','management'):
                future=self.jobs.get((ident,kind))
                if future and future.done():
                    try:
                        future.result()
                    except Exception:
                        self.controller.set_control(ident,enabled=False)
                        self.controller.enqueue(ident,'Canlı hesap görevi başarısız; yeni girişler durduruldu. Hesap uzlaştırması yeniden denenecek.',event_id=f'live-job:{ident}:{kind}:{int(now//60)}')
            self.execute_commands(account)
            runner=self.runners.get(ident)
            if runner is not None:
                try:
                    self.sync_grant(account)
                except Exception:
                    self.controller.set_control(ident,enabled=False)
                    self.controller.enqueue(ident,'Canlı başlatma izni doğrulanamadı; yeniden Başlat ile onayla.',event_id=f'live-grant-error:{ident}:{int(now)}')
                permission=self.entry_control(ident)
                for kind,method,interval,enabled in (
                    ('monitor',runner.monitor,max(1,getattr(self.cfg,'position_poll_seconds',5)),True),
                    ('management',getattr(runner,'manage_positions',lambda:None),max(1,getattr(self.cfg,'position_poll_seconds',5)),permission['enabled'] and self.cfg.live_position_management_enabled),
                    ('scan',runner.scan_market,max(1,self.cfg.worker_loop_interval_seconds),permission['enabled']),
                    ('analysis',runner.cycle,max(1,getattr(self.cfg,'position_poll_seconds',5)),permission['enabled'])):
                    key=(ident,kind)
                    future=self.jobs.get(key)
                    if enabled and (future is None or future.done()) and now-self.last_schedule.get(key,0)>=interval:
                        self.last_schedule[key]=now
                        self.jobs[key]=self.pool.submit(method)
            self.publish(account)

    def stop(self):
        self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
        for runner in self.runners.values():
            runner.close()
