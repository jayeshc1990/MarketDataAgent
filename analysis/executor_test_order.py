"""One TEST order to the Trade Executor: a cheap, far out-of-the-money NIFTY call from today's data.
Asks before sending; afterwards closes the test trade with a SELL so it can't block the next real signal.
    .venv/bin/python analysis/executor_test_order.py [--day YYYY-MM-DD] [--url URL] [--yes]"""
import argparse, secrets, sys, time
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import data as D, live_signals as L
from market_agent import executor as X

ap = argparse.ArgumentParser()
ap.add_argument("--day", default=str(pd.Timestamp.now().date()))
ap.add_argument("--url", default=None)
ap.add_argument("--yes", action="store_true")
a = ap.parse_args()
day = pd.Timestamp(a.day).date()
root = D.data_root()
raw = L.load_day(root, "NIFTY", day)
if raw.empty:
    sys.exit(f"no NIFTY option data for {day}")
expiry = min(e for e in D.list_expiries(root, "NIFTY") if e >= str(day))
last = raw[raw.symbol.str.contains(pd.Timestamp(expiry).strftime("%d%b%y").upper()) & (raw.type == "CE")]
last = last.sort_values("timestamp").groupby("symbol").tail(1)
pick = last[last.close >= 2].sort_values("close").iloc[0]        # cheapest call still trading at >= Rs 2

ex = X.Executor(a.url or X.config_url())
st = ex.status()
print(f"executor {ex.url}: mode={st.get('mode')} open={st.get('open')}")
if st.get("open"):
    sys.exit("a trade is open at the executor - not sending a test")
tid = f"TEST-{pd.Timestamp.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
sig = X.new_signal(tid, pick.symbol, float(pick.close))
X.validate(sig)
print("signal:", sig, f"\n(last price {pick.close} at {pick.timestamp:%H:%M}; 1 lot is about Rs {pick.close * 65:,.0f})")
if not a.yes and input("Type YES to send this to the executor: ").strip() != "YES":
    sys.exit("not sent")
ans = ex.signal(sig)
print(f"answer: HTTP {ans.http} {ans.body}")
if not ans.accepted:
    sys.exit(0)
for _ in range(10):                                              # watch it for ~20 s
    time.sleep(2)
    st = ex.status()
    legs = [(l.get("account"), l.get("state"), l.get("filled_qty"), l.get("message") or l.get("exit_reason"))
            for l in (st.get("trade") or {}).get("legs", [])]
    print(f"status: open={st.get('open')} trade={X.open_trade_id(st)} legs={legs}")
    if not st.get("open"):
        break
if X.open_trade_id(st) == tid:
    s = ex.signal(X.sell_signal(tid))
    print(f"closing the test trade: HTTP {s.http} {s.body}")
    for _ in range(10):
        time.sleep(2)
        st = ex.status()
        if not st.get("open"):
            break
    print("final status:", st)
