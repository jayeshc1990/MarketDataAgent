"""Multi-day support / resistance levels from daily index history.

For a trading day it uses only days before it:
  ema9 ... ema200   daily EMAs of the close (9, 21, 50, 100, 200)
  pwh / pwl         previous week's high / low
  pmh / pml         previous month's high / low
  zone<price>       swing zones: daily swing highs and lows (the extreme of 3 days either side) of the last
                    year, merged when within 0.25% of each other, kept when they hold 2+ swing points;
                    `strength` = how many swing points it holds
Levels further than 5% from the last close are left out.

Daily candles come from <data>/<INDEX>/HISTORY/<INDEX>_1day.csv (fetch_history.py), with days it lacks
filled from the collector's INDEX 1-min files (frozen holiday files skipped).
"""
from pathlib import Path

import numpy as np
import pandas as pd

EMA_PERIODS = (9, 21, 50, 100, 200)
SWING_WINDOW = 3          # days on each side a swing high / low must beat
SWING_LOOKBACK = 250      # trading days of swing points used (about a year)
ZONE_PCT = 0.25           # swing points within this % of each other form one zone
MIN_ZONE_POINTS = 2       # a zone needs at least this many swing points (price turned there more than once)
MAX_DIST_PCT = 5.0        # levels kept within this % of the last close


def load_daily(index_dir) -> pd.DataFrame:
    """Daily open/high/low/close indexed by date, for the index whose folder holds HISTORY/ and INDEX/."""
    base = Path(index_dir)
    if base.name == "INDEX":
        base = base.parent
    name = base.name
    frames = []
    hist = base / "HISTORY" / f"{name}_1day.csv"
    if hist.exists():
        h = pd.read_csv(hist, parse_dates=["timestamp"])
        h.index = h.timestamp.dt.normalize()
        frames.append(h[["open", "high", "low", "close"]])
    files = sorted((base / "INDEX").glob(f"{name}_INDEX_*_1min.csv"))
    if files:
        f = pd.concat(pd.read_csv(p, parse_dates=["timestamp"]) for p in files).drop_duplicates("timestamp")
        f = f[f.timestamp.dt.time < pd.Timestamp("15:30").time()]
        g = f.groupby(f.timestamp.dt.normalize())
        d = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        frames.append(d[g.close.nunique() > 5])                    # frozen (holiday) files skipped
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    daily = pd.concat(frames)
    return daily[~daily.index.duplicated(keep="first")].sort_index()  # HISTORY wins where both have a day


def _swing_zones(past: pd.DataFrame) -> list:
    w = SWING_WINDOW
    p = past.tail(SWING_LOOKBACK + w)
    span = 2 * w + 1
    hi = p.high[p.high == p.high.rolling(span, center=True).max()]
    lo = p.low[p.low == p.low.rolling(span, center=True).min()]
    pts = np.sort(np.concatenate([hi.values, lo.values]))
    zones, cur = [], []
    for x in pts:
        if cur and (x - cur[0]) / cur[0] * 100 > ZONE_PCT:
            zones.append(cur); cur = []
        cur.append(x)
    if cur:
        zones.append(cur)
    return [(f"zone{np.mean(z):.0f}", float(np.mean(z)), "swing zone", len(z)) for z in zones
            if len(z) >= MIN_ZONE_POINTS]


def levels_for(daily: pd.DataFrame, day) -> pd.DataFrame:
    """Levels for trading `day` (columns name, level, kind, strength), from the days before it."""
    day = pd.Timestamp(day).normalize()
    past = daily[daily.index < day]
    cols = ["name", "level", "kind", "strength"]
    if past.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for n in EMA_PERIODS:
        if len(past) >= n:
            rows.append((f"ema{n}", past.close.ewm(span=n, adjust=False).mean().iloc[-1], f"daily EMA {n}", 1))
    for per, tag, label in (("W", "w", "week"), ("M", "m", "month")):
        cur = day.to_period(per)
        prev = past[past.index.to_period(per) < cur]
        if len(prev):
            last = prev[prev.index.to_period(per) == prev.index[-1].to_period(per)]
            rows += [(f"p{tag}h", last.high.max(), f"previous {label} high", 1),
                     (f"p{tag}l", last.low.min(), f"previous {label} low", 1)]
    rows += _swing_zones(past)
    out = pd.DataFrame(rows, columns=cols)
    close = past.close.iloc[-1]
    return out[(out.level - close).abs() / close * 100 <= MAX_DIST_PCT].sort_values("level").reset_index(drop=True)
