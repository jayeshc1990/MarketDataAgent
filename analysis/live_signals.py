"""Live BUY / SELL alerts on Telegram from the forward-tested setups (rules in analysis/setups_backtest.py).

Runs once a minute in market hours (deploy/setup_signals.sh installs the timer). Each run replays today's
collected 1-min option data through the same backtest code, so the alerts follow the backtest exactly:
  BUY     a setup signal on the minute that just closed (the backtest buys at the high of the next candle)
  SELL    an alerted trade hit its exit: option stop, spot back through the level, next level, or 14:45
  SUMMARY the day's alerted trades, once after 14:50
Sent alerts are remembered in <data_dir>/SIGNALS/<date>.json, so nothing is sent twice and a failed send
is retried on the next run. Alerts older than `signals.stale_minutes` are not sent (e.g. after downtime).

    python analysis/live_signals.py                        # one run (what the timer does)
    python analysis/live_signals.py --dry-run              # print the messages instead of sending
    python analysis/live_signals.py --replay 2026-11-11    # replay a past day minute by minute (prints only)
"""
import argparse
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import data as D  # noqa: E402
from analysis import setups_backtest as B  # noqa: E402
from market_agent import telegram  # noqa: E402
from market_agent.config import IST, PROJECT_ROOT, Config  # noqa: E402

SUMMARY_AT = pd.Timedelta("14:50:00")
SYMBOL_RE = re.compile(r"^(?P<name>[A-Z]+)(?P<dd>\d{2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})(?P<strike>\d+)(?P<type>CE|PE)$")
SETUP_LABEL = {B.SR_SETUP: "Setup 5", B.SR6_SETUP: "Setup 6"}
EXIT_LABEL = {"option_stop": "option stop (-30)", "spot_back_through_level": "spot back through the level",
              "spot_at_next_level": "spot reached the next level", "forced_14:45": "14:45 square-off"}
FOOTER = "<i>Forward-test signal from your own rules, not investment advice.</i>"


def settings() -> dict:
    s = Config(PROJECT_ROOT / "config.yaml").raw.get("signals") or {}
    return {"underlying": s.get("underlying", "NIFTY"),
            "setups": s.get("setups", [B.SR_SETUP, B.SR6_SETUP]),
            "stale": pd.Timedelta(minutes=int(s.get("stale_minutes", 3)))}


def load_day(root: Path, underlying: str, day) -> pd.DataFrame:
    """1-min rows of `day` from every expiry folder that hasn't expired before it."""
    frames = []
    for expiry in D.list_expiries(root, underlying):
        if expiry < str(day):
            continue
        o = D.load_options(root, underlying, expiry, "1min")
        if not o.empty:
            frames.append(o[o["date"] == day])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def evaluate(raw: pd.DataFrame, index_dir: Path):
    """(trades, pending signals) from replaying `raw` through the backtest."""
    pending = []
    return B.run_setups(raw, index_dir, pending=pending), pending


# ---------------------------------------------------------------- messages
def contract(symbol: str) -> str:
    m = SYMBOL_RE.match(symbol)
    return f"{m['name']} {m['strike']} {m['type']} ({m['dd']} {m['mon']})" if m else symbol


def _num(x, fmt=",.0f"):
    return format(x, fmt) if pd.notna(x) else "-"


def buy_message(setup, symbol, signal, detail, side, spot=None, spot_stop=None, spot_target=None,
                signal_close=None, entry=None, entry_px=None) -> str:
    below = "below" if side == "CE" else "above"
    lines = [f"🟢 <b>BUY {html.escape(contract(symbol))}</b>",
             f"{SETUP_LABEL.get(setup, setup)} · {html.escape(detail)}"]
    if entry_px is None:
        lines += [f"Signal candle {signal:%H:%M}: option {_num(signal_close, '.2f')} · spot {_num(spot)}",
                  "Entry: buy on the next 1-min candle (backtest fills at its high)"]
    else:   # the alert comes late: the entry candle has already closed
        lines.append(f"Signal {signal:%H:%M} · backtest entry {entry:%H:%M} @ {entry_px:.2f}")
    exits = [f"spot {below} {_num(spot_stop)}" if spot_stop is not None else None,
             f"spot reaches {_num(spot_target)}" if spot_target is not None and pd.notna(spot_target) else None,
             "option 30 below entry", "14:45"]
    lines += ["Exit if: " + " · ".join(x for x in exits if x), FOOTER]
    return "\n".join(lines)


def sell_message(t) -> str:
    sign = "🟩" if t.net > 0 else "🟥"
    return "\n".join([
        f"🔴 <b>SELL {html.escape(contract(t.symbol))}</b> — {EXIT_LABEL.get(t.exit_reason, t.exit_reason)}",
        f"{SETUP_LABEL.get(t.setup, t.setup)} · entry {t.entry:%H:%M} @ {t.entry_px:.2f} → "
        f"exit {t.exit:%H:%M} @ {t.exit_px:.2f}",
        f"{sign} P&amp;L {t.pnl:+.2f} pts (net {t.net:+.2f} after costs)"])


def summary_message(day, closed: pd.DataFrame, setups) -> str:
    lines = [f"📋 <b>Signals {day:%d %b %Y}</b> ({', '.join(SETUP_LABEL.get(s, s) for s in setups)})"]
    if closed.empty:
        lines.append("No trades today.")
    for t in closed.sort_values("entry").itertuples():
        lines.append(f"{SETUP_LABEL.get(t.setup, t.setup)} · {contract(t.symbol)} · {t.entry:%H:%M}→{t.exit:%H:%M}"
                     f" · net {t.net:+.1f}")
    if not closed.empty:
        lines.append(f"<b>Total net {closed.net.sum():+.1f} pts</b> over {len(closed)} trade(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------- state + one run
def state_path(root: Path, day) -> Path:
    return root / "SIGNALS" / f"{day}.json"


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"buy": {}, "sell": {}, "skipped": {}, "summary": False}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _key(setup, signal, symbol) -> str:
    return f"{setup}|{pd.Timestamp(signal):%Y-%m-%d %H:%M}|{symbol}"


def process(trades: pd.DataFrame, pending: list, state: dict, now: pd.Timestamp, send, cfg) -> dict:
    """Sends the alerts that are due and records them in `state`. `send(text)` raises on failure, and then
    the alert stays unrecorded so the next run tries again."""
    setups, stale = cfg["setups"], cfg["stale"]
    for p in pending:
        k = _key(p["setup"], p["signal"], p["symbol"])
        if p["setup"] not in setups or k in state["buy"] or k in state["skipped"]:
            continue
        if now - p["signal"] > stale:
            state["skipped"][k] = "stale signal"
            continue
        send(buy_message(p["setup"], p["symbol"], p["signal"], p["detail"], p["side"], p["spot"], p["spot_stop"],
                         p["spot_target"], signal_close=p["signal_close"]))
        state["buy"][k] = str(now)
    for t in trades[trades.setup.isin(setups)].itertuples() if len(trades) else []:
        k = _key(t.setup, t.signal, t.symbol)
        if k in state["skipped"]:
            continue
        if k not in state["buy"]:        # no BUY yet (e.g. the runner was down at the signal minute)
            if now - t.entry > stale:
                state["skipped"][k] = "stale entry"
                continue
            send(buy_message(t.setup, t.symbol, t.signal, t.detail, t.side, entry=t.entry, entry_px=t.entry_px))
            state["buy"][k] = str(now)
        if t.exit_reason != "data_end_open" and k not in state["sell"]:
            send(sell_message(t))
            state["sell"][k] = {"exit": str(t.exit), "net": t.net}
    if not state["summary"] and now - now.normalize() >= SUMMARY_AT:
        alerted = trades[[_key(t.setup, t.signal, t.symbol) in state["sell"] for t in trades.itertuples()]] \
            if len(trades) else trades
        send(summary_message(now.date(), alerted, setups))
        state["summary"] = True
    return state


def run_once(dry_run: bool):
    cfg, root = settings(), D.data_root()
    now = pd.Timestamp(datetime.now(IST).replace(tzinfo=None))
    raw = load_day(root, cfg["underlying"], now.date())
    if raw.empty:
        print(f"{now:%H:%M:%S} no data for today yet")
        return
    if not dry_run and not telegram.configured():
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env (see market_agent/telegram.py)")
    trades, pending = evaluate(raw, root / cfg["underlying"] / "INDEX")
    path = state_path(root, now.date())
    state = load_state(path)
    sent = []

    def send(text):
        print(text if dry_run else f"sending: {text.splitlines()[0]}", flush=True)
        if not dry_run:
            telegram.send(text)
        sent.append(text)

    try:
        process(trades, pending, state, now, send, cfg)
    finally:
        if not dry_run:
            save_state(path, state)       # whatever was sent before a failure is never sent again
    print(f"{now:%H:%M:%S} data to {raw.timestamp.max():%H:%M}, {len(trades)} trade(s) today, {len(sent)} alert(s)")


def replay(day: str):
    """Feeds a past day in minute by minute, as the timer would, and prints every alert."""
    cfg, root = settings(), D.data_root()
    day = pd.Timestamp(day).date()
    raw = load_day(root, cfg["underlying"], day)
    if raw.empty:
        sys.exit(f"no data for {day}")
    index_dir = root / cfg["underlying"] / "INDEX"
    state = {"buy": {}, "sell": {}, "skipped": {}, "summary": False}
    for t in sorted(raw.timestamp.unique()):
        now = pd.Timestamp(t) + pd.Timedelta("1min 25s")     # timer runs 25 s after the minute's row is written
        if now - now.normalize() > pd.Timedelta("15:00:00"):
            break
        trades, pending = evaluate(raw[raw.timestamp <= t], index_dir)

        def send(text, now=now):
            print(f"--- {now:%H:%M:%S}\n{text}\n")

        process(trades, pending, state, now, send, cfg)
    final = B.run_setups(raw, index_dir)
    final = final[final.setup.isin(cfg["setups"])]
    print("=== backtest on the full day:")
    print(final[["setup", "signal", "entry", "exit", "symbol", "entry_px", "exit_px", "net", "exit_reason"]]
          .to_string(index=False) if len(final) else "no trades")
    return state, final


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the messages instead of sending them")
    ap.add_argument("--replay", metavar="YYYY-MM-DD", help="replay a past day minute by minute (prints only)")
    a = ap.parse_args()
    if a.replay:
        replay(a.replay)
    else:
        run_once(a.dry_run)


if __name__ == "__main__":
    main()
