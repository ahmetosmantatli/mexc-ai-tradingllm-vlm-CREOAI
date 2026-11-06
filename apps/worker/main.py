"""Worker stub for Phase 1.

Phase 2+ will run Quant → LangGraph → Risk → PaperBroker loops here.
This process only boots, logs safety defaults, and idles cleanly.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from trading.config import get_settings
from trading.risk import RiskEngine, get_preset

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
logger = logging.getLogger("worker")

_RUNNING = True


def _handle_stop(signum: int, _frame: object) -> None:
    global _RUNNING
    logger.info("received signal %s — shutting down", signum)
    _RUNNING = False


def main() -> int:
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())

    if settings.live_trading_enabled:
        logger.error("LIVE_TRADING_ENABLED=true is not supported in Phase 1 worker — refusing start")
        return 2

    preset = get_preset(settings.risk_profile)
    engine = RiskEngine(settings=settings, preset=preset)

    logger.info(
        "worker started | broker=%s profile=%s always_on_scan=%s floor=%s live=%s",
        settings.effective_broker_mode().value,
        settings.risk_profile.value,
        settings.always_on_scan and preset.always_on_scan,
        settings.capital_floor_try,
        settings.live_trading_enabled,
    )
    logger.info(
        "risk engine ready | stop_required=%s residual_min_pct=%.2f",
        preset.stop_required,
        preset.residual_min_pct,
    )
    _ = engine  # Phase 2 wires proposal loop

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    while _RUNNING:
        # Always-on scan flag is honored as a readiness signal only in Phase 1.
        time.sleep(5)

    logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
