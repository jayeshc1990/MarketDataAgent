"""Download as much past index history as Angel One gives (for multi-day support/resistance levels).

    python fetch_history.py                      # NIFTY spot: daily, 15-min, 5-min and 1-min candles
    python fetch_history.py --intervals ONE_DAY  # only daily candles
    python fetch_history.py --mock               # generated data, no credentials (testing)

Writes Data/<INDEX>/HISTORY/<INDEX>_<1day|1hour|15min|5min|1min>.csv (timestamp, open, high, low, close,
volume). Re-running only fetches what is missing: newer candles since the last row, then older ones until
Angel One has nothing more. It saves after every request, so it can be stopped and resumed.

It refuses to run in market hours (it shares Angel One's candle limit, ~10-15 requests/min, with the live
collector) unless --force. Expired option contracts can't be downloaded: Angel One only serves live tokens.
"""
import argparse
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd

from market_agent.config import IST, PROJECT_ROOT, Config, now_ist

log = logging.getLogger("history")

# interval -> (file label, days per request: Angel One's max range for that interval)
INTERVALS = {
    "ONE_DAY": ("1day", 2000),
    "ONE_HOUR": ("1hour", 400),
    "FIFTEEN_MINUTE": ("15min", 200),
    "FIVE_MINUTE": ("5min", 100),
    "ONE_MINUTE": ("1min", 30),
}
DEFAULT_INTERVALS = ["ONE_DAY", "FIFTEEN_MINUTE", "FIVE_MINUTE", "ONE_MINUTE"]
EMPTY_CHUNKS_TO_STOP = 3       # this many empty requests in a row going back = no older data
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def to_frame(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows, columns=COLS)
    ts = pd.to_datetime(df.timestamp, utc=True)
    df["timestamp"] = ts.dt.tz_convert(IST).dt.tz_localize(None)
    return df


def save(path, old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    df = pd.concat([x for x in (old, new) if len(x)], ignore_index=True) if len(old) or len(new) else old
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)
    return df


def fetch(client, exch, token, interval, path, since: datetime, now: datetime):
    days = INTERVALS[interval][1]
    df =pd.read_csv(path, parse_dates=["timestamp"]) if path.exists() else pd.DataFrame(columns=COLS)
    step = timedelta(days=days)

    # 1) newer candles than the file has (or everything up to now for a new file)
    start = df.timestamp.max() + timedelta(minutes=1) if len(df) else now - step
    while start < now:
        end = min(start + step, now)
        rows = to_frame(client.candles(exch, token, interval, start, end))
        df = save(path, df, rows)
        log.info("%s: %s .. %s -> %d candles (file %d)", path.name, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}",
                 len(rows), len(df))
        start = end

    # 2) older candles, going back until Angel One returns nothing several times in a row
    empty = 0
    end = df.timestamp.min() - timedelta(minutes=1) if len(df) else now
    while end > since and empty < EMPTY_CHUNKS_TO_STOP:
        start = max(end - step, since)
        rows = to_frame(client.candles(exch, token, interval, start, end))
        empty = empty + 1 if rows.empty else 0
        df = save(path, df, rows)
        log.info("%s: %s .. %s -> %d candles (file %d)", path.name, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}",
                 len(rows), len(df))
        end = start - timedelta(minutes=1)
    first = df.timestamp.min() if len(df) else None
    log.info("%s done: %d candles from %s to %s", path.name, len(df), first, df.timestamp.max() if len(df) else None)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    p.add_argument("--intervals", nargs="+", default=DEFAULT_INTERVALS, choices=list(INTERVALS))
    p.add_argument("--since", default="2010-01-01", help="don't go back further than this date")
    p.add_argument("--mock", action="store_true", help="generated data instead of Angel One")
    p.add_argument("--force", action="store_true", help="run even in market hours")
    a = p.parse_args()

    cfg = Config(Path(a.config))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    now = now_ist()
    if (not a.force and not a.mock and now.weekday() < 5
            and time(9, 0) <= now.time() <= time(15, 45)):
        sys.exit("Market hours: this would compete with the live collector for Angel One's candle limit. "
                 "Run it after 15:45 IST (or add --force).")

    if a.mock:
        from market_agent.mock_client import MockClient
        client = MockClient()
        client.scrip_master(now.date())
    else:
        from market_agent.angel_client import AngelClient
        client = AngelClient(cfg.broker["base_url"], cfg.credentials())
    client.login()

    end = now.replace(tzinfo=None, second=0, microsecond=0)
    since = datetime.fromisoformat(a.since)
    for name, spec in cfg.indices.items():
        exch, token = spec["spot"]["exchange"], spec["spot"]["token"]
        for interval in a.intervals:
            path = cfg.data_dir / name / "HISTORY" / f"{name}_{INTERVALS[interval][0]}.csv"
            log.info("== %s %s (token %s) -> %s", name, interval, token, path)
            fetch(client, exch, token, interval, path, since, end)


if __name__ == "__main__":
    main()
