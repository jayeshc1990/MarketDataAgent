"""Live BUY / SELL alerts on Telegram from the forward-tested setups (rules in analysis/setups_backtest.py).

Runs once a minute in market hours (deploy/setup_signals.sh installs the timer). Each run replays today's
collected 1-min option data through the same backtest code, so the alerts follow the backtest exactly:
  BUY     a setup signal on the minute that just closed: place a buy limit at the signal candle's close,
          live for the next 1-min candle (the backtest fills it if that candle trades at or below it)
  NOT FILLED  the next candle never traded at the limit: cancel the order (the backtest skips the signal)
  SELL    an alerted trade hit its exit: option stop, spot back through the level, next level, or 14:45
  SUMMARY the day's alerted trades, once after 14:50
Sent alerts are remembered in <data_dir>/SIGNALS/<date>.json, so nothing is sent twice and a failed send
is retried on the next run. Alerts older than `signals.stale_minutes` are not sent (e.g. after downtime).

Trade Executor (config `executor:`, client in market_agent/executor.py): for `executor.setups` the same run
also sends the trades to the executor on this VPS, which places the orders:
  NEW CALL/PUT  at the signal (with sl = signal close - 30, held at Zerodha as a GTT; reference @ signal close)
  SELL          when the backtest exits (spot level, target, 14:45) or says the limit wouldn't have filled
`GET /status` is read before every decision; a trade that ended at the executor (GTT stop, 15:10, manual
exit) is noticed there. Executor events are posted to Telegram as well (if it is set up).

    python analysis/live_signals.py                        # one run (what the timer does)
    python analysis/live_signals.py --dry-run              # print the messages instead of sending
    python analysis/live_signals.py --replay 2026-11-11    # replay a past day minute by minute (prints only)
    python analysis/live_signals.py --send-test 2026-11-11 # replay it and send its alerts, marked TEST
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import data as D  # noqa: E402
from analysis import setups_backtest as B  # noqa: E402
from market_agent import executor as X  # noqa: E402
from market_agent import telegram  # noqa: E402
from market_agent.config import IST, PROJECT_ROOT, Config  # noqa: E402

SUMMARY_AT = pd.Timedelta("14:50:00")
TEST_SEND_GAP = 1.0      # seconds between test messages (Telegram allows ~20 a minute in a group)
SYMBOL_RE = re.compile(r"^(?P<name>[A-Z]+)(?P<dd>\d{2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})(?P<strike>\d+)(?P<type>CE|PE)$")
SETUP_LABEL = {B.SR_SETUP: "Setup 5", B.SR6_SETUP: "Setup 6", B.SR7_SETUP: "Setup 7"}
EXIT_LABEL = {"option_stop": "option stop (-30)", "spot_back_through_level": "spot back through the level",
              "spot_at_next_level": "spot reached the next level", "forced_14:45": "14:45 square-off",
              "trail_near_target": "spot turned back near the target"}
NEAR_EXIT_SETUPS = {B.SR6_SETUP, B.SR7_SETUP}       # setups with the near-target exit (setups_backtest._sr_exit)
FOOTER = "<i>Forward-test signal from your own rules, not investment advice.</i>"


def settings() -> dict:
    s = Config(PROJECT_ROOT / "config.yaml").raw.get("signals") or {}
    return {"underlying": s.get("underlying", "NIFTY"),
            "setups": s.get("setups", [B.SR_SETUP, B.SR6_SETUP]),
            "stale": pd.Timedelta(minutes=int(s.get("stale_minutes", 3)))}


def exec_settings() -> dict:
    raw = Config(PROJECT_ROOT / "config.yaml").raw
    e, s = raw.get("executor") or {}, raw.get("signals") or {}
    return {"enabled": bool(e.get("enabled", False)), "url": e.get("url", X.DEFAULT_URL),
            "setups": e.get("setups", [B.SR6_SETUP]),
            "stale": pd.Timedelta(minutes=int(e.get("stale_minutes", s.get("stale_minutes", 3))))}


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
    """(trades, pending signals, unfilled signals) from replaying `raw` through the backtest."""
    pending, missed = [], []
    return B.run_setups(raw, index_dir, pending=pending, missed=missed), pending, missed


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
        nxt = pd.Timestamp(signal) + pd.Timedelta("1min")
        lines += [f"Signal candle {signal:%H:%M}: option {_num(signal_close, '.2f')} · spot {_num(spot)}",
                  f"Entry: <b>buy limit {_num(signal_close, '.2f')}</b> during the {nxt:%H:%M} candle; "
                  "cancel if not filled by its end"]
    else:   # the alert comes late: the entry candle has already closed
        lines.append(f"Signal {signal:%H:%M} · limit filled {entry:%H:%M} @ {entry_px:.2f}")
    has_target = spot_target is not None and pd.notna(spot_target)
    k = 1 if side == "CE" else -1
    if setup in NEAR_EXIT_SETUPS and has_target:
        target = (f"spot touches {_num(spot_target - k * B.SR_NEAR_TARGET)} (target {_num(spot_target)}) · "
                  f"after spot gets within {B.SR_NEAR_ZONE:.0f} of target, if it falls {B.SR_NEAR_TRAIL:.0f} "
                  "from its best")
    else:
        target = f"spot reaches {_num(spot_target)}" if has_target else None
    exits = [f"spot {below} {_num(spot_stop)}" if spot_stop is not None else None, target,
             "option 30 below entry", "14:45"]
    lines += ["Exit if: " + " · ".join(x for x in exits if x), FOOTER]
    return "\n".join(lines)


def unfilled_message(setup, symbol, signal, limit) -> str:
    return "\n".join([f"⚪ <b>NOT FILLED {html.escape(contract(symbol))}</b> — cancel the buy order",
                      f"{SETUP_LABEL.get(setup, setup)} · signal {pd.Timestamp(signal):%H:%M}: the next candle never "
                      f"traded at the limit {limit:.2f}, so this signal is skipped"])


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
    return {"buy": {}, "sell": {}, "skipped": {}, "unfilled": {}, "summary": False}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _key(setup, signal, symbol) -> str:
    return f"{setup}|{pd.Timestamp(signal):%Y-%m-%d %H:%M}|{symbol}"


def process(trades: pd.DataFrame, pending: list, state: dict, now: pd.Timestamp, send, cfg, missed=()) -> dict:
    """Sends the alerts that are due and records them in `state`. `send(text)` raises on failure, and then
    the alert stays unrecorded so the next run tries again. `missed`: signals whose limit buy wasn't filled."""
    setups, stale = cfg["setups"], cfg["stale"]
    state.setdefault("unfilled", {})
    for x in missed:                     # a BUY went out but the limit never filled: tell the user to cancel
        k = _key(x["setup"], x["signal"], x["symbol"])
        if x["setup"] in setups and k in state["buy"] and k not in state["unfilled"]:
            send(unfilled_message(x["setup"], x["symbol"], x["signal"], x["limit"]))
            state["unfilled"][k] = str(now)
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


# ---------------------------------------------------------------- trade executor
def _exec_note(text: str) -> str:
    return f"🤖 <b>Executor</b> · {text}"


def execute(trades: pd.DataFrame, pending: list, missed, state: dict, now: pd.Timestamp, ex, ecfg, notify) -> dict:
    """Sends NEW / SELL signals to the executor for `ecfg["setups"]` and records them in state["exec"].
    `ex` is a market_agent.executor.Executor; `notify(text)` posts to Telegram (must not raise)."""
    book = state.setdefault("exec", {})
    setups, stale = ecfg["setups"], ecfg["stale"]
    live = {k: v for k, v in book.items() if v["state"] in ("sending", "open", "closing")}
    due = [p for p in pending if p["setup"] in setups and _key(p["setup"], p["signal"], p["symbol"]) not in book]
    if not live and not due:
        return state
    try:
        st = ex.status()
    except X.ExecutorDown as e:
        if not state.get("exec_down"):
            notify(_exec_note(f"⚠️ can't reach it ({html.escape(str(e))}); nothing sent until it's back"))
        state["exec_down"] = str(now)
        return state
    state.pop("exec_down", None)
    held = X.open_trade_id(st)

    # 1. trades of ours that the executor no longer holds ended there (GTT stop, 15:10, manual exit)
    for k, v in live.items():
        if held == v["trade_id"]:
            if v["state"] == "sending":
                v["state"] = "open"
            continue
        if v["state"] == "open":
            notify(_exec_note(f"trade closed at the executor: {html.escape(v['contract'])}"))
        v.update(state="closed" if v["state"] != "sending" else "error", closed=str(now),
                 note=v.get("note") or "ended at the executor")

    # 2. exits: the backtest left the trade, or its limit buy wouldn't have filled
    exits = {_key(x["setup"], x["signal"], x["symbol"]): "limit not filled in the backtest" for x in missed}
    for t in trades.itertuples() if len(trades) else []:
        if t.exit_reason != "data_end_open":
            exits.setdefault(_key(t.setup, t.signal, t.symbol), EXIT_LABEL.get(t.exit_reason, t.exit_reason))
    if now - now.normalize() >= B.FORCED_EXIT:     # by the clock: the 14:45 candle isn't in the data yet
        for k, v in book.items():
            if v["state"] == "open":
                exits.setdefault(k, EXIT_LABEL["forced_14:45"])
    for k, why in exits.items():
        v = book.get(k)
        if not v or v["state"] != "open" or held != v["trade_id"]:
            continue
        try:
            a = ex.signal(X.sell_signal(v["trade_id"]))
        except X.ExecutorDown as e:
            notify(_exec_note(f"⚠️ SELL not delivered ({html.escape(str(e))}); retrying next minute"))
            continue
        if a.accepted:
            v.update(state="closing", sell=str(now), note=why)
            notify(_exec_note(f"🔴 SELL sent ({why}): {html.escape(v['contract'])}"))
        elif a.http == 409 and ("already closed" in a.message or "no open BUY position" in a.message):
            v.update(state="closed", closed=str(now), note=f"{why} (already closed at the executor)")
        else:
            v.update(note=f"SELL refused: HTTP {a.http} {a.message}")
            notify(_exec_note(f"⚠️ SELL refused (HTTP {a.http}): {html.escape(a.message)}. Check the executor"))

    # 3. entries: one trade at a time, only fresh signals
    for p in due:
        k = _key(p["setup"], p["signal"], p["symbol"])
        if k in book:       # the same signal listed again (one candle crossing several levels): first one wins
            continue
        name = contract(p["symbol"])
        if now - p["signal"] > stale:
            book[k] = {"state": "skipped", "note": "stale signal", "contract": name}
            continue
        if held:
            book[k] = {"state": "skipped", "note": f"executor busy with trade {held}", "contract": name}
            notify(_exec_note(f"skipped {html.escape(name)}: a trade ({html.escape(held)}) is still open"))
            continue
        tid = X.new_trade_id(now)
        sig = X.new_signal(tid, p["symbol"], p["signal_close"], p["signal_close"] - B.SR_OPT_STOP)
        book[k] = {"state": "sending", "trade_id": tid, "contract": sig["contract"], "signal": sig, "sent": str(now)}
        try:
            a = ex.signal(sig)
        except X.ExecutorDown as e:
            book[k].update(state="error", note=str(e))
            notify(_exec_note(f"⚠️ BUY not delivered, executor down ({html.escape(str(e))}): "
                              f"{html.escape(sig['contract'])}"))
            continue
        if a.http == 409 and "was already used" in a.message:     # an earlier try got through
            if X.open_trade_id(ex.status()) == tid:
                a = X.Answer(200, {"accepted": True})
        if a.accepted:
            legs = ", ".join(f"{g.get('account')} {g.get('quantity')}" for g in a.body.get("legs", []))
            book[k].update(state="open", symbol=a.body.get("symbol"), legs=a.body.get("legs"))
            held = tid
            notify(_exec_note(f"🟢 BUY sent: {html.escape(sig['contract'])} · SL {sig.get('sl', '-')}"
                              + (f" · {html.escape(legs)}" if legs else "")))
        else:
            kind = "refused" if a.http == 409 else f"rejected (HTTP {a.http}, a bug on this side)"
            book[k].update(state="refused" if a.http == 409 else "rejected", note=f"HTTP {a.http} {a.message}")
            notify(_exec_note(f"⚠️ BUY {kind}: {html.escape(a.message)} · {html.escape(sig['contract'])}"))
    return state


def run_once(dry_run: bool):
    cfg, root = settings(), D.data_root()
    now = pd.Timestamp(datetime.now(IST).replace(tzinfo=None))
    raw = load_day(root, cfg["underlying"], now.date())
    if raw.empty:
        print(f"{now:%H:%M:%S} no data for today yet")
        return
    ecfg = exec_settings()
    tg_on = dry_run or telegram.configured()
    if not tg_on and not ecfg["enabled"]:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env (see market_agent/telegram.py)")
    trades, pending, missed = evaluate(raw, root / cfg["underlying"] / "INDEX")
    path = state_path(root, now.date())
    state = load_state(path)
    sent = []

    def notify(text):                 # executor events: Telegram if possible, never fails the run
        print(text, flush=True)
        if not dry_run and telegram.configured():
            try:
                telegram.send(text)
            except telegram.TelegramError as e:
                print(f"telegram: {e}", flush=True)

    if ecfg["enabled"] and not dry_run:   # orders first: they are the time-critical part
        try:
            execute(trades, pending, missed, state, now, X.Executor(ecfg["url"]), ecfg, notify)
        finally:
            save_state(path, state)
    if not tg_on:
        print(f"{now:%H:%M:%S} Telegram not set up: alerts skipped")
        return

    def send(text):
        print(text if dry_run else f"sending: {text.splitlines()[0]}", flush=True)
        if not dry_run:
            telegram.send(text)
        sent.append(text)

    try:
        process(trades, pending, state, now, send, cfg, missed)
    finally:
        if not dry_run:
            save_state(path, state)       # whatever was sent before a failure is never sent again
    print(f"{now:%H:%M:%S} data to {raw.timestamp.max():%H:%M}, {len(trades)} trade(s) today, {len(sent)} alert(s)")


def replay(day: str, to_telegram: bool = False):
    """Feeds a past day in minute by minute, as the timer would, and prints every alert. With `to_telegram`
    each alert is also sent, marked as a test of that day, so you can see the real messages."""
    cfg, root = settings(), D.data_root()
    day = pd.Timestamp(day).date()
    raw = load_day(root, cfg["underlying"], day)
    if raw.empty:
        print(f"no data for {day}")
        return None, None
    index_dir = root / cfg["underlying"] / "INDEX"
    state = {"buy": {}, "sell": {}, "skipped": {}, "unfilled": {}, "summary": False}
    for t in sorted(raw.timestamp.unique()):
        now = pd.Timestamp(t) + pd.Timedelta("1min 25s")     # timer runs 25 s after the minute's row is written
        if now - now.normalize() > pd.Timedelta("15:00:00"):
            break
        trades, pending, missed = evaluate(raw[raw.timestamp <= t], index_dir)

        def send(text, now=now):
            print(f"--- {now:%H:%M:%S}\n{text}\n", flush=True)
            if to_telegram:
                telegram.send(f"🧪 <b>TEST</b> · alert of {day:%d %b} {now:%H:%M}\n{text}")
                time.sleep(TEST_SEND_GAP)

        process(trades, pending, state, now, send, cfg, missed)
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
    ap.add_argument("--send-test", metavar="YYYY-MM-DD", nargs="+",
                    help="replay past day(s) and send their alerts to Telegram, marked TEST")
    a = ap.parse_args()
    if a.send_test:
        if not telegram.configured():
            sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env (see market_agent/telegram.py)")
        for day in a.send_test:
            print(f"=== {day}: replaying minute by minute (a few minutes per day) ...", flush=True)
            replay(day, to_telegram=True)
    elif a.replay:
        replay(a.replay)
    else:
        run_once(a.dry_run)


if __name__ == "__main__":
    main()
