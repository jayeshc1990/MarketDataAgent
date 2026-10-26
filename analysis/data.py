"""Load collected CSVs for one expiry and compute the metrics the dashboard plots."""
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SYMBOL_RE = re.compile(r"^(?P<name>[A-Z]+)(?P<exp>\d{2}[A-Z]{3}\d{2})(?P<strike>\d+(?:\.\d+)?)(?P<type>CE|PE)_"
                       r"(?P<tf>1min|5min)\.csv$")
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BUILDUP_LABELS = {  # (price up?, OI up?) -> label
    (True, True): "Long build-up",
    (False, True): "Short build-up",
    (True, False): "Short covering",
    (False, False): "Long unwinding",
}


def data_root() -> Path:
    """MDA_DATA_DIR env var, else `data_dir` from config.yaml."""
    if os.environ.get("MDA_DATA_DIR"):
        return Path(os.environ["MDA_DATA_DIR"])
    from market_agent.config import Config
    return Config(PROJECT_ROOT / "config.yaml").data_dir


def list_underlyings(root: Path) -> list:
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and any(DATE_DIR_RE.match(c.name) for c in p.iterdir() if c.is_dir()))


def list_expiries(root: Path, underlying: str) -> list:
    base = root / underlying
    return sorted((p.name for p in base.iterdir() if p.is_dir() and DATE_DIR_RE.match(p.name)), reverse=True)


def files_signature(folder: Path) -> tuple:
    """Changes whenever a CSV in the folder is rewritten - used as a cache key."""
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in folder.glob("*.csv")))


def load_options(root: Path, underlying: str, expiry: str, tf: str) -> pd.DataFrame:
    rows = []
    for path in (root / underlying / expiry).glob(f"*_{tf}.csv"):
        m = SYMBOL_RE.match(path.name)
        if not m:
            continue
        df = pd.read_csv(path, parse_dates=["timestamp"])
        if df.empty:
            continue
        df["symbol"] = path.name.rsplit("_", 1)[0]
        df["strike"] = float(m["strike"])
        df["type"] = m["type"]
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True).sort_values(["timestamp", "strike", "type"])
    df["date"] = df["timestamp"].dt.date
    return df.reset_index(drop=True)


def load_index(root: Path, underlying: str, dates, tf: str) -> pd.DataFrame:
    frames = []
    for d in sorted(dates):
        path = root / underlying / "INDEX" / f"{underlying}_INDEX_{d}_{tf}.csv"
        if path.exists():
            frames.append(pd.read_csv(path, parse_dates=["timestamp"]))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    df["date"] = df["timestamp"].dt.date
    return df


# ---------------------------------------------------------------- metrics
def spot_series(opts: pd.DataFrame, idx: pd.DataFrame) -> pd.Series:
    """Spot close per timestamp: index file, else the options' underlying_spot column."""
    if not idx.empty and "close" in idx:
        s = idx.set_index("timestamp")["close"].dropna()
        if not s.empty:
            return s
    return opts.groupby("timestamp")["underlying_spot"].last().dropna()


def pcr(opts: pd.DataFrame) -> pd.DataFrame:
    g = opts.groupby(["timestamp", "type"])[["oi", "volume"]].sum().unstack("type")
    out = pd.DataFrame(index=g.index)
    out["pcr_oi"] = g[("oi", "PE")] / g[("oi", "CE")]
    out["pcr_volume"] = g[("volume", "PE")] / g[("volume", "CE")]
    return out.replace([np.inf, -np.inf], np.nan)


def max_pain(opts: pd.DataFrame) -> pd.Series:
    """Strike where option writers' payout is smallest (tracked strikes only)."""
    out = {}
    for ts, g in opts.dropna(subset=["oi"]).groupby("timestamp"):
        strikes = g["strike"].unique()
        ce = g[g["type"] == "CE"][["strike", "oi"]].to_numpy()
        pe = g[g["type"] == "PE"][["strike", "oi"]].to_numpy()
        pain = [(np.clip(k - ce[:, 0], 0, None) * ce[:, 1]).sum() + (np.clip(pe[:, 0] - k, 0, None) * pe[:, 1]).sum()
                for k in strikes]
        out[ts] = strikes[int(np.argmin(pain))]
    return pd.Series(out, name="max_pain")


def atm_straddle(opts: pd.DataFrame, spot: pd.Series) -> pd.DataFrame:
    """ATM strike, CE+PE premium and average IV per timestamp."""
    wide = opts.pivot_table(index=["timestamp", "strike"], columns="type", values=["close", "iv"])
    rows = {}
    for ts, s in spot.items():
        if ts not in wide.index.get_level_values(0):
            continue
        chain = wide.loc[ts]
        k = chain.index[np.argmin(np.abs(chain.index.to_numpy() - s))]
        r = chain.loc[k]
        rows[ts] = {"atm_strike": k, "straddle": r.get(("close", "CE"), np.nan) + r.get(("close", "PE"), np.nan),
                    "atm_iv": np.nanmean([r.get(("iv", "CE"), np.nan), r.get(("iv", "PE"), np.nan)])}
    return pd.DataFrame.from_dict(rows, orient="index")


def oi_change_matrix(opts: pd.DataFrame, typ: str, since: str = "day") -> pd.DataFrame:
    """strike x timestamp OI change vs the first value of the day (or of the whole selection)."""
    d = opts[opts["type"] == typ].pivot_table(index="strike", columns="timestamp", values="oi")
    if since == "selection":
        return d.sub(d.bfill(axis=1).iloc[:, 0], axis=0)
    out = []
    for day, cols in pd.Series(d.columns, index=d.columns).groupby(lambda c: c.date()):
        block = d[cols.values]
        out.append(block.sub(block.bfill(axis=1).iloc[:, 0], axis=0))
    return pd.concat(out, axis=1)


def buildup(opts: pd.DataFrame, t0, t1) -> pd.DataFrame:
    """Price and OI change per contract between two times, with the classic build-up label."""
    a = opts[opts["timestamp"] == t0].set_index(["strike", "type"])
    b = opts[opts["timestamp"] == t1].set_index(["strike", "type"])
    df = pd.DataFrame({"price_start": a["close"], "price_end": b["close"],
                       "oi_start": a["oi"], "oi_end": b["oi"]}).dropna()
    df["price_chg_%"] = (df["price_end"] / df["price_start"] - 1) * 100
    df["oi_chg"] = df["oi_end"] - df["oi_start"]
    df["oi_chg_%"] = (df["oi_end"] / df["oi_start"] - 1) * 100
    df["buildup"] = [BUILDUP_LABELS[(p > 0, o > 0)] for p, o in zip(df["price_chg_%"], df["oi_chg"])]
    return df.reset_index()


def buildup_timeline(opts: pd.DataFrame, typ: str, window: int) -> pd.DataFrame:
    """strike x timestamp build-up label over a rolling window of `window` bars (within each day)."""
    d = opts[opts["type"] == typ].sort_values("timestamp")
    price = d.pivot_table(index="timestamp", columns="strike", values="close")
    oi = d.pivot_table(index="timestamp", columns="strike", values="oi")
    day = price.index.date
    dp = price.groupby(day).diff(window)
    doi = oi.groupby(day).diff(window)
    lab = pd.DataFrame(np.nan, index=price.index, columns=price.columns, dtype=object)
    for (pu, ou), name in BUILDUP_LABELS.items():
        mask = ((dp > 0) == pu) & ((doi > 0) == ou) & dp.notna() & doi.notna() & (doi != 0)
        lab = lab.mask(mask, name)
    return lab.T


def gamma_exposure(opts: pd.DataFrame, ts, spot: float) -> pd.DataFrame:
    """Per-strike gamma exposure (Rs per 1% move), assuming option writers are short:
    calls counted +, puts -. Large positive = market makers dampen moves near that strike."""
    g = opts[opts["timestamp"] == ts].dropna(subset=["gamma", "oi"])
    sign = np.where(g["type"] == "CE", 1, -1)
    gex = g["gamma"] * g["oi"] * spot * spot * 0.01 * sign
    return pd.DataFrame({"strike": g["strike"], "type": g["type"], "gex": gex}).groupby("strike")["gex"].sum()


def order_flow(opts: pd.DataFrame, spot: pd.Series) -> pd.DataFrame:
    """Bid/ask imbalance (total buy vs sell qty) for calls and puts, and a combined signal:
    + means traders are bidding calls and offering puts (bullish), - the opposite."""
    d = opts.dropna(subset=["total_buy_qty", "total_sell_qty"])
    g = d.groupby(["timestamp", "type"])[["total_buy_qty", "total_sell_qty"]].sum().unstack("type")
    out = pd.DataFrame(index=g.index)
    for t in ("CE", "PE"):
        b, s = g[("total_buy_qty", t)], g[("total_sell_qty", t)]
        out[f"{t.lower()}_imbalance"] = (b - s) / (b + s)
    out["signal"] = out["ce_imbalance"] - out["pe_imbalance"]
    out["spot"] = spot.reindex(out.index)
    return out


def lead_lag(flow: pd.DataFrame, max_lead: int = 30) -> pd.Series:
    """Correlation of the flow signal now with the spot return over the next n bars (same day only)."""
    res = {}
    day = flow.index.date
    for n in range(1, max_lead + 1):
        fwd = flow["spot"].groupby(day).shift(-n) / flow["spot"] - 1
        ok = flow["signal"].notna() & fwd.notna()
        res[n] = flow["signal"][ok].corr(fwd[ok]) if ok.sum() > 10 else np.nan
    return pd.Series(res, name="corr")


def smile(opts: pd.DataFrame, ts) -> pd.DataFrame:
    g = opts[opts["timestamp"] == ts]
    return g.pivot_table(index="strike", columns="type", values="iv")
