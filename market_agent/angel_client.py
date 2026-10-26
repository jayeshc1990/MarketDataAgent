"""Thin Angel One SmartAPI REST client with login (TOTP), rate limiting and retries."""
import logging
import threading
import time as _time
from collections import deque
from datetime import datetime

import pyotp
import requests

log = logging.getLogger(__name__)

ROUTES = {
    "login": "/rest/auth/angelbroking/user/v1/loginByPassword",
    "candle": "/rest/secure/angelbroking/historical/v1/getCandleData",
    "quote": "/rest/secure/angelbroking/market/v1/quote/",
    "greeks": "/rest/secure/angelbroking/marketData/v1/optionGreek",
}

# (max_calls, window_seconds) - kept slightly under Angel One's published limits
LIMITS = {
    "login": [(1, 1.0)],
    "candle": [(2, 1.0), (170, 60.0), (4800, 3600.0)],
    "quote": [(8, 1.0), (450, 60.0), (4800, 3600.0)],
    "greeks": [(1, 1.0), (80, 60.0), (900, 3600.0)],
}

TOKEN_ERRORS = {"AG8001", "AG8002", "AG8003", "AB8050", "AB8051", "AB1010"}
QUOTE_BATCH = 50


class RateLimiter:
    def __init__(self, limits):
        self.limits = limits
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            while True:
                now = _time.monotonic()
                longest = max(w for _, w in self.limits)
                while self.calls and now - self.calls[0] > longest:
                    self.calls.popleft()
                sleep_for = 0.0
                for max_calls, window in self.limits:
                    recent = [t for t in self.calls if now - t < window]
                    if len(recent) >= max_calls:
                        sleep_for = max(sleep_for, window - (now - recent[0]) + 0.01)
                if sleep_for <= 0:
                    self.calls.append(now)
                    return
                _time.sleep(sleep_for)


class AngelAPIError(Exception):
    def __init__(self, message, code=""):
        super().__init__(f"{code} {message}".strip())
        self.code = code


class AngelClient:
    def __init__(self, base_url: str, creds: dict):
        self.base_url = base_url.rstrip("/")
        self.creds = creds
        self.jwt = None
        self.session = requests.Session()
        self.limiters = {k: RateLimiter(v) for k, v in LIMITS.items()}

    # ---------------------------------------------------------------- core
    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.creds["ANGEL_API_KEY"],
        }
        if self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def _post(self, route: str, payload: dict, retries: int = 4):
        url = self.base_url + ROUTES[route]
        for attempt in range(retries):
            self.limiters[route].wait()
            try:
                resp = self.session.post(url, json=payload, headers=self._headers(), timeout=20)
            except requests.RequestException as e:
                log.warning("%s network error (%s), retry %d", route, e, attempt + 1)
                _time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) or "exceeding access rate" in resp.text:
                log.warning("%s HTTP %s, backing off", route, resp.status_code)
                _time.sleep(2 ** attempt)
                continue
            if resp.status_code in (401, 403) and route != "login":
                self.login()
                continue
            try:
                body = resp.json()
            except ValueError:
                log.warning("%s non-JSON response (HTTP %s): %s", route, resp.status_code, resp.text[:200])
                _time.sleep(2 ** attempt)
                continue
            if body.get("status") is False or body.get("success") is False:
                code = body.get("errorcode") or body.get("errorCode") or ""
                if code in TOKEN_ERRORS and route != "login":
                    log.info("Session expired (%s), logging in again", code)
                    self.login()
                    continue
                raise AngelAPIError(body.get("message", "request failed"), code)
            return body.get("data")
        raise AngelAPIError(f"{route}: retries exhausted")

    # ---------------------------------------------------------------- API
    def login(self):
        self.jwt = None
        totp = pyotp.TOTP(self.creds["ANGEL_TOTP_SECRET"]).now()
        data = self._post("login", {
            "clientcode": self.creds["ANGEL_CLIENT_CODE"],
            "password": self.creds["ANGEL_PIN"],
            "totp": totp,
        })
        self.jwt = data["jwtToken"]
        log.info("Logged in to Angel One as %s", self.creds["ANGEL_CLIENT_CODE"])

    def candles(self, exchange: str, token: str, interval: str, start: datetime, end: datetime):
        """Return list of [iso_ts, open, high, low, close, volume]."""
        try:
            data = self._post("candle", {
                "exchange": exchange,
                "symboltoken": str(token),
                "interval": interval,
                "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                "todate": end.strftime("%Y-%m-%d %H:%M"),
            })
        except AngelAPIError as e:
            if e.code == "AB1004" or "No Data" in str(e):
                return []
            raise
        return data or []

    def quotes(self, exchange_tokens: dict):
        """exchange_tokens: {"NFO": [tok, ...], ...} -> list of FULL quote dicts."""
        out = []
        for exch, tokens in exchange_tokens.items():
            tokens = [str(t) for t in tokens]
            for i in range(0, len(tokens), QUOTE_BATCH):
                data = self._post("quote", {"mode": "FULL",
                                            "exchangeTokens": {exch: tokens[i:i + QUOTE_BATCH]}})
                out.extend((data or {}).get("fetched") or [])
        return out

    def option_greeks(self, name: str, expiry: str):
        """expiry like '28OCT2026'. Returns list of dicts with strikePrice/optionType/delta/..."""
        return self._post("greeks", {"name": name, "expirydate": expiry}) or []
