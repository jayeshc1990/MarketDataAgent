# MarketDataAgent

Collects Indian F&O market data from **Angel One SmartAPI** every minute, Mon–Fri, 09:15–15:40 IST.

**How (default `collection.mode: stream`):** one WebSocket connection streams live ticks (price, day volume,
OI, total buy/sell qty, best-5 depth) for every tracked instrument. The agent builds each 1-min candle itself
and writes it a few seconds after the minute closes, so every minute is complete and current. Greeks come from
the REST greeks API once a minute. At 15:40 the REST candle API replaces the day's candles with the
exchange's official ones where Angel One allows it (it limits candle calls to ~10–15/min).

## What is collected

| Instrument | Data |
|---|---|
| NIFTY / BANKNIFTY / SENSEX spot | 1-min & 5-min OHLC, LTP (default config: NIFTY only, strikes in multiples of 100) |
| Nearest index future | OHLC, volume, OI, total buy/sell qty |
| INDIA VIX | OHLC |
| Crude oil (nearest MCX CRUDEOIL future) | OHLC, volume, OI |
| Index options (range set in `config.yaml`) | OHLC, volume, OI, IV, delta, gamma, theta, vega, total bid/ask qty, top-5 depth orders & qty, best bid/ask, underlying spot |

Greeks come from Angel One's option-greeks API. When that API has no data (e.g. SENSEX/BFO), they are computed locally with Black-Scholes (`greeks_source` column = `api` / `local`).

## Output layout

```
Data/
  NIFTY/
    INDEX/NIFTY_INDEX_2026-10-23_1min.csv     <- spot + futures (volume/OI), one file per day
    INDEX/NIFTY_INDEX_2026-10-23_5min.csv
    2026-10-28/                               <- option expiry
      NIFTY28OCT2625000CE_1min.csv            <- one pair per contract, grows daily until expiry
      NIFTY28OCT2625000CE_5min.csv
  BANKNIFTY/...   SENSEX/...
  INDIAVIX/INDIAVIX_2026-10-23_1min.csv
  CRUDEOIL/CRUDEOIL_2026-10-23_1min.csv
```

Timestamps are candle **start** times in IST. 5-min files are built from the 1-min candles (O=first, H=max, L=min, C=last, volume=sum; OI, greeks and bids = value at the end of the bucket).
Snapshot columns (OI, greeks, bids, depth) are captured live a few seconds after each minute closes, so they are only filled for minutes the agent was running.

## Setup

```bash
python -m pip install -r requirements.txt
```

1. Create a SmartAPI app at https://smartapi.angelbroking.com/ and enable TOTP.
2. Copy `.env.example` to `.env` and fill in the API key, client code, PIN and TOTP secret.
3. Adjust `config.yaml` (expiries, strikes each side of ATM, holidays, ...).

## Running

```bash
python run_agent.py
```
Runs today's session: logs in at 09:10, collects every minute until 15:40, does a final candle backfill, exits.

| Command | Purpose |
|---|---|
| `python run_agent.py --daemon` | keep running across days |
| `python run_agent.py --check` | test login, quotes and candles once (OK/FAIL) |
| `python run_agent.py --once` | one cycle right now (ignores market hours) |
| `python run_agent.py --once --mock` | offline test with generated data, no credentials |

### Automatic start (Ubuntu 24.04 server)

```bash
git clone <repo-url> MarketDataAgent && cd MarketDataAgent
bash deploy/setup_ubuntu.sh        # venv + systemd timer, creates .env from the template
nano .env                          # fill in Angel One credentials
.venv/bin/python run_agent.py --once   # quick live test
```
A systemd timer starts the agent at 09:05 IST Mon–Fri, even if the server clock is UTC. Data is written to `Data/` in the project folder.

| Command | Purpose |
|---|---|
| `systemctl list-timers marketdataagent.timer` | next scheduled run |
| `systemctl status marketdataagent` | is it running now |
| `journalctl -u marketdataagent -f` | live log (also in `logs/`) |
| `git pull && .venv/bin/pip install -r requirements.txt` | update to the latest code |

### Automatic start (Windows Task Scheduler)

```bash
powershell -ExecutionPolicy Bypass -File install_task.ps1
```
Registers the `MarketDataAgent` task at 09:05 Mon–Fri. The PC must be on (or able to wake) and logged in to the network.

## Analysis dashboard

A web dashboard (`analysis/app.py`, Streamlit + Plotly) for exploring one expiry at a time. Pick the index,
expiry, trading days, 1/5-min candles and strike range in the sidebar.

| Tab | What it shows |
|---|---|
| Overview | spot candles + futures + max pain, futures OI, put-call ratio, ATM straddle premium |
| PCR | put-call ratio (OI) with spot, PCR change and % change vs previous bar, change-in-OI PCR (PE vs CE OI added since open), PCR % change since open, volume PCR, table + CSV download |
| Open interest | CE / PE OI-change heatmaps (strike x time, spot overlaid), OI by strike at any moment |
| Build-up | long/short build-up, short covering, long unwinding per strike for any window, plus a timeline |
| Volatility & gamma | ATM IV, IV smile now vs earlier, gamma exposure by strike |
| Order flow | call-vs-put bid pressure and whether it leads spot moves (lead/lag correlation) |
| Contract | one strike: candles, volume, OI, IV, delta, bid/ask ratio, CSV download |
| Data explorer | every row in a sortable table: Excel-like column filters, or SQL (DuckDB) over `options` and `index_data`, with example queries and CSV download |
| Backtest | runs the 4 frozen option-buying setups on chosen days (or uploaded CSVs): trades, scoreboard, daily and cumulative net points; saves results to a forward-test journal |

Each tab has a "How to read this" note.

**Install on the server** (once, after `setup_ubuntu.sh`):
```bash
bash deploy/setup_ui.sh
```
It runs as the `marketdataui` service, listening only on the server itself (127.0.0.1:8501).
To open it, start an SSH tunnel from your PC and browse to http://localhost:8501:
```bash
ssh -L 8501:localhost:8501 root@<server-ip>
```

**Open it from the internet, with a login** (one username + two passwords, both required):
```bash
bash deploy/setup_ui.sh --public
```
It asks for the username and the two passwords (only salted hashes are saved, in `.env`), creates a
self-signed HTTPS certificate, listens on all addresses and opens port 8501 in ufw if ufw is on.
Browse to `https://<server-ip>:8501` (the browser warns once about the certificate: Advanced → Proceed).
If your VPS provider has its own firewall, allow TCP 8501 there too.

* Change the login: `.venv/bin/python analysis/set_dashboard_password.py && sudo systemctl restart marketdataui`
* The login page and browser tab only show "Library Management" (`PUBLIC_NAME` in `analysis/auth.py`); the real
  dashboard appears after login.
* 5 wrong attempts from one IP lock that IP out for 15 minutes.
* Reloading the page logs you out (each browser tab is its own session).
* In public mode the dashboard refuses to start without a login configured.
* Back to tunnel-only: `bash deploy/setup_ui.sh`

### PCR files

`analysis/export_pcr.py` writes the put-call ratio for every expiry to CSV, one row per bar:
```
Data/NIFTY/PCR/2026-11-03/NIFTY_PCR_2026-10-28_1min.csv    <- expiry folder, one file per day
Data/NIFTY/PCR/2026-11-03/NIFTY_PCR_2026-10-28_5min.csv
```
| Column | Meaning |
|---|---|
| `pcr` | put OI ÷ call OI (whole open interest, all tracked strikes) |
| `pcr_prev` | actual PCR at the previous candle |
| `pcr_chg`, `pcr_chg_pct` | PCR change vs `pcr_prev`, absolute and % |
| `ce_oi_chg`, `pe_oi_chg` | OI added since the day's first bar (each strike from when it started being tracked) |
| `chg_oi_pcr` | change-in-OI PCR = `pe_oi_chg` ÷ `ce_oi_chg` |
| `pcr_chg_open`, `pcr_chg_open_pct` | PCR change since the day's first bar, absolute and % |
| `spot`, `ce_oi`, `pe_oi`, `ce_volume`, `pe_volume`, `pcr_volume`, `contracts` | context; a jump in `contracts` means a new strike joined |

It only reads the collected CSVs, so it never affects the collector. Install the timer that runs it every
minute in market hours (and once at 16:05 after the backfill), and build files for data collected so far:
```bash
bash deploy/setup_pcr.sh
```
Run it by hand any time: `.venv/bin/python analysis/export_pcr.py` (add `--all` to rebuild every expiry).

Run it locally against any copy of the data: `MDA_DATA_DIR=/path/to/Data streamlit run analysis/app.py`.

### Backtest of the 4 setups

`analysis/setups_backtest.py` holds the rules (in its docstring) and runs them for the dashboard's Backtest tab.
A trade is bought on the candle after the signal candle, at that candle's high (worst fill).
Each run can save its days to `Data/BACKTEST/setups_journal.csv`; re-running a day replaces it. 28–30 Oct 2026
are the days the rules were found on, so the journal view hides them by default. The same can be run by hand on
combined option CSVs (Data explorer export format):
```bash
.venv/bin/python analysis/setups_backtest.py Full_data.csv [more.csv ...]   # --no-save, --journal PATH
```

## Notes / limits

* Volume is in units (contracts x lot size), like OI and bid/ask quantities. Angel One's REST candles report F&O volume in lots; the agent converts them.
* Live candles are built from ticks: a minute with no trades gets a flat candle at the previous close with volume 0. The minute the agent starts in is skipped (only partly seen), and the first candle's volume is blank until the end-of-day backfill.
* `collection.mode: poll` is the old REST-only mode; on this account it only keeps up with ~10–15 candle calls per minute.
* Holidays: weekends are skipped. Exchange holidays are skipped if listed in `config.yaml`, or detected automatically when no index candles appear by 09:25.
* Contracts are picked around the ATM at start-up. With `track_new_atm_strikes: true`, new strikes are added when the index moves.
* If a CSV is open in Excel, it cannot be written to; the agent retries on the next cycle.
* Logs: `logs/agent_YYYY-MM-DD.log`.
