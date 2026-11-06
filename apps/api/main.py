"""Read-only operator API; worker owns account observation and paper state."""
import time
from fastapi import FastAPI, HTTPException
from trading import __version__
from trading.config import get_settings
from trading.runtime.store import RuntimeStore

app = FastAPI(title='MEXC AI Paper Monitor', version=__version__)


def runtime_status():
    store = RuntimeStore(get_settings().runtime_db_path)
    try:
        snapshot = store.get('status')
        if snapshot is None:
            raise HTTPException(503, 'Worker has not produced a verified snapshot')
        snapshot['snapshot_age_seconds'] = max(0, time.time()-snapshot['observed_at'])
        snapshot['stale'] = snapshot['snapshot_age_seconds'] > max(30, get_settings().position_poll_seconds*3)
        return snapshot
    finally:
        store.db.close()


@app.get('/health')
def health():
    cfg = get_settings()
    return dict(status='ok', version=__version__, live_trading_enabled=cfg.live_trading_enabled,
                broker_mode=cfg.effective_broker_mode().value, risk_profile=cfg.risk_profile.value,
                always_on_scan=cfg.always_on_scan)


@app.get('/settings')
def settings_public():
    return get_settings().public_dict()


@app.get('/status')
def status():
    return runtime_status()


@app.get('/ready')
def ready():
    s = runtime_status()
    if s['stale'] or s['reason'].startswith(('HOLD_ACCOUNT', 'HOLD_MONITOR', 'HOLD_WAITING')):
        raise HTTPException(503, s['reason'])
    return dict(status='paper_worker_responding', reason=s['reason'])


@app.get('/equity')
def equity():
    s = runtime_status()
    return dict(real_account=s['real_account'], paper=s['paper'], orders_are_paper=True,
                stale=s['stale'], snapshot_age_seconds=s['snapshot_age_seconds'])


@app.post('/equity/deposit')
@app.post('/equity/floor')
@app.post('/equity/reset')
def legacy_mutation_disabled():
    raise HTTPException(409, 'Legacy TRY seed mutations disabled. Deposit on MEXC; worker observes actual USDT account. Paper ledger remains independent.')


def create_app():
    return app
