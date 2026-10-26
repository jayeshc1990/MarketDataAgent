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
    try:
        _collect_session(cfg, collector, start, end, today)
    finally:
        collector.stop()


def _collect_session(cfg, collector, start, end, today):
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


def run_check(cfg, client) -> bool:
    """Call each Angel One endpoint once and report OK/FAIL - for diagnosing access problems."""
    name, spec = next(iter(cfg.indices.items()))
    exch, token = spec["spot"]["exchange"], spec["spot"]["token"]
    day = now_ist().date()
    while not cfg.is_trading_day(day) or (day == now_ist().date() and now_ist().time() < cfg.market_start):
        day -= timedelta(days=1)
    start = datetime.combine(day, cfg.market_start)

    checks = [
        ("login", client.login),
        (f"quote {name}", lambda: client.quotes({exch: [token]})),
        (f"candles {name} {day}", lambda: client.candles(exch, token, "ONE_MINUTE", start,
                                                         start + timedelta(minutes=15))),
    ]
    if cfg.collect_mode == "stream":
        checks.append((f"live stream {name}", lambda: _check_stream(client, exch, token)))
    ok = True
    for label, call in checks:
        try:
            result = call()
            extra = (f" ({len(result)} rows)" if isinstance(result, list)
                     else f" ({result})" if isinstance(result, str) else "")
            print(f"  OK    {label}{extra}")
        except Exception as e:
            ok = False
            print(f"  FAIL  {label}: {e}")
    return ok


def _check_stream(client, exch, token, wait=15):
    from .stream import MODE_QUOTE
    got = []
    stream = client.stream(got.append)
    stream.subscribe(exch, [token], MODE_QUOTE)
    stream.start()
    deadline = _time.monotonic() + wait
    while not got and _time.monotonic() < deadline:
        _time.sleep(0.2)
    stream.stop()
    if not got:
        raise RuntimeError(f"no ticks within {wait}s (connected={stream.connected.is_set()})")
    t = got[-1]
    when = datetime.fromtimestamp(t["exchange_ts"] / 1000, IST).strftime("%H:%M:%S")
    return f"ltp={t['ltp']} at {when}"


def run_once(cfg, client, master_loader=None, minutes: int = 2) -> None:
    """Quick test right now (ignores market hours).
    stream mode: collect live candles for `minutes` complete minutes. poll mode: one cycle + backfill."""
    collector = Collector(cfg, client, Store(), master_loader)
    collector.setup(now_ist())
    try:
        if collector.stream:
            first = now_ist().replace(second=0, microsecond=0) + timedelta(minutes=1)
            log.info("Collecting live candles for %d full minute(s) - first is written at %s",
                     minutes, (first + timedelta(minutes=1)).strftime("%H:%M:%S"))
            for i in range(1, minutes + 1):
                _sleep_until(first + timedelta(minutes=i, seconds=cfg.snapshot_offset))
                collector.cycle(now_ist())
        else:
            collector.cycle(now_ist())
            collector.cycle(now_ist(), full=True)
    finally:
        collector.stop()
    log.info("Done - files saved under %s", cfg.data_dir)
