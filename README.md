# MarketDataAgent

Collects Indian F&O market data from **Angel One SmartAPI** every minute, Mon–Fri, 09:15–15:40 IST.

## What is collected

| Instrument | Data |
|---|---|
| NIFTY / BANKNIFTY / SENSEX spot | 1-min & 5-min OHLC, LTP |
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

## Notes / limits

* Angel One allows ~5000 candle requests/hour, so option candles are refreshed round-robin (every contract at least every `option_candle_refresh_minutes`, default 5). All candles are backfilled at 15:40, so the files are complete at the end of the day. Index, futures, VIX and crude candles are refreshed every minute.
* Holidays: weekends are skipped. Exchange holidays are skipped if listed in `config.yaml`, or detected automatically when no index candles appear by 09:25.
* Contracts are picked around the ATM at start-up. With `track_new_atm_strikes: true`, new strikes are added when the index moves.
* If a CSV is open in Excel, it cannot be written to; the agent retries on the next cycle.
* Logs: `logs/agent_YYYY-MM-DD.log`.
