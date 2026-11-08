import json

from trading.control.usage_report import export
from trading.runtime.store import RuntimeStore
from trading.runtime.usage import archive_trace


def test_usage_export_deduplicates_history_keeps_unknown_and_no_secrets(tmp_path):
    root=tmp_path/'data'
    store=RuntimeStore(str(root/'ahmet'/'session1'/'vlm'/'runtime.sqlite3'))
    trace=dict(id='one',symbol='BTC_USDT',strategy='trend',started_at=100,raw_prompt='SECRET',
        attempts=[dict(provider='groq',model='vision',status='rejected',generation_requests=1,http_status=400,raw_error='SECRET',input_tokens=None,output_tokens=None),
                  dict(provider='anthropic',model='claude',status='success',generation_requests=1,count_requests=1,input_tokens=100,output_tokens=20,estimated_cost_usd=.0006)])
    archive_trace(store,trace)
    store.put('analysis_history',[trace])
    output=tmp_path/'reports'
    report=export(root,output)
    assert report['evaluations']==1
    groq=next(g for g in report['groups'] if g['provider']=='groq')
    assert groq['unknown_usage_requests']==1 and groq['known_estimated_cost_usd'] is None
    claude=next(g for g in report['groups'] if g['provider']=='anthropic')
    assert claude['avg_input_tokens']==100 and claude['count_requests']==1
    for path in output.iterdir():
        assert 'SECRET' not in path.read_text()
        assert path.stat().st_mode&0o777==0o600
    assert export(root,output,since=101)['evaluations']==0


def test_archive_survives_recent_history_rollover_and_separates_variants(tmp_path):
    root=tmp_path/'data'
    for version in ('text','vlm'):
        store=RuntimeStore(str(root/'friend'/'session' /version/'runtime.sqlite3'))
        for i in range(35):
            trace=dict(id=str(i),started_at=i,attempts=[dict(provider='groq',model='model',status='success',generation_requests=1,input_tokens=200,output_tokens=40)])
            archive_trace(store,trace)
        store.put('analysis_history',[trace])
    report=export(root,tmp_path/'out')
    assert report['evaluations']==70
    assert len(report['groups'])==2
    assert all(g['generation_requests']==35 for g in report['groups'])


def test_live_usage_and_position_management_are_exported_with_actual_variant(tmp_path):
    root=tmp_path/'data';store=RuntimeStore(str(root/'friend'/'live'/'runtime.sqlite3'))
    for ident,version,strategy in [('entry','text','trend'),('management','vlm','position_management')]:
        archive_trace(store,dict(id=ident,mode='LIVE',analysis_version=version,strategy=strategy,started_at=100,
            attempts=[dict(provider='gemini',model='fake',status='success',generation_requests=1,input_tokens=300,output_tokens=50)]))
    report=export(root,tmp_path/'out')
    assert report['evaluations']==2
    assert {g['variant'] for g in report['groups']}=={'text','vlm'}
    assert 'position_management' in (tmp_path/'out'/'ai-requests.csv').read_text()



def test_review_summary_separates_exact_repeats_market_repeats_and_paid_causes(tmp_path):
    from trading.control.usage_report import review_summary
    rows=[]
    for ident,fingerprint in [('one','first-input'),('two','second-input'),('three','second-input')]:
        rows.append(dict(id=ident,account='friend',variant='text',symbol='BTC_USDT',strategy='watch_review',
            status='completed',action='hold',market_fingerprint='same-market',input_fingerprint=fingerprint,
            fallback_reason='free_pool_quota_or_cooldown',attempts=[
                dict(provider='groq',status='quota_skipped',error_category='request_too_large',generation_requests=0),
                dict(provider='anthropic',status='success',generation_requests=1,estimated_cost_usd=.04)]))
    rows.append(dict(account='friend',variant='text',status='provider_wait',attempts=[
        dict(provider='gemini',status='quota_skipped',error_category='free_capacity_in_flight',generation_requests=0)]))
    result=review_summary(rows)
    assert result['model_decisions']=={'hold':3}
    assert result['routing_skip_reasons']=={'request_too_large':3,'free_capacity_in_flight':1}
    assert result['paid_by_strategy'][0]['known_estimated_cost_usd']==.12
    assert result['review_counts'][0]['exact_input_repeat_reviews']==1
    assert result['review_counts'][0]['same_market_repeat_reviews']==2
    assert result['review_counts'][0]['sent_analyses']==3
    assert result['paid_route_reasons']=={'free_pool_quota_or_cooldown':3}
