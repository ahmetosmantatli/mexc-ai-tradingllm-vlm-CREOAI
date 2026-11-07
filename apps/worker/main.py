"""Single persistent worker; PAPER default, LIVE requires explicit deployment gates."""
import fcntl
import logging
from pathlib import Path
import signal
import time

from trading.config import get_settings, BrokerMode
from trading.runtime.runner import PaperRunner

logger = logging.getLogger('worker')
_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


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
            if Path(cfg.accounts_file).is_file():
                if cfg.broker_mode == BrokerMode.LIVE:
                    from trading.runtime.live_fleet import LiveFleet
                    fleet = LiveFleet(cfg)
                else:
                    from trading.runtime.fleet import Fleet
                    fleet = Fleet(cfg)
                fleet.start()
                logger.info("Telegram worker started | mode=%s | accounts=%s", cfg.broker_mode.value, len(fleet.accounts))
                last_heartbeat = 0.0
                signal.signal(signal.SIGINT, _stop)
                signal.signal(signal.SIGTERM, _stop)
                try:
                    while _RUNNING:
                        fleet.tick()
                        if time.monotonic()-last_heartbeat>=60:
                            for account in fleet.accounts:
                                snapshot=fleet.control_store.get(f"account:{account.account_id}:status",{})
                                logger.info("%s status | account=%s version=%s reason=%s",cfg.broker_mode.value,account.account_id,snapshot.get('version','pending'),snapshot.get('reason','awaiting_setup'))
                            last_heartbeat=time.monotonic()
                        time.sleep(1)
                finally:
                    fleet.stop()
                return 0
            if cfg.broker_mode == BrokerMode.LIVE:
                raise ValueError('LIVE requires the multi-account registry and explicit account allowlist')
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
            deadline = time.monotonic()+cfg.position_poll_seconds
            while _RUNNING and time.monotonic() < deadline:
                time.sleep(max(0, min(0.5, deadline-time.monotonic())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
