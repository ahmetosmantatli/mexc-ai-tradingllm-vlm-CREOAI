"""Independent paper price supervision; never waits for analysis or Telegram.

All users of the same ledger must share ``lock`` around ledger access. Network
requests deliberately happen outside that lock. This is quote polling, not an
exchange protective order or a promise of execution at the trigger price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ProtectionUnavailable(RuntimeError):
    """New risk must wait until independent supervision is healthy."""


@dataclass
class ProtectionHealth:
    checked_at: float = 0.0
    checked_monotonic: float = 0.0
    ok: bool = False
    errors: dict[str, str] = field(default_factory=dict)
    positions: dict[str, int] = field(default_factory=dict)
    fatal: bool = False


class PositionProtection:
    def __init__(self, ledger_getter: Callable[[], Any], public_client: Any,
                 *, lock: Any = None, poll_seconds: float = 2.0,
                 max_age_seconds: float = 30.0, health_max_age_seconds: float = 15.0,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        if not all(math.isfinite(x) and x > 0 for x in (poll_seconds, max_age_seconds, health_max_age_seconds)):
            raise ValueError('protection intervals must be finite and positive')
        self.ledger_getter = ledger_getter
        self.public = public_client
        self.lock = lock if lock is not None else threading.RLock()
        self.poll_seconds = poll_seconds
        self.max_age = max_age_seconds
        self.health_max_age = health_max_age_seconds
        self.clock, self.monotonic = clock, monotonic
        self.health = ProtectionHealth()
        self._health_lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _quote(self, symbol):
        ticker = self.public.get_ticker(symbol)
        values = (ticker.bid1, ticker.ask1, ticker.raw.get('fairPrice'))
        if not all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in values):
            raise ValueError('missing or invalid executable quote / fair price')
        bid, ask, fair = (float(v) for v in values)
        stamp = float(ticker.raw['timestamp']) / 1000
        age = self.clock() - stamp
        if not math.isfinite(age) or age < -5 or age > self.max_age or ask < bid:
            raise ValueError('stale or crossed quote')
        return bid, ask, fair

    def tick(self):
        """Poll every current symbol; one failed market cannot skip another stop.

        Persistence errors latch fatal health. Caller must restart/reconcile rather
        than recover by trading against potentially divergent in-memory state.
        """
        with self._tick_lock:
            with self.lock:
                ledger = self.ledger_getter()
                positions = {} if ledger is None else {
                    symbol: int(position['opened_at'])
                    for symbol, position in ledger.positions.items()
                }
            errors = {}
            checked = {}
            for symbol, opened_at in positions.items():
                try:
                    quote = self._quote(symbol)
                except Exception as exc:
                    errors[symbol] = type(exc).__name__
                    continue
                try:
                    with self.lock:
                        current = self.ledger_getter()
                        if current is not ledger:
                            errors[symbol] = 'LedgerChanged'
                            continue
                        position = current.positions.get(symbol)
                        if position is None:
                            continue
                        if int(position['opened_at']) != opened_at:
                            errors[symbol] = 'PositionChanged'
                            continue
                        current.mark(symbol, *quote)
                        checked[symbol] = opened_at
                except Exception as exc:
                    # No raw exceptions: persistence messages could contain paths.
                    with self._health_lock:
                        self.health.fatal = True
                    errors[symbol] = f'LedgerMutation:{type(exc).__name__}'
            with self._health_lock:
                self.health.checked_at = self.clock()
                self.health.checked_monotonic = self.monotonic()
                self.health.errors = errors
                self.health.positions = checked
                self.health.ok = not errors and not self.health.fatal
            return self.snapshot()

    def snapshot(self):
        with self._health_lock:
            return dict(ok=self.health.ok, checked_at=self.health.checked_at,
                        age_seconds=max(0, self.monotonic()-self.health.checked_monotonic),
                        errors=dict(self.health.errors), positions=dict(self.health.positions),
                        fatal=self.health.fatal)

    def assert_ready(self):
        """Call under shared ledger lock immediately before adding new risk."""
        state = self.snapshot()
        if not state['ok'] or state['age_seconds'] > self.health_max_age:
            raise ProtectionUnavailable('independent price supervision unavailable or stale')
        with self.lock:
            ledger = self.ledger_getter()
            positions = {} if ledger is None else ledger.positions
            if any(state['positions'].get(s) != int(p['opened_at']) for s, p in positions.items()):
                raise ProtectionUnavailable('new or replaced positions await first supervision tick')

    def _run(self):
        while not self._stop.is_set():
            started = self.monotonic()
            try:
                self.tick()
            except Exception as exc:
                with self._health_lock:
                    self.health.ok = False
                    self.health.fatal = True
                    self.health.errors = {'worker': type(exc).__name__}
                logger.error('independent protection failed (%s)', type(exc).__name__)
            self._stop.wait(max(0.01, self.poll_seconds-(self.monotonic()-started)))

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='paper-protection', daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=10.0):
        """Return whether worker stopped; do not close HTTP client during a request."""
        self._stop.set()
        with self._health_lock:
            self.health.ok = False
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return False
        return True
