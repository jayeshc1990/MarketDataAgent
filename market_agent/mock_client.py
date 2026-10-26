"""Offline stand-in for AngelClient + scrip master, for testing without credentials."""
import math
import random
from datetime import datetime, timedelta

import pandas as pd

from .instruments import ScripMaster

SPOTS = {"99926000": 25000.0, "99926009": 55000.0, "99919000": 82000.0, "99926017": 13.5}
UNDERLYINGS = {"NIFTY": ("NFO", 25000.0, 50), "BANKNIFTY": ("NFO", 55000.0, 100),
               "SENSEX": ("BFO", 82000.0, 100)}


def _wave(token, minute_index):
    seed = int(token) % 997
    return math.sin(minute_index / 25 + seed) * 0.003 + math.sin(minute_index / 7 + seed * 2) * 0.001


class MockClient:
    def __init__(self):
        self.meta = {}          # token -> dict(kind, base, strike, opt, underlying)
        for tok, base in SPOTS.items():
            self.meta[tok] = {"kind": "spot", "base": base}

    def scrip_master(self, today):
        rows, tok = [], 500000
        expiries = [today + timedelta(days=(3 - today.weekday()) % 7 + 7 * i) for i in range(2)]
        for name, (exch, base, step) in UNDERLYINGS.items():
            tok += 1
            fut_exp = expiries[-1]
            rows.append(self._row(tok, f"{name}{fut_exp:%d%b%y}FUT".upper(), name, exch, fut_exp, 0, "FUTIDX"))
            self.meta[str(tok)] = {"kind": "future", "base": base * 1.003}
            for exp in expiries:
                for k in range(-30, 31):
                    strike = round(base / step) * step + k * step
                    for opt in ("CE", "PE"):
                        tok += 1
                        sym = f"{name}{exp:%d%b%y}{int(strike)}{opt}".upper()
                        rows.append(self._row(tok, sym, name, exch, exp, strike, "OPTIDX"))
                        self.meta[str(tok)] = {"kind": "option", "base": base, "strike": strike,
                                               "opt": opt, "exp": exp}
        tok += 1
        rows.append(self._row(tok, f"CRUDEOIL{expiries[-1]:%d%b%y}FUT".upper(), "CRUDEOIL", "MCX",
                              expiries[-1], 0, "FUTCOM"))
        self.meta[str(tok)] = {"kind": "future", "base": 5800.0}
        return ScripMaster(rows)

    @staticmethod
    def _row(tok, sym, name, exch, exp, strike, itype):
        return {"token": str(tok), "symbol": sym, "name": name, "exch_seg": exch,
                "expiry": exp.strftime("%d%b%Y").upper(), "strike": f"{strike * 100:.6f}",
                "lotsize": "75", "instrumenttype": itype, "tick_size": "5.0"}

    # -------------------------------------------------------------- prices
    def _price(self, token, ts):
        m = self.meta[str(token)]
        idx = int((ts - ts.replace(hour=9, minute=15)).total_seconds() // 60)
        w = _wave(token, idx)
        if m["kind"] != "option":
            return m["base"] * (1 + w)
        spot = m["base"] * (1 + _wave(7, idx))
        intrinsic = max(0.0, spot - m["strike"]) if m["opt"] == "CE" else max(0.0, m["strike"] - spot)
        return round(intrinsic + 0.004 * m["base"] * math.exp(-abs(spot - m["strike"]) / (0.02 * m["base"])) + 5, 2)

    def login(self):
        pass

    def candles(self, exchange, token, interval, start, end):
        out, ts = [], start.replace(second=0, microsecond=0)
        close_t = ts.replace(hour=15, minute=30)
        while ts < min(end, close_t):
            p = self._price(token, ts)
            rnd = random.Random(f"{token}{ts}")
            o, c = p * (1 + rnd.uniform(-5e-4, 5e-4)), p
            h, l = max(o, c) * (1 + rnd.uniform(0, 5e-4)), min(o, c) * (1 - rnd.uniform(0, 5e-4))
            vol = 0 if self.meta[str(token)]["kind"] == "spot" else rnd.randint(100, 50000)
            out.append([ts.strftime("%Y-%m-%dT%H:%M:%S+05:30"), round(o, 2), round(h, 2),
                        round(l, 2), round(c, 2), vol])
            ts += timedelta(minutes=1)
        return out

    def quotes(self, exchange_tokens):
        now = datetime.now().replace(second=0, microsecond=0)
        now = min(now, now.replace(hour=15, minute=29))
        out = []
        for exch, toks in exchange_tokens.items():
            for t in toks:
                rnd = random.Random(f"q{t}{now}")
                ltp = round(self._price(t, now), 2)
                kind = self.meta[str(t)]["kind"]
                depth = {"buy": [{"price": round(ltp - 0.05 * (i + 1), 2), "quantity": rnd.randint(75, 7500),
                                  "orders": rnd.randint(1, 40)} for i in range(5)],
                         "sell": [{"price": round(ltp + 0.05 * (i + 1), 2), "quantity": rnd.randint(75, 7500),
                                   "orders": rnd.randint(1, 40)} for i in range(5)]}
                out.append({"exchange": exch, "symbolToken": str(t), "ltp": ltp,
                            "opnInterest": 0 if kind == "spot" else rnd.randint(10000, 5000000),
                            "totBuyQuan": rnd.randint(1000, 900000), "totSellQuan": rnd.randint(1000, 900000),
                            "depth": depth if kind != "spot" else {}})
        return out

    def stream(self, on_tick):
        return MockStream(self, on_tick)

    def option_greeks(self, name, expiry):
        if name == "SENSEX":
            raise RuntimeError("mock: greeks not available for BFO")  # exercises local fallback
        out = []
        for m in self.meta.values():
            if m["kind"] == "option" and m["exp"].strftime("%d%b%Y").upper() == expiry \
                    and m["base"] == UNDERLYINGS[name][1]:
                mny = (m["base"] - m["strike"]) / m["base"]
                d = 0.5 + max(-0.49, min(0.49, mny * 20))
                out.append({"name": name, "expiry": expiry, "strikePrice": f"{m['strike']:.6f}",
                            "optionType": m["opt"], "delta": f"{d if m['opt'] == 'CE' else d - 1:.4f}",
                            "gamma": "0.0012", "theta": "-12.5", "vega": "9.8", "impliedVolatility": "14.2"})
        return out


class MockStream:
    """Pushes generated ticks for subscribed tokens a few times per second (same API as AngelStream)."""

    def __init__(self, client, on_tick):
        import threading
        self.client, self.on_tick = client, on_tick
        self.tokens = {}            # (exchange, token) -> mode
        self.connected = threading.Event()
        self.stopping = False
        self.ticks = 0
        self.volume = {}

    def subscribe(self, exchange, tokens, mode=3):
        for t in tokens:
            self.tokens[(exchange, str(t))] = mode

    def start(self):
        import threading
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stopping = True

    def _run(self):
        import time as _t
        self.connected.set()
        while not self.stopping:
            now = datetime.now()
            for (exch, tok), mode in list(self.tokens.items()):
                rnd = random.Random()
                kind = self.client.meta[tok]["kind"]
                ltp = round(self.client._price(tok, now) * (1 + rnd.uniform(-8e-4, 8e-4)), 2)
                tick = {"mode": mode, "exchange": exch, "token": tok,
                        "exchange_ts": int(_t.time() * 1000), "ltp": ltp}
                if mode >= 2:
                    if kind != "spot":
                        self.volume[tok] = self.volume.get(tok, 100000) + rnd.randint(0, 300)
                    tick.update(volume=self.volume.get(tok, 0), total_buy_qty=rnd.randint(1000, 900000),
                                total_sell_qty=rnd.randint(1000, 900000))
                if mode >= 3:
                    lv = lambda sign: [{"price": round(ltp + sign * 0.05 * (i + 1), 2),
                                        "quantity": rnd.randint(75, 7500), "orders": rnd.randint(1, 40)}
                                       for i in range(5)]
                    tick.update(oi=rnd.randint(10000, 5000000), depth={"buy": lv(-1), "sell": lv(1)})
                self.ticks += 1
                self.on_tick(tick)
            _t.sleep(0.5)
