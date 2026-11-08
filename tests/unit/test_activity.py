from types import SimpleNamespace

from trading.control.activity import activity_summary,publish_activity
from trading.runtime.store import RuntimeStore


def snap(**kw):
    return dict(mode='LIVE',control=dict(enabled=True,stopped=False),positions={},**kw)


def test_watch_counts_are_plans_not_orders():
    rows=[dict(status='watching',expires_at=2000),dict(status='triggered',last_action='hold'),
          dict(status='reviewing'),dict(status='triggered'),dict(status='expired'),dict(status='watching',expires_at=100)]
    result=activity_summary(snap(watchlist=rows,scan_summary=dict(queue_size=7)),now=1000)
    assert (result['waiting'],result['review'],result['new_data'],result['queued'])==(1,2,1,7)
    assert 'açık emir değil' in result['text'] and result['category']=='review'


def test_capacity_wait_does_not_claim_every_quota_exhausted():
    result=activity_summary(snap(stage='model_provider_wait',analysis_capacity=dict(free_slots=0,paid_slots=0)))
    assert result['category']=='capacity'
    assert 'Bütün modellerin kotası bittiği anlamına gelmez' in result['text']


def test_active_analysis_and_completed_result_override_stale_capacity_reason():
    for stage,expected in [('model_evaluation','analyzing'),('model_completed','searching')]:
        result=activity_summary(snap(stage=stage,entry_status=dict(reason='HOLD_LIVE_AI_CAPACITY')))
        assert result['category']==expected


def test_invalid_output_is_not_reported_as_waiting_for_entry():
    result=activity_summary(snap(stage='model_invalid_or_unavailable_model',watchlist=[dict(status='watching')]))
    assert result['category']=='analysis_failed'
    assert 'geçerli BEKLE kararı değildir' in result['text']


def test_periodic_and_change_messages_are_bounded_and_account_scoped(tmp_path):
    messages=[];controller=SimpleNamespace(enqueue=lambda *a,**k:messages.append((a,k)))
    store=RuntimeStore(str(tmp_path/'control'))
    settings=SimpleNamespace(telegram_activity_interval_seconds=900)
    for now in range(1000,1110):
        publish_activity(controller,store,'a',snap(stage='model_provider_wait'),settings,now)
    assert len(messages)==1
    publish_activity(controller,store,'a',snap(stage='model_evaluation'),settings,1120)
    assert len(messages)==2
    publish_activity(controller,store,'a',snap(stage='model_evaluation'),settings,2020)
    assert len(messages)==3
    publish_activity(controller,store,'b',snap(stage='model_evaluation'),settings,2020)
    assert len(messages)==4 and messages[-1][0][0]=='b'


def test_paused_without_positions_is_quiet_but_monitor_warning_can_notify(tmp_path):
    messages=[];controller=SimpleNamespace(enqueue=lambda *a,**k:messages.append((a,k)))
    store=RuntimeStore(str(tmp_path/'control'));settings=SimpleNamespace(telegram_activity_interval_seconds=900)
    paused=dict(mode='LIVE',control=dict(enabled=False,stopped=True),positions={})
    publish_activity(controller,store,'a',paused,settings,1000)
    assert not messages
    paused.update(positions={'AAA_USDT':{}},worker_health=dict(delayed_jobs=[dict(kind='monitor',age_seconds=400)]))
    publish_activity(controller,store,'a',paused,settings,1001)
    assert 'Geciken görev: borsa uzlaştırması, 400 sn' in messages[0][0][1]


def test_fallback_mode_is_explicit_and_hold_is_final():
    text=activity_summary(snap(provider_pool=dict(paid_fallback_enabled=False)))['text']
    assert 'Ücretli Claude yedeği: kapalı' in text
    assert 'geçerli BEKLE kararı fallback başlatmaz' in text
