"""CSV time-series store: one 1-min file per series plus a derived 5-min file."""
import logging
import os
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
INT_SUFFIXES = ("volume", "oi", "_qty", "orders_top5", "qty_top5")


def _agg_for(col: str) -> str:
    if col.endswith("open"):
        return "first"
    if col.endswith("high"):
        return "max"
    if col.endswith("low"):
        return "min"
    if col.endswith("volume"):
        return lambda x: x.sum(min_count=1)   # NaN (not 0) for buckets with no candles
    return "last"   # close, OI, greeks, bids ... = state at end of the 5-min bucket


class Series:
    """Rows keyed by candle start time (IST). Files: <base>_1min.csv and <base>_5min.csv."""

    def __init__(self, base: Path, columns: list):
        self.base = base
        self.columns = columns
        self.rows = {}          # pd.Timestamp -> dict
        self.dirty = False
        f1 = self.path("1min")
        if f1.exists():
            try:
                df = pd.read_csv(f1)
                for rec in df.to_dict("records"):
                    ts = pd.Timestamp(rec.pop("timestamp"))
                    self.rows[ts] = {k: v for k, v in rec.items() if pd.notna(v)}
            except Exception as e:   # corrupt/partial file - keep going, it will be rewritten
                log.warning("Could not read %s: %s", f1, e)

    def path(self, tf: str) -> Path:
        return self.base.parent / f"{self.base.name}_{tf}.csv"

    def last_candle_ts(self, col="close"):
        ts = [t for t, r in self.rows.items() if col in r]
        return max(ts) if ts else None

    def update(self, ts, values: dict):
        ts = pd.Timestamp(ts)
        row = self.rows.setdefault(ts, {})
        for k, v in values.items():
            if v is not None:
                row[k] = v
        self.dirty = True

    def flush(self):
        if not self.dirty or not self.rows:
            return
        df = pd.DataFrame.from_dict(self.rows, orient="index").sort_index()
        df.index.name = "timestamp"
        df = df.reindex(columns=[c for c in self.columns if c != "timestamp"])
        five = df.groupby(df.index.floor("5min")).agg({c: _agg_for(c) for c in df.columns})
        five.index.name = "timestamp"
        self.base.parent.mkdir(parents=True, exist_ok=True)
        ok = self._write(df, self.path("1min")) & self._write(five, self.path("5min"))
        self.dirty = not ok

    @staticmethod
    def _write(df, path: Path) -> bool:
        tmp = path.with_suffix(".tmp")
        out = df.copy()
        for c in out.columns:
            if c.endswith(INT_SUFFIXES):
                try:
                    out[c] = pd.to_numeric(out[c]).round().astype("Int64")
                except (TypeError, ValueError):
                    pass
        out.index = out.index.strftime("%Y-%m-%d %H:%M:%S")
        out.to_csv(tmp, index_label="timestamp")
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            log.warning("%s is locked (open in Excel?) - will retry next cycle", path.name)
            tmp.unlink(missing_ok=True)
            return False


class Store:
    def __init__(self):
        self.series = {}

    def get(self, base: Path, columns: list) -> Series:
        key = str(base)
        if key not in self.series:
            self.series[key] = Series(base, columns)
        return self.series[key]

    def flush_all(self):
        for s in self.series.values():
            s.flush()
