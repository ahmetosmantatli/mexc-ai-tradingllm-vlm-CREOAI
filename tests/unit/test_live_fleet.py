from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from trading.config import Settings
from trading.control.accounts import Account
from trading.runtime.live_fleet import LiveFleet
from trading.runtime.store import RuntimeStore


class Controller:
    def __init__(self,token,accounts,store):
        self.store=store
        self.messages=[]
    def dispatch(self,update): pass
    def tick(self): pass
    def control(self,ident):
        return dict(self.store.get(f'account:{ident}:control',{}))
    def set_control(self,ident,**updates):
        row=self.control(ident); row.update(updates)
        import time
        row['updated_at']=time.time()
        self.store.put(f'account:{ident}:control',row)
    def enqueue(self,*args,**kw): self.messages.append((args,kw))


class Runner:
    def __init__(self,cfg,**kw):
        self.cfg=cfg; self.store=kw['store']; self.executor=kw.get('executor')
        self.external_control=kw['external_control']; self.calls=[]
        self.positions={}; self.safe=True
    def monitor(self): self.calls.append('monitor')
    def scan_market(self): self.calls.append('scan')
    def cycle(self):
        assert self.external_control()['enabled']
        self.calls.append('analysis')
    def status(self,reason):
        return dict(mode='LIVE',reason=reason,equity=1234,available=900,
            positions=self.positions,account=dict(ready=True,equity=1234,available=900))
    def can_disconnect(self): return self.safe
    def close(self): self.calls.append('close')


def settings(tmp_path,**updates):
    values=dict(_env_file=None,broker_mode='live',live_trading_enabled=True,
        live_automatic_enabled=True,live_allowed_accounts=('a',),
        live_readiness_ack='I_UNDERSTAND_REAL_ORDERS',telegram_bot_token='test',
        account_vault_key=Fernet.generate_key().decode(),
        runtime_db_path=str(tmp_path/'runtime.sqlite3'),shared_budget_db_path=str(tmp_path/'budget.sqlite3'))
    values.update(updates)
    return Settings(**values)


def fleet(tmp_path,**updates):
    return LiveFleet(settings(tmp_path,**updates),accounts=[Account('a',1,1,'test-key','test-secret')],
        controller_factory=Controller,runner_factory=Runner,
        private_factory=lambda *args:SimpleNamespace(),public_factory=lambda *args:SimpleNamespace(),
        llm_factory=lambda *args:SimpleNamespace())


@pytest.mark.parametrize('change',[dict(live_automatic_enabled=False),dict(live_trading_enabled=False),
    dict(live_allowed_accounts=()),dict(live_readiness_ack=''),dict(broker_mode='paper')])
def test_live_startup_requires_every_explicit_gate(tmp_path,change):
    with pytest.raises(ValueError): fleet(tmp_path,**change)


def test_first_live_boot_does_not_reuse_running_paper_control(tmp_path):
    store=RuntimeStore(str(tmp_path/'control.sqlite3'))
    store.put('account:a:control',dict(enabled=True,capital_mode='demo',capital_chosen=True))
    f=fleet(tmp_path)
    try:
        assert not f.entry_control('a')['enabled']
        assert f.controller.control('a')['stopped']
        assert len(f.runners)==1
        assert not hasattr(f.runners['a'],'ledger')
    finally: f.stop()


def test_live_delayed_job_is_visible_and_notified_without_changing_entry_control(tmp_path):
    import time
    f=fleet(tmp_path)
    try:
        f.controller.set_control('a',enabled=True,stopped=False)
        f.jobs[('a','analysis')]=SimpleNamespace(done=lambda:False)
        f.last_schedule[('a','analysis')]=time.time()-400
        result=f.publish(f.accounts[0])
        assert result['worker_health']['delayed_jobs'][0]['kind']=='analysis'
        assert 'Geciken görev: AI analizi' in f.controller.messages[-1][0][1]
        assert f.controller.control('a')['enabled']
    finally:f.stop()


def test_paused_live_continues_monitor_only_and_publishes_actual_balance(tmp_path):
    f=fleet(tmp_path)
    try:
        f.tick()
        for job in f.jobs.values(): job.result(timeout=2)
        assert f.runners['a'].calls==['monitor']
        snapshot=f.publish(f.accounts[0])
        assert snapshot['mode']=='LIVE'
        assert snapshot['equity']==1234
        assert snapshot['available']==900
        assert 'paper' not in snapshot
    finally: f.stop()


def test_live_capital_rejects_demo_and_uses_dynamic_full_balance(tmp_path):
    f=fleet(tmp_path)
    try:
        with pytest.raises(Exception):
            f.apply_capital(f.accounts[0],dict(id='demo',mode='demo',amount=500))
        f.apply_capital(f.accounts[0],dict(id='full',mode='account',allocation_mode='full'))
        control=f.controller.control('a')
        assert control['capital_amount'] is None
        assert control['allocation_mode']=='full'
        assert not control['enabled']
    finally: f.stop()


def test_live_disconnect_refuses_open_or_uncertain_execution(tmp_path):
    f=fleet(tmp_path)
    try:
        f.runners['a'].positions={'BTC_USDT':{'positionId':4}}
        with pytest.raises(Exception): f.disconnect_account('a')
        f.runners['a'].positions={}; f.runners['a'].safe=False
        with pytest.raises(Exception): f.disconnect_account('a')
        assert 'a' in f.runners
        assert not f.control_store.get('account:a:disconnected',False)
    finally: f.stop()


def test_stopped_or_unlisted_account_never_runs_ai_jobs(tmp_path):
    f=fleet(tmp_path)
    try:
        f.controller.set_control('a',enabled=True,stopped=True,capital_chosen=True,
            capital_mode='account',version_chosen=True,profile_chosen=True)
        f.tick()
        for job in f.jobs.values(): job.result(timeout=2)
        assert 'analysis' not in f.runners['a'].calls
        assert 'scan' not in f.runners['a'].calls
    finally: f.stop()


def test_explicit_start_grant_is_not_repeated_or_extended(tmp_path):
    f=fleet(tmp_path)
    calls=[]
    f.runners['a'].executor=SimpleNamespace(grant_start=lambda *args:calls.append(args),revoke_start=lambda:calls.append('revoke'))
    try:
        f.controller.set_control('a',enabled=True,stopped=False,capital_chosen=True,
            capital_mode='account',version_chosen=True,profile_chosen=True)
        f.sync_grant(f.accounts[0]); f.sync_grant(f.accounts[0])
        assert len(calls)==1
        assert calls[0][0]==1
        f.controller.set_control('a',enabled=False)
        f.sync_grant(f.accounts[0])
        assert calls[-1]=='revoke'
    finally: f.stop()


def test_native_events_distinguish_pending_from_open_and_use_actual_net(tmp_path):
    f=fleet(tmp_path)
    try:
        row=dict(binding_id='generation',intent_id='intent',status='entry_write_unknown',
            proposal=dict(symbol='BTC_USDT',metadata={'decision_ai':{'provider':'gemini','model':'vision'}}))
        f._publish_intent('a',row)
        assert 'sonucu belirsiz' in f.controller.messages[-1][0][1]
        assert 'İşlem açıldı' not in f.controller.messages[-1][0][1]
        row.update(status='closed',net_pnl=12.3,realized_complete=True)
        f._publish_intent('a',row)
        assert '+12.3000 USDT' in f.controller.messages[-1][0][1]
        assert f.controller.messages[-1][1]['event_id']=='live:generation:intent:closed'
    finally: f.stop()


def test_entry_rejection_notification_includes_numeric_exchange_error(tmp_path):
    f=fleet(tmp_path)
    try:
        row=dict(binding_id='generation',intent_id='rejected',status='entry_rejected',
            proposal=dict(symbol='INJ_USDT'),entry_rejection=dict(
                stage='submit_market',exchange_code=2001,http_status=200,
                message='secret response must not be published'))
        f._publish_intent('a',row)
        text=f.controller.messages[-1][0][1]
        assert 'MEXC hata kodu: 2001' in text
        assert 'HTTP: 200' in text
        assert 'Emir gönderimi' in text
        assert 'secret response' not in text
        row.update(intent_id='old',entry_rejection={})
        f._publish_intent('a',row)
        assert 'eski kayıtta saklanmamış' in f.controller.messages[-1][0][1]
    finally:f.stop()


def test_default_live_runner_and_executor_construct_without_network(tmp_path):
    f=LiveFleet(settings(tmp_path),accounts=[Account('a',1,1,'test-key','test-secret')],
        controller_factory=Controller,private_factory=lambda *args:SimpleNamespace(writes_enabled=True),
        public_factory=lambda *args:SimpleNamespace(),llm_factory=lambda *args:SimpleNamespace())
    try:
        from trading.runtime.live_runner import LiveRunner
        from trading.brokers.live.automatic import AutomaticLiveExecutor
        assert isinstance(f.runners['a'],LiveRunner)
        assert isinstance(f.runners['a'].executor,AutomaticLiveExecutor)
        assert f.runners['a'].binding_id==f.runners['a'].executor.binding_id
        assert f.publish(f.accounts[0])['mode']=='LIVE'
    finally: f.stop()


def test_live_paper_live_roundtrip_revokes_old_start_and_pending_commands(tmp_path):
    first=fleet(tmp_path)
    vault_key=first.cfg.account_vault_key
    try:
        first.controller.set_control('a',enabled=True,stopped=False,capital_chosen=True,
            capital_mode='account',version_chosen=True,profile_chosen=True)
        assert first.entry_control('a')['enabled']
    finally:first.stop()
    control=RuntimeStore(str(tmp_path/'control.sqlite3'))
    # PAPER startup marks the same persisted account control namespace.
    control.put('account:a:worker_mode','paper')
    control.put('account:a:commands',[{'id':'paper-close','kind':'close','status':'pending'}])
    control.put('account:a:live_grant_revision',123)
    second=fleet(tmp_path,account_vault_key=vault_key)
    try:
        assert not second.entry_control('a')['enabled']
        assert second.controller.control('a')['stopped']
        assert second.control_store.get('account:a:live_grant_revision') is None
        assert second.control_store.get('account:a:commands')[0]['status']=='cancelled_mode_change'
    finally:second.stop()


def test_management_job_has_its_own_schedule_and_stops_with_ai(tmp_path):
    f=fleet(tmp_path,live_position_management_enabled=True)
    f.runners['a'].manage_positions=lambda:f.runners['a'].calls.append('management')
    try:
        f.controller.set_control('a',enabled=True,stopped=False,capital_chosen=True,
            capital_mode='account',version_chosen=True,profile_chosen=True)
        f.tick()
        for job in f.jobs.values():job.result(timeout=2)
        assert ('a','management') in f.jobs
        assert 'management' in f.runners['a'].calls and 'monitor' in f.runners['a'].calls
        f.controller.set_control('a',enabled=False,stopped=True)
        f.last_schedule={};f.tick()
        for job in f.jobs.values():job.result(timeout=2)
        assert f.runners['a'].calls.count('management')==1
        assert f.runners['a'].calls.count('monitor')==2
    finally:f.stop()


def test_management_message_distinguishes_confirmed_stop_from_uncertain_action(tmp_path):
    f=fleet(tmp_path)
    row=dict(binding_id='b',intent_id='i',proposal=dict(symbol='BTC_USDT'),
        management_events=[dict(decision_id='d',action='move_stop',status='confirmed',reason='Yeni destek teyit edildi.',
            old_stop=98,new_stop=99,target=104,ai=dict(provider='gemini',model='test'))])
    try:
        f._publish_management('a',row)
        text=f.controller.messages[-1][0][1]
        assert '98 → 99' in text and 'gemini / test' in text and 'doğrulandı' in text
        assert f.controller.messages[-1][1]['event_id']=='live-management:b:i:d'
        row['management_events']=[];row['management_pending']=dict(decision_id='pending')
        f._publish_management('a',row)
        assert 'henüz doğrulanmadı' in f.controller.messages[-1][0][1]
    finally:f.stop()


def test_three_slow_jobs_do_not_starve_next_monitor_cycle(tmp_path):
    import threading
    f=fleet(tmp_path,live_position_management_enabled=True)
    release=threading.Event();started=[threading.Event() for _ in range(3)]
    def slow(index):
        started[index].set();release.wait(3)
    runner=f.runners['a']
    runner.scan_market=lambda:slow(0)
    runner.cycle=lambda:slow(1)
    runner.manage_positions=lambda:slow(2)
    try:
        f.controller.set_control('a',enabled=True,stopped=False,capital_chosen=True,
            capital_mode='account',version_chosen=True,profile_chosen=True)
        f.tick();f.jobs[('a','monitor')].result(timeout=1)
        assert all(event.wait(1) for event in started)
        f.last_schedule[('a','monitor')]=0
        f.tick()
        f.jobs[('a','monitor')].result(timeout=1)
        assert runner.calls.count('monitor')==2
    finally:
        release.set();f.stop()
