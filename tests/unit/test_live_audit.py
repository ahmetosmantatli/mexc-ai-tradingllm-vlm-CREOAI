import json
from trading.control.live_audit import audit,format_report
from trading.runtime.store import RuntimeStore
from trading.runtime.usage import archive_trace


def test_read_only_audit_separates_model_decisions_execution_and_unknown_records(tmp_path):
    path=tmp_path/'runtime.sqlite3';store=RuntimeStore(str(path))
    store.put('account:friend:live_snapshot',dict(binding_id='b',observed_at=95))
    store.put('live:automatic_grant:friend',dict(expires_at=200,revoked=False))
    examples=[dict(id='hold',status='completed',action='hold'),dict(id='watch',status='completed',action='watch'),
        dict(id='reject',status='completed',action='trade',execution_status='rejected',execution_reason='HOLD_LIVE_EXECUTION_CONTRACT_QUANTITY_LIMIT'),
        dict(id='unknown',status='completed',action='trade',execution_status='awaiting_risk'),
        dict(id='no_call',status='provider_wait',action='hold'),
        dict(id='sizing',status='invalid_model_sizing',model_action='trade',action='hold',execution_status='not_opened')]
    for row in examples:archive_trace(store,dict(started_at=90,strategy='trend',**row))
    store.put('live:intent:a',dict(staged_at=90,binding_id='b',entry_write_attempted=True,status='entry_write_unknown',api_key='SECRET'))
    result=audit(path,since=80,now=100)
    assert result['evaluations']==6 and result['model_decisions']=={'hold':1,'watch':1,'trade':3}
    assert result['entry_attempt_records']==1 and result['entry_fill_records']==0
    assert result['unresolved_proposal_records']==1
    assert result['execution_rejections']['invalid_model_sizing']==1
    assert result['start_grant_expired'] is False and result['snapshot_age_seconds']==5
    assert 'SECRET' not in json.dumps(result) and 'SECRET' not in format_report(result)


def test_empty_model_trade_history_cannot_prove_executor_works(tmp_path):
    path=tmp_path/'runtime.sqlite3';store=RuntimeStore(str(path))
    archive_trace(store,dict(id='old',started_at=1,status='completed',action='trade'))
    result=audit(path,since=10,now=100)
    assert result['model_trade_proposals']==0
    assert 'kanıtlanamaz' in format_report(result)


def test_execution_error_excludes_arbitrary_text_and_unknown_reason_codes():
    from trading.runtime.execution_diagnostics import safe_execution_error
    from trading.brokers.live.execution import LiveExecutionError
    failure=LiveExecutionError('private_preflight_rejected',reason_codes=('INSUFFICIENT_FREE_MARGIN','SECRET'))
    details=safe_execution_error(failure)
    assert details['code']=='PRIVATE_PREFLIGHT'
    assert details['reason_codes']==['INSUFFICIENT_FREE_MARGIN']
    unknown=safe_execution_error(ValueError('api_key=SECRET raw response'))
    assert unknown['code']=='UNCLASSIFIED_EXECUTION_ERROR'
    assert 'SECRET' not in json.dumps([details,unknown])


def test_validated_trade_rejected_by_sizing_is_not_reported_as_model_hold():
    from trading.agents.telemetry import trace_outcome
    trace=dict(model_action='trade',action='hold',status='invalid_model_sizing',execution_status='not_opened')
    assert 'AI işlem önerdi' in trace_outcome(trace)
    assert 'beklemeyi seçti' not in trace_outcome(trace)
