"""Market-hours scheduling: run one trading day, or loop forever as a daemon."""
import logging
import time as _time
from datetime import datetime, timedelta

from .collector import Collector
from .config import IST, now_ist
from .storage import Store

log = logging.getLogger(__name__)
HOLIDAY_CHECK_AFTER = timedelta(minutes=10)   # after market start


def _sleep_until(target: datetime):
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        _time.sleep(min(remaining, 30))


def run_day(cfg, client, master_loader=None) -> None:
    now = now_ist()
    today = now.date()
    if not cfg.is_trading_day(today):
        log.info("%s is not a trading day - nothing to do", today)
        return
    start = datetime.combine(today, cfg.market_start, IST)
    end = datetime.combine(today, cfg.market_end, IST)
    if now >= end:
        log.info("Market session for %s already over", today)
        return

    _sleep_until(start - timedelta(minutes=5))
    collector = Collector(cfg, client, Store(), master_loader)
    collector.setup(now_ist())

    next_run = max(start + timedelta(minutes=1), now_ist().replace(second=0, microsecond=0) + timedelta(minutes=1))
    holiday_checked = False
    while next_run <= end:
        _sleep_until(next_run + timedelta(seconds=cfg.snapshot_offset))
        t0 = _time.monotonic()
        try:
            collector.cycle(now_ist())
        except Exception:
            log.exception("Cycle failed")
        log.info("Cycle %s done in %.1fs", next_run.strftime("%H:%M"), _time.monotonic() - t0)

        if not holiday_checked and next_run >= start + HOLIDAY_CHECK_AFTER:
            holiday_checked = True
            if not collector.has_index_data_today():
                log.warning("No index candles by %s - treating %s as a market holiday",
                            next_run.strftime("%H:%M"), today)
                return
        next_run = max(next_run + timedelta(minutes=1),
                       now_ist().replace(second=0, microsecond=0) + timedelta(minutes=1))

    log.info("Market closed - final backfill of all candles")
    try:
        collector.cycle(now_ist(), full=True)
    except Exception:
        log.exception("Final backfill failed")
    log.info("Day complete")


def run_daemon(cfg, client, master_loader=None) -> None:
    while True:
        run_day(cfg, client, master_loader)
        tomorrow = (now_ist() + timedelta(days=1)).date()
        _sleep_until(datetime.combine(tomorrow, cfg.market_start, IST) - timedelta(minutes=10))


def run_once(cfg, client, master_loader=None) -> None:
    """Single snapshot + candle backfill right now (ignores market hours) - for testing."""
    collector = Collector(cfg, client, Store(), master_loader)
    collector.setup(now_ist())
    collector.cycle(now_ist())
    collector.cycle(now_ist(), full=True)
