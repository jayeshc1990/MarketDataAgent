"""MarketDataAgent entry point.

    python run_agent.py              # run today's session (waits for 09:10 IST, stops at 15:40)
    python run_agent.py --daemon     # keep running, every trading day
    python run_agent.py --once       # one immediate cycle + backfill (testing, ignores market hours)
    python run_agent.py --check      # test login, quotes and candles once, print OK/FAIL
    python run_agent.py --mock ...   # use generated data instead of Angel One (no credentials needed)
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from market_agent.config import IST, PROJECT_ROOT, Config, now_ist
from market_agent import runner


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    # IST, even on a UTC server (staticmethod: a plain function on the class would receive self)
    logging.Formatter.converter = staticmethod(lambda secs: datetime.fromtimestamp(secs, IST).timetuple())
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.FileHandler(log_dir / f"agent_{now_ist():%Y-%m-%d}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ])


def main():
    p = argparse.ArgumentParser(description="Indian F&O market data collector")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true", help="run every trading day forever")
    mode.add_argument("--once", action="store_true", help="single cycle now, for testing")
    mode.add_argument("--check", action="store_true", help="test API access once and exit")
    p.add_argument("--mock", action="store_true", help="use generated data (no broker login)")
    args = p.parse_args()

    cfg = Config(Path(args.config))
    setup_logging(cfg.log_dir)

    if args.mock:
        from market_agent.mock_client import MockClient
        client = MockClient()
        loader = client.scrip_master
    else:
        from market_agent.angel_client import AngelClient
        client = AngelClient(cfg.broker["base_url"], cfg.credentials())
        loader = None

    if args.check:
        sys.exit(0 if runner.run_check(cfg, client) else 1)
    elif args.once:
        runner.run_once(cfg, client, loader)
    elif args.daemon:
        runner.run_daemon(cfg, client, loader)
    else:
        runner.run_day(cfg, client, loader)


if __name__ == "__main__":
    main()
