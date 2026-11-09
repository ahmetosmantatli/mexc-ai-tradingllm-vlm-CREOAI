"""Durable, allowlisted usage journal; no prompts, secrets or chart content."""
import json

TRACE_FIELDS=('id','mode','analysis_version','symbol','strategy','confidence','side','setup_id','setup_family','started_at','completed_at','status','action','model_action','provider','model','fallback_reason','execution_status','execution_reason','execution_details','rationale','invalidation','watch_note','why_reconsidered','market_fingerprint','input_fingerprint','context_policy','prompt_bytes')
TRACE_FIELDS += ('runner_id','account_id','task','queued_at','strategy_cohort','trade_horizon')
ATTEMPT_FIELDS=('provider','model','provider_reported','status','phase','http_status','error_category','error_code','error_param','latency_ms','generation_requests','count_requests','input_tokens','output_tokens','estimated_cost_usd','pending_usd','estimated_input_tokens','token_budget','completion_token_limit','output_stop_reason','validation_issues','credential_id','quota_group','retry_number','retry_after_seconds','daily_quota_exhausted')


ATTEMPT_FIELDS += ('neuron_reserved_estimate',)


def usage_task(trace):
    return trace.get('task') or {
        'position_management': 'position_management',
        'watch_review': 'watch_review',
    }.get(trace.get('strategy'), 'entry')


def archive_trace(store, trace):
    safe={key:trace[key] for key in TRACE_FIELDS if key in trace}
    safe.update(runner_id=trace.get('runner_id', 'baseline-v1'), task=usage_task(trace))
    safe['attempts']=[{key:a[key] for key in ATTEMPT_FIELDS if key in a} for a in trace.get('attempts',[])]
    with store.lock,store.db:
        store.db.execute('CREATE TABLE IF NOT EXISTS ai_usage_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        store.db.execute('INSERT OR REPLACE INTO ai_usage_events VALUES (?,?)',(trace['id'],json.dumps(safe,allow_nan=False)))
