"""Angel One scrip master: download/cache and resolve futures & option contracts."""
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contract:
    token: str
    symbol: str
    name: str
    exchange: str
    expiry: date
    strike: float = 0.0
    option_type: str = ""      # CE / PE / "" for futures
    lot_size: int = 0

    @property
    def expiry_api(self) -> str:   # format used by optionGreek API
        return self.expiry.strftime("%d%b%Y").upper()


def _parse_expiry(s: str):
    try:
        return datetime.strptime(s, "%d%b%Y").date()
    except (ValueError, TypeError):
        return None


class ScripMaster:
    def __init__(self, rows: list):
        self.rows = rows

    @classmethod
    def load(cls, url: str, cache_dir: Path, today: date):
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"scrip_master_{today.isoformat()}.json"
        if not path.exists():
            log.info("Downloading scrip master ...")
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            path.write_bytes(resp.content)
            for old in cache_dir.glob("scrip_master_*.json"):
                if old != path:
                    old.unlink(missing_ok=True)
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def _contracts(self, name, exchange, instrument_type, today):
        out = []
        for r in self.rows:
            if r.get("name") != name or r.get("exch_seg") != exchange \
                    or r.get("instrumenttype") != instrument_type:
                continue
            exp = _parse_expiry(r.get("expiry"))
            if exp is None or exp < today:
                continue
            sym = r.get("symbol", "")
            opt = sym[-2:] if instrument_type.startswith("OPT") else ""
            out.append(Contract(
                token=str(r["token"]), symbol=sym, name=name, exchange=exchange, expiry=exp,
                strike=float(r.get("strike") or 0) / 100.0, option_type=opt,
                lot_size=int(float(r.get("lotsize") or 0)),
            ))
        return out

    def nearest_future(self, name, exchange, today, instrument_type="FUTIDX"):
        futs = self._contracts(name, exchange, instrument_type, today)
        if not futs:
            raise LookupError(f"No {instrument_type} future found for {name} on {exchange}")
        return min(futs, key=lambda c: c.expiry)

    def option_chain(self, name, exchange, today):
        """{expiry: {(strike, 'CE'|'PE'): Contract}}"""
        chain = {}
        for c in self._contracts(name, exchange, "OPTIDX", today):
            chain.setdefault(c.expiry, {})[(c.strike, c.option_type)] = c
        return dict(sorted(chain.items()))


def select_expiries(chain: dict, spec) -> list:
    expiries = list(chain.keys())
    if isinstance(spec, int):
        return expiries[:spec]
    wanted = {date.fromisoformat(str(s)) for s in spec}
    missing = wanted - set(expiries)
    if missing:
        log.warning("Configured expiries not found: %s", sorted(missing))
    return [e for e in expiries if e in wanted]


def strikes_around(contracts: dict, spot: float, each_side: int) -> list:
    strikes = sorted({k[0] for k in contracts})
    if not strikes:
        return []
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    lo, hi = max(0, atm_idx - each_side), atm_idx + each_side + 1
    return strikes[lo:hi]
