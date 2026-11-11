"""Forward-test the 4 NIFTY option setups on each day's data.

Run from the dashboard (Backtest tab) or by hand on combined option CSVs (same columns as the
Data explorer / Full_data export: timestamp, symbol, strike, type, open, high, low, close, oi,
delta, underlying_spot, ...). Each run replaces that day's rows in the journal.

    python analysis/setups_backtest.py Full_data_2026-11-02.csv [more.csv ...]

Rules (shared by all setups)
  Entry  : ITM option (|delta| >= 0.55), spot moved >= +10 pts in our direction over the
           last 15 min, and 3-min writer flow in our favour (put OI minus call OI change,
           strikes within +-300 of spot; CE wants > 0, PE wants < 0).
           Signals only 10:00-14:45; contract = the one with delta closest to 0.60.
           Bought on the candle after the signal candle, at that candle's HIGH (worst fill);
           no entry if that candle is 14:45 or later.
  Exit   : checked from the candle after the entry candle: initial stop -15, trailing stop 20
           below the highest high since entry,
           no time exit, forced close at 14:45. One position at a time.
  Setups : 1 no limits | 2 10-min cooldown after exit | 3 cooldown + max 5/day | 4 cooldown + max 3/day

Setup 5 - support/resistance + day trend + PCR (own rules, see run_sr):
  Levels : support = biggest PE-OI strike below spot (within 500), previous day's low and close, first
           15-min low, day's low so far; resistance = the same on the call / high side.
  Event  : on each 5-min spot candle 09:30-14:25: bounce (touches support, closes 5+ above it, green) or
           breakout (closes 5+ above a resistance it was below) -> CE; rejection / breakdown -> PE.
  Filter : spot on the trade's side of the day's open, PCR since the open moved the trade's way, and at
           least 40 pts of room to the next level. Never on the expiry day of the traded expiry.
  Trade  : ITM option (delta 0.55-0.75, closest to 0.62), bought at the high of the next 1-min candle.
           Exit when spot goes 15 pts back through the level, spot reaches the next level, the option
           falls 30, or 14:45. One position at a time, max 3 trades a day.
"""
import re
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ENTRY_START = pd.Timedelta("10:00:00")
FORCED_EXIT = pd.Timedelta("14:45:00")
NEAR_STRIKES = 300
MIN_DELTA, TARGET_DELTA = 0.55, 0.60
SPOT_MOVE_15M = 10
INITIAL_SL, TRAIL = 15.0, 20.0
COST_PTS = 2.0   # brokerage + STT + exchange + GST + stamp + ~1 pt slippage, per round trip
BASELINE_DAYS = {pd.Timestamp(d).date() for d in ("2026-10-28", "2026-10-29", "2026-10-30")}   # rules found here

SETUPS = {
    "1_no_limits": dict(cooldown=0, max_per_day=None),
    "2_cooldown10": dict(cooldown=10, max_per_day=None),
    "3_cooldown10_max5": dict(cooldown=10, max_per_day=5),
    "4_cooldown10_max3": dict(cooldown=10, max_per_day=3),
}
SR_SETUP = "5_sr_trend_pcr"
ALL_SETUPS = list(SETUPS) + [SR_SETUP]
SR_FOUND_UNTIL = pd.Timestamp("2026-11-11").date()     # setup 5's rules were found on data up to this day
NEEDED = ["timestamp", "symbol", "strike", "type", "high", "low", "close", "oi", "delta", "underlying_spot"]


def journal_path() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from analysis.data import data_root
    return data_root() / "BACKTEST" / "setups_journal.csv"


def load_csvs(paths) -> pd.DataFrame:
    return pd.concat([pd.read_csv(p, encoding="utf-8-sig", parse_dates=["timestamp"]) for p in paths])


def prepare(d: pd.DataFrame) -> pd.DataFrame:
    """Clean 1-min option rows and add the signal inputs; all diffs stay inside one day and one contract."""
    missing = [c for c in NEEDED if c not in d]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    d = d[NEEDED].copy()
    d["timestamp"] = pd.to_datetime(d.timestamp)
    d = d[d.oi.notna() & d.close.notna()].drop_duplicates(["timestamp", "symbol"])
    d["day"] = d.timestamp.dt.normalize()
    d["tod"] = d.timestamp - d.day
    d = d.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    d["oid3"] = d.groupby(["day", "symbol"]).oi.diff(3)
    near = d[(d.strike - d.underlying_spot).abs() <= NEAR_STRIKES]
    flow = near.pivot_table(index="timestamp", columns="type", values="oid3", aggfunc="sum")
    flow3 = (flow.get("PE", 0) - flow.get("CE", 0)) / 1e6           # + = put writers adding (bullish)
    spot = d.groupby("timestamp").underlying_spot.first()
    spot_r15 = spot.groupby(spot.index.normalize()).diff(15)
    side = np.where(d.type == "CE", 1, -1)
    d["al_flow3"] = side * d.timestamp.map(flow3).values
    d["al_spot_r15"] = side * d.timestamp.map(spot_r15).values
    d["moneyness"] = side * (d.underlying_spot - d.strike)
    d["adelta"] = d.delta.abs()
    return d


def signals(d):
    m = ((d.adelta >= MIN_DELTA) & (d.al_flow3 > 0) & (d.al_spot_r15 > SPOT_MOVE_15M)
         & (d.moneyness.abs() <= 250) & (d.tod >= ENTRY_START) & (d.tod < FORCED_EXIT))
    s = d[m].assign(dd=(d.adelta - TARGET_DELTA).abs())
    return s.sort_values("dd").groupby(["type", "timestamp"]).head(1).sort_values(["timestamp", "dd"])


def run_exit(g, e, c0):
    """Bought at c0 during candle e; the stop is checked from the candle after e."""
    stop = c0 - INITIAL_SL; peak = c0
    for j in range(e + 1, len(g)):
        if g.tod.iat[j] >= FORCED_EXIT:
            return g.close.iat[j - 1], j - 1, "forced_14:45"
        if g.low.iat[j] <= stop:
            return stop, j, "trail" if stop > c0 - INITIAL_SL else "stop_loss"
        peak = max(peak, g.high.iat[j])
        stop = max(stop, peak - TRAIL)
    return g.close.iat[-1], len(g) - 1, "data_end_open"   # file ended before exit: still open


def backtest(d, sig, cooldown, max_per_day):
    px = {k: g.reset_index(drop=True) for k, g in d.groupby(["day", "symbol"])}
    trades = []
    for day, gd in sig.groupby("day"):
        free_at, count = pd.Timestamp.min, 0
        for _, r in gd.iterrows():
            if r.timestamp <= free_at or (max_per_day and count >= max_per_day):
                continue
            g = px[(day, r.symbol)]
            e = int(np.flatnonzero(g.timestamp.values == r.timestamp.to_datetime64())[0]) + 1   # entry candle
            if e >= len(g) or g.tod.iat[e] >= FORCED_EXIT:
                continue
            entry_px = g.high.iat[e]          # worst fill: the high of the candle after the signal
            exit_px, j, why = run_exit(g, e, entry_px)
            count += 1
            free_at = g.timestamp.iat[j] + pd.Timedelta(minutes=cooldown)
            pnl = exit_px - entry_px
            trades.append(dict(day=day.date(), signal=r.timestamp, entry=g.timestamp.iat[e], exit=g.timestamp.iat[j],
                               symbol=r.symbol, side=r.type, entry_px=entry_px, exit_px=round(exit_px, 2),
                               pnl=round(pnl, 2), net=round(pnl - COST_PTS, 2), mins=j - e, exit_reason=why))
    return pd.DataFrame(trades)


# ---------------------------------------------------------------- setup 5: support/resistance + trend + PCR
SR_TOUCH, SR_PIERCE, SR_CONFIRM = 15, 25, 5     # pts: how close counts as a touch, max pierce, close beyond level
SR_WALL_RANGE, SR_ROOM = 500, 40
SR_SPOT_STOP, SR_OPT_STOP, SR_MAX_PER_DAY = 15, 30.0, 3
SR_DELTA = (0.55, 0.75, 0.62)
SUP_LEVELS = ["oi_sup", "pdl", "pdc", "orl", "dlo_prev"]
RES_LEVELS = ["oi_res", "pdh", "pdc", "orh", "dhi_prev"]
EXPIRY_RE = re.compile(r"^[A-Z]+(\d{2}[A-Z]{3}\d{2})")


def prev_day_levels(days, d: pd.DataFrame, index_dir=None) -> pd.DataFrame:
    """Previous trading day's spot high/low/close for each day, from the INDEX files when given (skipping
    frozen holiday files), else from the option data's own underlying_spot."""
    daily = None
    if index_dir and Path(index_dir).exists():
        frames = [pd.read_csv(f, parse_dates=["timestamp"]) for f in sorted(Path(index_dir).glob("*_INDEX_*_1min.csv"))]
        if frames:
            f = pd.concat(frames).drop_duplicates("timestamp")
            f = f[f.timestamp.dt.time < pd.Timestamp("15:30").time()]
            g = f.groupby(f.timestamp.dt.normalize())
            daily = g.agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))[g.close.nunique() > 5]
    if daily is None or daily.empty:
        s = d.groupby("timestamp").underlying_spot.first()
        s = s[s.index.time < pd.Timestamp("15:30").time()]
        g = s.groupby(s.index.normalize())
        daily = pd.DataFrame({"h": g.max(), "l": g.min(), "c": g.last()})[g.nunique() > 5]
    out = {}
    for day in days:
        before = daily[daily.index < day]
        out[day] = before.iloc[-1].values if len(before) else (np.nan,) * 3
    return pd.DataFrame.from_dict(out, orient="index", columns=["pdh", "pdl", "pdc"])


def _sr_market(d: pd.DataFrame, index_dir=None) -> pd.DataFrame:
    """Per-minute spot, day open, levels and PCR for setup 5."""
    M = pd.DataFrame({"spot": d.groupby("timestamp").underlying_spot.first()})
    M["day"] = M.index.normalize()
    M = M.join(prev_day_levels(sorted(M.day.unique()), d, index_dir), on="day")
    tod = pd.TimedeltaIndex(M.index - M.day.values)
    g = M.groupby("day")
    M["dopen"] = g.spot.transform("first")
    M["dhi_prev"] = g.spot.transform(lambda s: s.cummax().shift(1))
    M["dlo_prev"] = g.spot.transform(lambda s: s.cummin().shift(1))
    first15 = M[tod < pd.Timedelta("09:30:00")].groupby("day").spot.agg(orh="max", orl="min")
    M = M.join(first15, on="day")
    M.loc[tod < pd.Timedelta("09:30:00"), ["orh", "orl"]] = np.nan
    tot = d.pivot_table(index="timestamp", columns="type", values="oi", aggfunc="sum")
    M["pcr"] = tot.get("PE") / tot.get("CE")
    M["pcr_open"] = M.pcr - M.pcr.groupby(M.day).transform("first")
    oi = d.pivot_table(index="timestamp", columns=["type", "strike"], values="oi")
    pe, ce = oi["PE"], oi["CE"].reindex(columns=oi["PE"].columns)
    spot = M.spot.reindex(oi.index).values[:, None]
    k = pe.columns.values[None, :]
    M["oi_sup"] = pe.where((k <= spot + 50) & (k >= spot - SR_WALL_RANGE)).idxmax(axis=1).reindex(M.index)
    M["oi_res"] = ce.where((k >= spot - 50) & (k <= spot + SR_WALL_RANGE)).idxmax(axis=1).reindex(M.index)
    return M


def sr_events(M: pd.DataFrame) -> pd.DataFrame:
    """Bounce / breakout (CE) and rejection / breakdown (PE) at the close of 5-min spot candles."""
    B = M.groupby(M.index.floor("5min")).agg(o=("spot", "first"), h=("spot", "max"), l=("spot", "min"),
                                             c=("spot", "last"), ts=("spot", lambda s: s.index[-1]))
    B["day"] = B.index.normalize(); B["pc"] = B.groupby("day").c.shift(1)
    btod = B.index - B.day
    B = B[(btod >= pd.Timedelta("09:30:00")) & (btod <= pd.Timedelta("14:25:00"))]
    ev = []
    for _, b in B.iterrows():
        m = M.loc[b.ts]
        for n in SUP_LEVELS:
            L = m[n]
            if pd.isna(L):
                continue
            if L - SR_PIERCE <= b.l <= L + SR_TOUCH and b.c >= L + SR_CONFIRM and b.c > b.o:
                ev.append((b.ts, "CE", "bounce", n, L))
            if pd.notna(b.pc) and b.pc >= L and b.c <= L - SR_CONFIRM:
                ev.append((b.ts, "PE", "breakdown", n, L))
        for n in RES_LEVELS:
            L = m[n]
            if pd.isna(L):
                continue
            if L - SR_TOUCH <= b.h <= L + SR_PIERCE and b.c <= L - SR_CONFIRM and b.c < b.o:
                ev.append((b.ts, "PE", "reject", n, L))
            if pd.notna(b.pc) and b.pc <= L and b.c >= L + SR_CONFIRM:
                ev.append((b.ts, "CE", "breakout", n, L))
    E = pd.DataFrame(ev, columns=["ts", "side", "kind", "level", "L"])
    E["day"] = E.ts.dt.normalize()
    keep, last = [], {}
    for i, r in E.sort_values("ts", kind="stable").iterrows():     # same level & kind at most once per 30 min
        key = (r.day, r.side, r.kind, r.level)
        if key in last and r.ts - last[key] < pd.Timedelta("30min"):
            continue
        last[key] = r.ts; keep.append(i)
    return E.loc[keep].reset_index(drop=True)


def _next_level(side, m):
    if side == "CE":
        c = [m[n] for n in RES_LEVELS if pd.notna(m[n]) and m[n] > m.spot + 15]
        return min(c) if c else np.nan
    c = [m[n] for n in SUP_LEVELS if pd.notna(m[n]) and m[n] < m.spot - 15]
    return max(c) if c else np.nan


def _sr_exit(g, e, c0, k, spot_stop, spot_target):
    stop = c0 - SR_OPT_STOP
    for j in range(e + 1, len(g)):
        if g.tod.iat[j] >= FORCED_EXIT:
            return g.close.iat[j - 1], j - 1, "forced_14:45"
        if g.low.iat[j] <= stop:
            return stop, j, "option_stop"
        s = g.underlying_spot.iat[j]
        if k * (s - spot_stop) < 0:
            return g.close.iat[j], j, "spot_back_through_level"
        if pd.notna(spot_target) and k * (s - spot_target) >= 0:
            return g.close.iat[j], j, "spot_at_next_level"
    return g.close.iat[-1], len(g) - 1, "data_end_open"


def run_sr(d: pd.DataFrame, index_dir=None) -> pd.DataFrame:
    """Setup 5 trades on prepared 1-min option data (see the module docstring)."""
    exp = pd.to_datetime(d.symbol.str.extract(EXPIRY_RE)[0], format="%d%b%y")
    d = d[exp.values != d.day.values]                       # skip the traded expiry's own expiry day
    if d.empty:
        return pd.DataFrame()
    M = _sr_market(d, index_dir)
    E = sr_events(M)
    px = {k: g.reset_index(drop=True) for k, g in d.groupby(["day", "symbol"])}
    lo, hi, tgt = SR_DELTA
    trades, free_at, count = [], pd.Timestamp.min, {}
    for _, r in E.iterrows():
        m = M.loc[r.ts]; k = 1 if r.side == "CE" else -1
        room = abs(_next_level(r.side, m) - m.spot)
        if not (k * (m.spot - m.dopen) > 0 and k * m.pcr_open > 0 and room >= SR_ROOM):
            continue
        if r.ts <= free_at or count.get(r.day, 0) >= SR_MAX_PER_DAY:
            continue
        c = d[(d.timestamp == r.ts) & (d.type == r.side) & d.adelta.between(lo, hi)]
        if c.empty:
            continue
        c = c.iloc[(c.adelta - tgt).abs().argmin()]
        g = px[(r.day, c.symbol)]
        e = int(np.flatnonzero(g.timestamp.values == r.ts.to_datetime64())[0]) + 1
        if e >= len(g) or g.tod.iat[e] >= FORCED_EXIT:
            continue
        entry_px = g.high.iat[e]
        exit_px, j, why = _sr_exit(g, e, entry_px, k, r.L - k * SR_SPOT_STOP, _next_level(r.side, m))
        free_at = g.timestamp.iat[j]; count[r.day] = count.get(r.day, 0) + 1
        pnl = exit_px - entry_px
        trades.append(dict(day=r.day.date(), signal=r.ts, entry=g.timestamp.iat[e], exit=g.timestamp.iat[j],
                           symbol=c.symbol, side=r.side, entry_px=entry_px, exit_px=round(exit_px, 2), pnl=round(pnl, 2),
                           net=round(pnl - COST_PTS, 2), mins=j - e, exit_reason=why,
                           detail=f"{r.kind} @ {r.level} {r.L:.0f}"))
    return pd.DataFrame(trades)


def run_setups(raw: pd.DataFrame, index_dir=None) -> pd.DataFrame:
    """All trades of all setups, one row per trade with a `setup` column. `index_dir` (the INDEX folder)
    gives setup 5 the previous day's high/low/close; without it they come from the data's own spot."""
    d = prepare(raw)
    sig = signals(d)
    out = [backtest(d, sig, **cfg).assign(setup=name) for name, cfg in SETUPS.items()]
    out.append(run_sr(d, index_dir).assign(setup=SR_SETUP))
    out = [t for t in out if len(t)]
    cols = ["setup", "day", "signal", "entry", "exit", "symbol", "side", "entry_px", "exit_px", "pnl", "net", "mins",
            "exit_reason", "detail"]
    return pd.concat(out, ignore_index=True).reindex(columns=cols) if out else pd.DataFrame(columns=cols)


def forward_only(j: pd.DataFrame) -> pd.DataFrame:
    """Journal rows from days after each setup's rule-finding days."""
    if j.empty:
        return j
    found = np.where(j.setup == SR_SETUP, SR_FOUND_UNTIL, max(BASELINE_DAYS))
    return j[j.day.values > found]


def scoreboard(trades: pd.DataFrame):
    """(totals per setup, net points per day x setup). Setups with no trades still get a row."""
    t = trades.assign(day=pd.to_datetime(trades.day).dt.date, win=trades.net > 0)
    per_day = t.groupby(["setup", "day"]).net.sum().unstack(0).reindex(columns=ALL_SETUPS).sort_index()
    tot = t.groupby("setup").agg(trades=("net", "size"), win_rate=("win", "mean"), net_per_trade=("net", "mean"),
                                 net_total=("net", "sum"), best=("net", "max"), worst=("net", "min"))
    tot = tot.reindex(ALL_SETUPS)
    tot.insert(0, "days", t.day.nunique())
    tot["trades"] = tot.trades.fillna(0).astype(int)
    tot["net_total"] = tot.net_total.fillna(0)
    tot["losing_days"] = (per_day < 0).sum()
    tot["worst_day"] = per_day.min()
    return tot, per_day


def read_journal(path: Path) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=["setup", "day"])
    j = pd.read_csv(path)
    for c in ("signal", "entry", "exit"):
        if c in j:
            j[c] = pd.to_datetime(j[c])
    j["day"] = pd.to_datetime(j.day).dt.date
    return j


def update_journal(trades: pd.DataFrame, days, path: Path) -> pd.DataFrame:
    """Replace `days` in the journal with `trades` (a day with no trades is simply cleared)."""
    days = {pd.Timestamp(x).date() for x in days}
    j = read_journal(path)
    j = j[~j.day.isin(days)]
    j = pd.concat([x for x in (j, trades) if len(x)], ignore_index=True) if len(j) or len(trades) else j
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    j.sort_values(["setup", "entry"]).to_csv(path, index=False)
    return read_journal(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="combined option CSV(s)")
    ap.add_argument("--journal", help="default: <data_dir>/BACKTEST/setups_journal.csv")
    ap.add_argument("--no-save", action="store_true", help="print only, don't update the journal")
    ap.add_argument("--index-dir", help="INDEX folder for previous-day levels (default: <data_dir>/NIFTY/INDEX)")
    a = ap.parse_args()

    raw = load_csvs(a.csv)
    trades = run_setups(raw, a.index_dir or journal_path().parent.parent / "NIFTY" / "INDEX")
    for name in ALL_SETUPS:
        t = trades[trades.setup == name]
        print(f"\n### {name}: {len(t)} trades, net {t.net.sum():+.1f} pts")
        if len(t):
            show = t.assign(signal=t.signal.dt.strftime("%d-%b %H:%M"), entry=t.entry.dt.strftime("%H:%M"),
                            exit=t.exit.dt.strftime("%H:%M"))
            print(show.drop(columns=["setup", "day"]).to_string(index=False))
    if a.no_save:
        return
    path = Path(a.journal) if a.journal else journal_path()
    j = update_journal(trades, pd.to_datetime(raw.timestamp).dt.normalize().unique(), path)
    tot, per_day = scoreboard(j)
    print(f"\n=== Running totals ({path}) ===")
    print(tot.round(2).to_string())
    print("\nNet points per day:")
    print(per_day.round(1).to_string())


if __name__ == "__main__":
    main()
