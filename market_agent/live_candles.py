"""Builds 1-minute candles from live ticks, keyed by (exchange, token)."""
import threading
from datetime import datetime, timedelta

from .config import IST

ONE_MIN = timedelta(minutes=1)


def tick_minute(tick, fallback: datetime) -> datetime:
    """Candle start (naive IST) for a tick, from the exchange timestamp when present."""
    ms = tick.get("exchange_ts") or 0
    t = datetime.fromtimestamp(ms / 1000, IST).replace(tzinfo=None) if ms > 0 else fallback
    return t.replace(second=0, microsecond=0)


class CandleBuilder:
    """Bar dicts: minute, open, high, low, close, volume (None = unknown), last (latest tick)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.open_bars = {}     # key -> bar still collecting ticks
        self.pending = {}       # key -> finished bars not yet collected
        self.cum_volume = {}    # key -> day volume at the end of the last finished bar
        self.emitted = {}       # key -> last bar handed out by collect()

    def on_tick(self, tick, now: datetime):
        key = (tick["exchange"], tick["token"])
        minute = tick_minute(tick, now)
        price = tick["ltp"]
        with self.lock:
            done = self.emitted.get(key)
            if done and minute <= done["minute"]:
                return                                   # late packet for a minute already written
            bar = self.open_bars.get(key)
            if bar and minute < bar["minute"]:
                return
            if bar and minute > bar["minute"]:
                self._finish(key, bar)
                bar = None
            if bar is None:
                bar = self.open_bars[key] = {"minute": minute, "open": price, "high": price,
                                             "low": price, "close": price}
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["last"] = tick

    def _finish(self, key, bar):
        day_vol = bar["last"].get("volume")
        base = self.cum_volume.get(key)
        # First bar after start-up: day volume before it is unknown -> None (end-of-day backfill fixes it)
        bar["volume"] = max(0, day_vol - base) if day_vol is not None and base is not None else None
        if day_vol is not None:
            self.cum_volume[key] = day_vol
        self.pending.setdefault(key, []).append(bar)

    def collect(self, upto: datetime) -> dict:
        """Close every bar that started before `upto`; return {key: [bars]} oldest first.
        Minutes without trades become flat bars at the previous close with volume 0."""
        with self.lock:
            for key, bar in list(self.open_bars.items()):
                if bar["minute"] < upto:
                    self._finish(key, bar)
                    del self.open_bars[key]
            out = {}
            for key in set(self.pending) | set(self.emitted):
                ref = self.emitted.get(key)
                bars = []
                for b in sorted(self.pending.pop(key, []), key=lambda b: b["minute"]):
                    bars += _flats(ref, b["minute"])
                    bars.append(b)
                    ref = b
                bars += _flats(ref, upto)
                if bars:
                    self.emitted[key] = bars[-1]
                    out[key] = bars
            return out


def _flats(ref, before):
    """Flat bars for the minutes after bar `ref` and before `before`."""
    out = []
    if ref is None:
        return out
    m = ref["minute"] + ONE_MIN
    while m < before:
        c = ref["close"]
        out.append({"minute": m, "open": c, "high": c, "low": c, "close": c, "volume": 0,
                    "last": ref["last"]})
        m += ONE_MIN
    return out
