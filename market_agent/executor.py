"""Client for the Trade Executor (its SIGNAL_SPEC, version 2026-11-18).

The executor runs on its own server and places the orders; this side only says *what* and *when*:
    POST http://162.35.172.36:8000/signal   one JSON object: NEW (CALL / PUT / SELL), UPDATE or EXIT
    GET  http://162.35.172.36:8000/status   {"open": true/false, "trade": {...}}  - the source of truth
    GET  http://162.35.172.36:8000/health   {"ok": true}
It accepts only this machine's registered IP (74.50.64.172); any other IP gets 403 or no answer.

Every signal is checked here against the spec before it is sent (a 422 would mean a bug on this side).
Retries happen only when no answer came back (connection error, timeout, 5xx): the identical body,
same trade_id, after 0.5 s, 1 s and 2 s; after that ExecutorDown is raised.

    python -m market_agent.executor --status        # what the executor holds now (url from config.yaml)
"""
import argparse
import json
import re
import secrets
import time
from dataclasses import dataclass, field

import requests

DEFAULT_URL = "http://162.35.172.36:8000"
TIMEOUT = 5.0
RETRY_WAITS = (0.5, 1.0, 2.0)
TRADE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
# the spec's contract pattern (section 12)
CONTRACT_RE = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9&_-]*\s+[0-9]+(\.[0-9]+)?\s*([Cc][Ee]|[Pp][Ee])\s*\(\s*"
                         r"([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[\s-]*[A-Za-z]{3,9}[\s-]*([0-9]{4}|[0-9]{2})?)\s*\)"
                         r"\s*(@\s*[0-9]+(\.[0-9]+)?)?\s*$")
ANGEL_SYMBOL_RE = re.compile(r"^(?P<name>[A-Z]+)(?P<dd>\d{2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})(?P<strike>\d+)(?P<type>CE|PE)$")


class ExecutorDown(RuntimeError):
    """No answer from the executor after the retries (it is down or restarting)."""


@dataclass
class Answer:
    http: int
    body: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.http == 200 and bool(self.body.get("accepted"))

    @property
    def message(self) -> str:
        return str(self.body.get("message") or self.body.get("detail") or "")


# ---------------------------------------------------------------- building signals
def new_trade_id(now) -> str:
    """IST date + time + 4 random hex digits, as the spec recommends: 20261118-101530-7f3a."""
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def contract_text(symbol: str, ref_price: float = None) -> str:
    """Angel One symbol -> the spec's contract text: NIFTY17NOV2625300CE -> 'NIFTY 25300 CE (17 NOV 2026) @ 303.65'.
    The expiry in Angel One's symbol is the contract's real expiry date (holiday moves included)."""
    m = ANGEL_SYMBOL_RE.match(symbol)
    if not m:
        raise ValueError(f"can't read the contract from symbol {symbol!r}")
    text = f"{m['name']} {m['strike']} {m['type']} ({int(m['dd'])} {m['mon']} 20{m['yy']})"
    if ref_price is not None and ref_price > 0:
        text += f" @ {ref_price:.2f}"
    return text


def new_signal(trade_id: str, symbol: str, ref_price: float, sl: float = None, tp: float = None) -> dict:
    side = ANGEL_SYMBOL_RE.match(symbol)["type"] if ANGEL_SYMBOL_RE.match(symbol) else None
    s = {"type": "NEW", "trade_id": trade_id, "action": "CALL" if side == "CE" else "PUT",
         "contract": contract_text(symbol, ref_price)}
    if sl is not None and sl > 0:
        s["sl"] = round(float(sl), 2)
    if tp is not None and tp == tp and tp > ref_price and (sl is None or tp > sl):
        s["tp"] = round(float(tp), 2)
    return s


def sell_signal(trade_id: str) -> dict:
    return {"type": "NEW", "trade_id": trade_id, "action": "SELL"}


def validate(s: dict) -> None:
    """Raises ValueError when `s` breaks the spec (field names, values, number types, contract shape)."""
    allowed = {("NEW", "BUY"): {"type", "trade_id", "action", "contract", "sl", "tp", "price"},
               ("NEW", "SELL"): {"type", "trade_id", "action", "contract"},
               ("UPDATE", None): {"type", "trade_id", "sl", "tp"},
               ("EXIT", None): {"type", "trade_id"}}
    kind = s.get("type")
    action = s.get("action")
    key = ("NEW", "SELL") if (kind, action) == ("NEW", "SELL") else ("NEW", "BUY") if kind == "NEW" else (kind, None)
    if key not in allowed:
        raise ValueError(f"unknown signal type/action {kind!r}/{action!r}")
    extra = set(s) - allowed[key]
    if extra:
        raise ValueError(f"fields not allowed for {kind}: {sorted(extra)}")
    if not isinstance(s.get("trade_id"), str) or not TRADE_ID_RE.match(s["trade_id"]):
        raise ValueError(f"bad trade_id {s.get('trade_id')!r}")
    for f in ("sl", "tp", "price"):
        v = s.get(f)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0 or v != v
                              or v in (float("inf"),)):
            raise ValueError(f"{f} must be a number above 0, got {v!r}")
    if key == ("NEW", "BUY"):
        if action not in ("CALL", "PUT", "BUY"):
            raise ValueError(f"bad action {action!r}")
        c = s.get("contract")
        if not isinstance(c, str) or not CONTRACT_RE.match(c) or len(c) > 80:
            raise ValueError(f"bad contract {c!r}")
        typ = re.search(r"\s*(CE|PE)\s*\(", c.upper()).group(1)
        if (action, typ) in (("CALL", "PE"), ("PUT", "CE")):
            raise ValueError(f"{action} needs a {'CE' if action == 'CALL' else 'PE'} contract")
        if s.get("sl") and s.get("tp") and not s["sl"] < s["tp"]:
            raise ValueError("sl must be below tp")
    if kind == "UPDATE" and "sl" not in s and "tp" not in s:
        raise ValueError("UPDATE needs sl and/or tp")


# ---------------------------------------------------------------- talking to it
class Executor:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = TIMEOUT, waits=RETRY_WAITS):
        self.url, self.timeout, self.waits = url.rstrip("/"), timeout, waits

    def _request(self, method: str, path: str, body: dict = None) -> Answer:
        last = None
        for wait in (0.0,) + tuple(self.waits):
            if wait:
                time.sleep(wait)
            try:
                r = requests.request(method, self.url + path, json=body, timeout=self.timeout,
                                     headers={"Content-Type": "application/json"} if body is not None else None)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"message": r.text[:200]}
            return Answer(r.status_code, data if isinstance(data, dict) else {"message": str(data)})
        raise ExecutorDown(f"{method} {path}: no answer after {len(self.waits) + 1} tries ({last})")

    def signal(self, s: dict) -> Answer:
        validate(s)
        return self._request("POST", "/signal", s)

    def status(self) -> dict:
        a = self._request("GET", "/status")
        if a.http != 200 or "open" not in a.body:
            raise ExecutorDown(f"GET /status: HTTP {a.http} {a.message}")
        return a.body

    def health(self) -> bool:
        try:
            return self._request("GET", "/health").body.get("ok") is True
        except ExecutorDown:
            return False


def open_trade_id(status: dict):
    """trade_id of the executor's open trade, or None."""
    return (status.get("trade") or {}).get("trade_id") if status.get("open") else None


def config_url() -> str:
    """executor.url from config.yaml (DEFAULT_URL if it isn't set)."""
    try:
        from pathlib import Path
        from market_agent.config import Config
        return (Config(Path(__file__).resolve().parent.parent / "config.yaml").raw.get("executor") or {}).get(
            "url") or DEFAULT_URL
    except Exception:
        return DEFAULT_URL


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="default: executor.url in config.yaml")
    ap.add_argument("--status", action="store_true", help="print GET /status")
    a = ap.parse_args()
    url = a.url or config_url()
    ex = Executor(url, waits=())
    try:
        h = ex._request("GET", "/health")
    except ExecutorDown as e:
        print(f"health ({url}): NO ANSWER - executor down, or its firewall doesn't let this machine in ({e})")
        return 1
    if h.http == 403:
        print(f"health ({url}): HTTP 403 - this machine's IP isn't registered on the executor: {h.message}")
        return 1
    if h.http != 200 or h.body.get("ok") is not True:
        print(f"health ({url}): HTTP {h.http} {h.body}")
        return 1
    print(f"health ({url}): ok")
    if a.status:
        print(json.dumps(ex.status(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
