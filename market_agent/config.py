import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import yaml

IST = timezone(timedelta(hours=5, minutes=30), "IST")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def load_env(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self, path: Path):
        with open(path, encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        load_env(PROJECT_ROOT / ".env")

        r = self.raw
        self.data_dir = PROJECT_ROOT / r.get("data_dir", "Data")
        self.log_dir = PROJECT_ROOT / r.get("log_dir", "logs")
        self.cache_dir = PROJECT_ROOT / r.get("cache_dir", "cache")

        m = r["market"]
        self.market_start = parse_hhmm(m.get("start", "09:15"))
        self.market_end = parse_hhmm(m.get("end", "15:40"))
        self.weekdays = set(m.get("weekdays", [0, 1, 2, 3, 4]))
        self.holidays = {str(d) for d in (m.get("holidays") or [])}
        self.snapshot_offset = int(m.get("snapshot_offset_seconds", 3))

        self.broker = r["broker"]
        self.indices = r["indices"]
        self.extras = r.get("extras") or {}
        self.options = r.get("options") or {}

        c = r.get("collection") or {}
        self.collect_mode = c.get("mode", "stream")
        self.track_new_atm = bool(c.get("track_new_atm_strikes", True))
        self.option_refresh_min = max(1, int(c.get("option_candle_refresh_minutes", 5)))

        g = r.get("greeks") or {}
        self.greeks_source = g.get("source", "api_then_local")
        self.risk_free_rate = float(g.get("risk_free_rate", 0.065))
        self.dividend_yield = float(g.get("dividend_yield", 0.0))

    def credentials(self) -> dict:
        keys = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing credentials in .env: {', '.join(missing)}")
        return {k: os.environ[k] for k in keys}

    def is_trading_day(self, d) -> bool:
        return d.weekday() in self.weekdays and d.isoformat() not in self.holidays
