"""Single persistent paper worker. Never submits real exchange orders."""
import fcntl
import logging
from pathlib import Path
import signal
import time

from trading.config import get_settings
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
                from trading.runtime.fleet import Fleet
                fleet = Fleet(cfg)
                fleet.start()
                signal.signal(signal.SIGINT, _stop)
                signal.signal(signal.SIGTERM, _stop)
                try:
                    while _RUNNING:
                        fleet.tick()
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
            deadline = time.monotonic()+cfg.position_poll_seconds
            while _RUNNING and time.monotonic() < deadline:
                time.sleep(max(0, min(0.5, deadline-time.monotonic())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
