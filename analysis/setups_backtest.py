"""Forward-test the 4 NIFTY option setups on each day's data.

Run from the dashboard (Backtest tab) or by hand on combined option CSVs (same columns as the
Data explorer / Full_data export: timestamp, symbol, strike, type, open, high, low, close, oi,
delta, underlying_spot, ...). Each run replaces that day's rows in the journal.

    python analysis/setups_backtest.py Full_data_2026-11-02.csv [more.csv ...]

Rules (shared by all setups)
  Entry  : ITM option (|delta| >= 0.55), spot moved >= +10 pts in our direction over the
           last 15 min, and 3-min writer flow in our favour (put OI minus call OI change,
           strikes within +-300 of spot; CE wants > 0, PE wants < 0).
           Entries only 10:00-14:45; contract = the one with delta closest to 0.60.
  Exit   : initial stop -15, trailing stop 20 below the highest high since entry,
           no time exit, forced close at 14:45. One position at a time.
  Setups : 1 no limits | 2 10-min cooldown after exit | 3 cooldown + max 5/day | 4 cooldown + max 3/day
"""
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


def run_exit(g, i):
    c0 = g.close.iat[i]; stop = c0 - INITIAL_SL; peak = c0
    for j in range(i + 1, len(g)):
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
            i = int(np.flatnonzero(g.timestamp.values == r.timestamp.to_datetime64())[0])
            exit_px, j, why = run_exit(g, i)
            count += 1
            free_at = g.timestamp.iat[j] + pd.Timedelta(minutes=cooldown)
            pnl = exit_px - r.close
            trades.append(dict(day=day.date(), entry=r.timestamp, exit=g.timestamp.iat[j], symbol=r.symbol,
                               side=r.type, entry_px=r.close, exit_px=round(exit_px, 2), pnl=round(pnl, 2),
                               net=round(pnl - COST_PTS, 2), mins=j - i, exit_reason=why))
    return pd.DataFrame(trades)


def run_setups(raw: pd.DataFrame) -> pd.DataFrame:
    """All trades of all 4 setups, one row per trade with a `setup` column."""
    d = prepare(raw)
    sig = signals(d)
    out = [backtest(d, sig, **cfg).assign(setup=name) for name, cfg in SETUPS.items()]
    out = [t for t in out if len(t)]
    cols = ["setup", "day", "entry", "exit", "symbol", "side", "entry_px", "exit_px", "pnl", "net", "mins",
            "exit_reason"]
    return pd.concat(out, ignore_index=True)[cols] if out else pd.DataFrame(columns=cols)


def scoreboard(trades: pd.DataFrame):
    """(totals per setup, net points per day x setup). Setups with no trades still get a row."""
    t = trades.assign(day=pd.to_datetime(trades.day).dt.date, win=trades.net > 0)
    per_day = t.groupby(["setup", "day"]).net.sum().unstack(0).reindex(columns=list(SETUPS)).sort_index()
    tot = t.groupby("setup").agg(trades=("net", "size"), win_rate=("win", "mean"), net_per_trade=("net", "mean"),
                                 net_total=("net", "sum"), best=("net", "max"), worst=("net", "min"))
    tot = tot.reindex(list(SETUPS))
    tot.insert(0, "days", t.day.nunique())
    tot["trades"] = tot.trades.fillna(0).astype(int)
    tot["net_total"] = tot.net_total.fillna(0)
    tot["losing_days"] = (per_day < 0).sum()
    tot["worst_day"] = per_day.min()
    return tot, per_day


def read_journal(path: Path) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=["setup", "day"])
    j = pd.read_csv(path, parse_dates=["entry", "exit"])
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
    a = ap.parse_args()

    raw = load_csvs(a.csv)
    trades = run_setups(raw)
    for name in SETUPS:
        t = trades[trades.setup == name]
        print(f"\n### {name}: {len(t)} trades, net {t.net.sum():+.1f} pts")
        if len(t):
            show = t.assign(entry=t.entry.dt.strftime("%d-%b %H:%M"), exit=t.exit.dt.strftime("%H:%M"))
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
