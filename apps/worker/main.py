"""Persistent worker. Paper never submits real orders. Live boots disarmed."""
import fcntl
import logging
from pathlib import Path
import signal
import time

from trading.config import BrokerMode, get_settings
from trading.runtime.runner import PaperRunner

logger = logging.getLogger('worker')
_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


def worker_mode(cfg):
    """paper, live, or refused. Live requires both gates; anything else stays off."""
    live = cfg.broker_mode == BrokerMode.LIVE
    enabled = cfg.live_trading_enabled is True
    if live and enabled:
        return 'live'
    if live or enabled:
        return 'refused'
    return 'paper'


def _wait(cfg):
    deadline = time.monotonic()+cfg.position_poll_seconds
    while _RUNNING and time.monotonic() < deadline:
        time.sleep(max(0, min(0.5, deadline-time.monotonic())))


def main():
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper(), format='%(asctime)s %(levelname)s %(message)s')
    # httpx INFO logs include the full Telegram bot URL/token.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    lock_path = Path(cfg.runtime_db_path+'.worker.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error('another worker owns this ledger')
            return 2
        try:
            mode = worker_mode(cfg)
            if mode == 'refused':
                logger.error('startup refused: set both BROKER_MODE=live and LIVE_TRADING_ENABLED=true, or leave both off')
                return 2
            if mode == 'live':
                from trading.runtime.live_runner import LiveRunner
                runner = LiveRunner(cfg)
                logger.info('LIVE worker started disarmed; this process is the only Telegram poller; Arm or /arm from an allowlisted operator')
                try:
                    runner.present_controls()
                except Exception as exc:
                    logger.warning('live telegram controls not sent (%s)', type(exc).__name__)
                signal.signal(signal.SIGINT, _stop)
                signal.signal(signal.SIGTERM, _stop)
                while _RUNNING:
                    try:
                        status = runner.cycle()
                        logger.info('live cycle: %s equity=%s armed=%s', status.get('reason'), status.get('equity'), status.get('armed'))
                    except Exception as exc:
                        logger.error('fatal worker failure (%s); restart/reconciliation required', type(exc).__name__)
                        return 1
                    _wait(cfg)
                return 0
            if Path(cfg.accounts_file).is_file():
                from trading.runtime.fleet import Fleet
                fleet = Fleet(cfg)
                fleet.start()
                logger.info("Telegram PAPER worker started | accounts=%s | real orders disabled", len(fleet.accounts))
                last_heartbeat = 0.0
                signal.signal(signal.SIGINT, _stop)
                signal.signal(signal.SIGTERM, _stop)
                try:
                    while _RUNNING:
                        fleet.tick()
                        if time.monotonic()-last_heartbeat>=60:
                            for account in fleet.accounts:
                                snapshot=fleet.control_store.get(f"account:{account.account_id}:status",{})
                                logger.info("paper status | account=%s version=%s reason=%s",account.account_id,snapshot.get('version','pending'),snapshot.get('reason','awaiting_setup'))
                            last_heartbeat=time.monotonic()
                        time.sleep(1)
                finally:
                    fleet.stop()
                return 0
            runner = PaperRunner(cfg)
        except ValueError as exc:
            logger.error('startup refused: %s', exc)
            return 2
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        logger.info('PAPER worker started; no live orders; account credentials configured=%s', cfg.mexc_api_key_configured)
        while _RUNNING:
            try:
                status = runner.cycle()
                logger.info('paper cycle: %s', status['reason'])
            except Exception as exc:
                # A persistence failure is fatal. Do not trade against stale memory.
                logger.error('fatal worker failure (%s); restart/reconciliation required', type(exc).__name__)
                return 1
            _wait(cfg)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
