"""Worker loop: scan → propose → risk → paper execute (Phase 2).

PAPER ONLY. LIVE_TRADING_ENABLED must remain false.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.graph import EvaluationGraph
from trading.config import get_settings
from trading.pipeline import TradingPipeline
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
        logger.error(
            "LIVE_TRADING_ENABLED=true is not supported in Phase 2 worker — refusing start"
        )
        return 2

    preset = get_preset(settings.risk_profile)
    llm = AnthropicClient(settings=settings)
    graph = EvaluationGraph(llm=llm)
    engine = RiskEngine(settings=settings, preset=preset)
    pipeline = TradingPipeline(
        settings=settings,
        graph=graph,
        risk_engine=engine,
    )

    logger.info(
        "worker started | broker=%s profile=%s always_on_scan=%s floor=%s live=%s "
        "interval=%.1fs anthropic=%s",
        settings.effective_broker_mode().value,
        settings.risk_profile.value,
        settings.always_on_scan and preset.always_on_scan,
        settings.capital_floor_try,
        settings.live_trading_enabled,
        settings.worker_loop_interval_seconds,
        "enabled" if llm.available else "stub",
    )
    logger.info(
        "pipeline ready | stop_required=%s residual_min_pct=%.2f scanner_max_symbols=%s",
        preset.stop_required,
        preset.residual_min_pct,
        settings.scanner_max_symbols,
    )

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    while _RUNNING:
        try:
            if settings.always_on_scan or preset.always_on_scan:
                pipeline.run_cycle()
            else:
                logger.info("always_on_scan disabled — idle tick")
        except Exception:  # noqa: BLE001 — keep loop alive; log and continue
            logger.exception("cycle failed; continuing after interval")

        # Interruptible sleep
        deadline = time.monotonic() + float(settings.worker_loop_interval_seconds)
        while _RUNNING and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    logger.info("worker stopped | cycles=%s", len(pipeline.audit_log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
