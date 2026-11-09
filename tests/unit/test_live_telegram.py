import json
import httpx
from trading.control.accounts import Account
from trading.control.live_telegram import LiveTelegramController
from trading.runtime.store import RuntimeStore


def setup(tmp_path,snapshots=None):
    calls=[]
    def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'ok':True,'result':[]})
    accounts=[Account('a',11,11,'',''),Account('b',22,22,'','')]
    controller=LiveTelegramController('fake',accounts,RuntimeStore(str(tmp_path/'control')),
        snapshot_provider=lambda ident:(snapshots or {}).get(ident,{}),client=httpx.Client(transport=httpx.MockTransport(send)))
    return controller,calls


def ready(controller):
    controller.store.put('account:a:linked',True)
    controller.set_control('a',capital_chosen=True,capital_pending=None,capital_mode='account',allocation_mode='full',
        version='text',version_chosen=True,profile_chosen=True,risk_profile='balanced')


def test_live_setup_has_no_demo_or_paper_cash_and_requires_explicit_start(tmp_path):
    ctl,calls=setup(tmp_path)
    ctl._command('a','start')
    assert not ctl.control('a')['enabled']
    assert 'PAPER' not in ctl.settings_text('a')
    ready(ctl)
    ctl._command('a','start')
    assert ctl.control('a')['enabled']
    assert 'işlem başına Telegram onayı beklemez' in ctl.settings_text('a')
    ctl._command('a','vlm')
    assert not ctl.control('a')['enabled']
    ctl._command('a','trader_aggressive')
    assert not ctl.control('a')['enabled']


def test_live_settings_explain_weekly_disabled_without_peak_drop_limit(tmp_path):
    snapshot=dict(entry_risk_policy=dict(profile='aggressive_growth',daily_loss_limit_pct=.50,
        weekly_loss_limit_enabled=False,weekly_loss_limit_pct=.135,drawdown_limit_pct=.225))
    ctl,_=setup(tmp_path,{'a':snapshot})
    ready(ctl)
    ctl.set_control('a',risk_profile='aggressive_growth',risk_per_trade_pct=.03)
    text=ctl.settings_text('a')
    assert 'Günlük kayıp eşiği: %50' in text
    assert 'Haftalık kayıp durdurması kapalı.' in text
    assert 'Zirveden düşüş' not in text


def test_live_stop_blocks_ai_and_keeps_monitoring_description(tmp_path):
    ctl,calls=setup(tmp_path);ready(ctl)
    ctl._command('a','start');ctl._command('a','stop')
    assert ctl.control('a')['enabled'] is False and ctl.control('a')['stopped'] is True
    assert 'uzlaştırması sürer' in calls[-1]['text']


def test_live_balance_is_reported_without_static_seed_and_unknown_is_not_zero(tmp_path):
    ctl,calls=setup(tmp_path,{'a':{'mode':'LIVE','live_account':{'balance':{'equity':11.5854,'available':10.2,'unrealized':.3,'position_margin':1}}}})
    text=ctl.status_text('a')
    assert '11.5854' in text and '10.2000' in text and 'Sanal' not in text
    assert 'doğrulanmadı' in ctl.status_text('b')


def test_live_demo_rejected_and_full_capital_queues_no_fabricated_balance(tmp_path):
    ctl,calls=setup(tmp_path);ready(ctl)
    ctl.request_capital('a','demo',500)
    assert not ctl.store.get('account:a:commands',[])
    ctl.request_capital('a','account',allocation_mode='full')
    command=ctl.store.get('account:a:commands')[0]
    assert command['amount'] is None and command['allocation_mode']=='full'
    assert ctl.control('a')['enabled'] is False


def test_unauthorized_live_start_never_changes_account_state(tmp_path):
    ctl,calls=setup(tmp_path);ready(ctl)
    ctl.dispatch({'message':{'chat':{'id':33,'type':'private'},'from':{'id':33},'text':'/run'}})
    assert not ctl.control('a')['enabled'] and not ctl.control('b')['enabled']
    assert not calls


def test_native_close_button_does_not_require_missing_exchange_create_time(tmp_path):
    ctl,calls=setup(tmp_path,{'a':{'mode':'LIVE','positions':{'BTC_USDT':dict(symbol='BTC_USDT',intent_id='intent',position_id='42',analysis_version='text')}}})
    targets=ctl.position_targets('a')
    assert targets[0]['intent_id']=='intent' and targets[0]['position_id']=='42'
    ctl.position_buttons('a')
    assert calls[-1]['reply_markup']['inline_keyboard'][0][0]['text']=='Kapat: BTC_USDT'


def test_runner2_selection_requires_settings_and_fresh_start(tmp_path):
    ctl,calls=setup(tmp_path,{'a':{'available_runner_ids':['baseline-v1','runner2']}})
    ready(ctl)
    ctl._command('a','start')
    assert ctl.control('a')['enabled'] is True
    assert 'Runner 1' in ctl.status_text('a')
    ctl._command('a','runner:runner2')  # Stale selection buttons cannot change a running account.
    assert ctl.control('a')['enabled'] is True
    assert 'runner_id' not in ctl.control('a')

    ctl._command('a','settings')
    assert ctl.control('a')['enabled'] is False
    buttons=calls[-1]['reply_markup']['inline_keyboard'][0]
    assert [item['callback_data'] for item in buttons]==['control:runner:baseline-v1','control:runner:runner2']
    ctl._command('a','text')  # An older analysis-mode button cannot skip the choice.
    assert ctl.control('a')['version_chosen'] is False

    ctl._command('a','runner:runner2')
    assert ctl.control('a')['runner_id']=='runner2'
    assert 'runner_id' not in ctl.control('b')
    ctl._command('a','start')
    assert ctl.control('a')['enabled'] is False
    ctl._command('a','text')
    ctl._command('a','trader_aggressive')
    ctl._command('a','start')
    assert ctl.control('a')['enabled'] is True
    assert 'Runner 2' in ctl.status_text('a')


def test_unavailable_runner2_blocks_start_and_offers_baseline_recovery(tmp_path):
    ctl,calls=setup(tmp_path)
    ready(ctl)
    ctl.set_control('a',runner_id='runner2')
    ctl._command('a','start')
    assert ctl.control('a')['enabled'] is False
    assert 'kullanılamıyor' in calls[-1]['text']
    buttons=calls[-1]['reply_markup']['inline_keyboard'][0]
    assert [item['callback_data'] for item in buttons]==['control:runner:baseline-v1']
    ctl._command('a','runner:runner2')
    assert ctl.control('a')['enabled'] is False
    ctl._command('a','runner:baseline-v1')
    assert ctl.control('a')['runner_id']=='baseline-v1'
    assert ctl.control('a')['enabled'] is False


def test_live_emergency_button_pauses_and_queues_exact_review_without_start(tmp_path):
    intent='a'*32
    snapshot={'live_halt':dict(intent_id=intent,requires_review=True)}
    ctl,calls=setup(tmp_path,{'a':snapshot});ready(ctl)
    assert ctl.keyboard('a')['inline_keyboard'][0][0]['callback_data']==f'control:reviewhalt:{intent}'
    ctl._command('a','start')
    assert not ctl.control('a')['enabled']
    ctl._command('a','reviewhalt:'+'b'*32)
    assert not ctl.store.get('account:a:commands')
    ctl.set_control('a',enabled=True)
    ctl._command('a','reviewhalt:'+intent)
    ctl._command('a','reviewhalt:'+intent)
    queue=ctl.store.get('account:a:commands')
    assert len(queue)==1 and queue[0]['intent_id']==intent
    assert ctl.control('a')['stopped'] and not ctl.control('a')['enabled']
    snapshot.clear()
    ctl._command('a','start')
    assert ctl.control('a')['enabled']
