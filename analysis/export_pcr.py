"""Write put-call ratio files from the collected option CSVs (reads them only; never touches the collector).

    python analysis/export_pcr.py            # expiries not yet expired, only when their CSVs changed
    python analysis/export_pcr.py --all      # rebuild every expiry and day

Output: Data/<INDEX>/PCR/<expiry>/<INDEX>_PCR_<date>_1min.csv (and _5min), one row per bar.
PCR uses every tracked strike of that expiry (the dashboard can narrow it with its strike slider).
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import data as D  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
TIMEFRAMES = ("1min", "5min")
ROUND = {"pcr": 4, "pcr_prev": 4, "pcr_chg": 4, "chg_oi_pcr": 4, "pcr_chg_open": 4, "pcr_volume": 4,
         "pcr_chg_pct": 2, "pcr_chg_open_pct": 2, "spot": 2}


def pcr_path(root: Path, underlying: str, expiry: str, day, tf: str) -> Path:
    return root / underlying / "PCR" / expiry / f"{underlying}_PCR_{day}_{tf}.csv"


def _newest_mtime(paths) -> float:
    return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)


def _write(df, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.round(ROUND)
    for c in ("ce_oi", "pe_oi", "ce_oi_chg", "pe_oi_chg", "ce_volume", "pe_volume"):
        out[c] = out[c].astype("Int64")
    tmp = path.with_suffix(".tmp")
    out.to_csv(tmp)
    os.replace(tmp, path)       # atomic: the dashboard never reads a half-written file


def export(root: Path, underlying: str, expiry: str, tf: str, rebuild: bool) -> int:
    """Writes the PCR files for one expiry/timeframe; returns how many were written."""
    sources = list((root / underlying / expiry).glob(f"*_{tf}.csv"))
    outputs = list((root / underlying / "PCR" / expiry).glob(f"*_{tf}.csv"))
    if not sources or (not rebuild and outputs and _newest_mtime(outputs) >= _newest_mtime(sources)):
        return 0            # nothing new since the last run
    opts = D.load_options(root, underlying, expiry, tf)
    if opts.empty:
        return 0
    days = sorted(opts["date"].unique())
    spot = D.spot_series(opts, D.load_index(root, underlying, days, tf))
    table = D.pcr(opts, spot)
    written = 0
    for day in days:
        path = pcr_path(root, underlying, expiry, day, tf)
        # past days don't change, so only the latest day is rewritten (plus any missing file)
        if rebuild or day == days[-1] or not path.exists():
            _write(table[table.index.date == day], path)
            written += 1
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="rebuild every expiry and day, including expired ones")
    args = ap.parse_args()
    root = D.data_root()
    today = datetime.now(IST).date().isoformat()
    for underlying in D.list_underlyings(root):
        for expiry in D.list_expiries(root, underlying):
            if not args.all and expiry < today:
                continue
            for tf in TIMEFRAMES:
                n = export(root, underlying, expiry, tf, rebuild=args.all)
                if n:
                    print(f"{underlying} {expiry} {tf}: wrote {n} PCR file(s)")


if __name__ == "__main__":
    main()
