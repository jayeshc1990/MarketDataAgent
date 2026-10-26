"""Per-minute collection cycle: candles, OI / bids / depth and greeks.

stream mode (default): candles are built live from Angel One's WebSocket ticks; the REST candle
endpoint is only used for the end-of-day backfill. poll mode: candles and quotes via REST.
"""
import logging
import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import pandas as pd

from .angel_client import RateLimited
from .config import IST
from .greeks import greeks as bs_greeks
from .instruments import ScripMaster, select_expiries, strikes_around
from .live_candles import CandleBuilder
from .stream import MODE_QUOTE, MODE_SNAPQUOTE

log = logging.getLogger(__name__)
CASH_CLOSE = time(15, 30)   # NSE/BSE F&O close; no snapshots for minutes at/after this
FULL_BACKFILL_MAX_WAIT = 20 * 60   # end-of-day backfill waits out rate limits up to this long
BACKFILL_SAVE_EVERY = 25           # feeds between saves during a full backfill
SPOT_WAIT = 30                     # seconds to wait for a streamed spot price at start-up

INDEX_COLS = ["timestamp", "open", "high", "low", "close", "spot_ltp",
              "fut_symbol", "fut_open", "fut_high", "fut_low", "fut_close", "fut_volume",
              "fut_oi", "fut_ltp", "fut_total_buy_qty", "fut_total_sell_qty"]
EXTRA_COLS = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "oi", "ltp"]
OPTION_COLS = ["timestamp", "open", "high", "low", "close", "volume", "oi", "ltp",
               "iv", "delta", "gamma", "theta", "vega", "greeks_source",
               "total_buy_qty", "total_sell_qty",
               "bid_orders_top5", "bid_qty_top5", "ask_orders_top5", "ask_qty_top5",
               "best_bid", "best_ask", "underlying_spot"]


@dataclass(eq=False)
class Feed:
    """One instrument whose candles + snapshot go into a Series (optionally with a column prefix)."""
    exchange: str
    token: str
    series: object
    prefix: str = ""
    with_volume: bool = True
    kind: str = "index"           # index | future | extra | option
    contract: object = None
    underlying: str = ""
    last_fetch: float = field(default=0.0)

    @property
    def label(self) -> str:
        sym = self.contract.symbol if self.contract else self.underlying or self.token
        return f"{sym} ({self.exchange}/{self.token})"


def _depth_stats(depth):
    buy = (depth or {}).get("buy") or []
    sell = (depth or {}).get("sell") or []
    return {
        "bid_orders_top5": sum(int(d.get("orders") or 0) for d in buy),
        "bid_qty_top5": sum(int(d.get("quantity") or 0) for d in buy),
        "ask_orders_top5": sum(int(d.get("orders") or 0) for d in sell),
        "ask_qty_top5": sum(int(d.get("quantity") or 0) for d in sell),
        "best_bid": buy[0].get("price") if buy else None,
        "best_ask": sell[0].get("price") if sell else None,
    }


def _tick_as_quote(t):
    """Stream tick -> the REST quote field names the row builders use."""
    return {"ltp": t.get("ltp"), "opnInterest": t.get("oi"), "totBuyQuan": t.get("total_buy_qty"),
            "totSellQuan": t.get("total_sell_qty"), "depth": t.get("depth")}


def _ts(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(IST).tz_localize(None)
    return ts


class Collector:
    def __init__(self, cfg, client, store, scrip_master_loader=None):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.load_master = scrip_master_loader or (
            lambda today: ScripMaster.load(cfg.broker["scrip_master_url"], cfg.cache_dir, today))
        self.feeds = []
        self.spot_feeds = {}           # index name -> Feed
        self.option_keys = set()       # tokens already tracked
        self.chains = {}               # index name -> {expiry: {(strike, type): Contract}}
        self.by_key = {}               # (exchange, token) -> Feed
        self.today = None
        self.builder = CandleBuilder()
        self.stream = None
        self.last_spot = {}            # index name -> latest spot price
        self.last_ltp = {}             # (exchange, token) -> latest streamed price

    # ------------------------------------------------------------ setup
    def setup(self, now: datetime):
        self.today = now.date()
        self.client.login()
        master = self.load_master(self.today)
        d = self.today.isoformat()
        cfg = self.cfg

        for name, spec in cfg.indices.items():
            series = self.store.get(cfg.data_dir / name / "INDEX" / f"{name}_INDEX_{d}", INDEX_COLS)
            spot = Feed(spec["spot"]["exchange"], spec["spot"]["token"], series,
                        with_volume=False, kind="index", underlying=name)
            self._add_feed(spot)
            self.spot_feeds[name] = spot
            try:
                fut = master.nearest_future(name, spec["fno_exchange"], self.today)
                self._add_feed(Feed(fut.exchange, fut.token, series, prefix="fut_",
                                       kind="future", contract=fut, underlying=name))
                log.info("%s future: %s", name, fut.symbol)
            except LookupError as e:
                log.warning("%s", e)
            if name in cfg.options:
                self.chains[name] = master.option_chain(name, spec["fno_exchange"], self.today)

        for name, spec in cfg.extras.items():
            series = self.store.get(cfg.data_dir / name / f"{name}_{d}", EXTRA_COLS)
            if spec.get("kind") == "future":
                try:
                    fut = master.nearest_future(spec["name"], spec["exchange"], self.today,
                                                spec.get("instrument_type", "FUTCOM"))
                except LookupError as e:
                    log.warning("%s", e)
                    continue
                self._add_feed(Feed(fut.exchange, fut.token, series, kind="extra", contract=fut))
                log.info("%s contract: %s", name, fut.symbol)
            else:
                self._add_feed(Feed(spec["exchange"], spec["token"], series,
                                       with_volume=False, kind="extra"))

        if cfg.collect_mode == "stream":
            # start streaming index/futures first; option strikes are added once the spot is known
            self.stream = self.client.stream(self._on_tick)
            for f in self.feeds:
                self._subscribe(f)
            # ticks only cover part of the minute the stream starts in, so that minute is skipped
            self.stream_start_minute = datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)
            self.stream.start()

        spots = self._spot_prices()
        self.last_spot.update({k: v for k, v in spots.items() if v})
        for name in self.chains:
            self._add_option_strikes(name, spots.get(name))
        log.info("Tracking %d feeds (%d option contracts)",
                 len(self.feeds), sum(f.kind == "option" for f in self.feeds))

    def _add_feed(self, feed):
        self.feeds.append(feed)
        self.by_key[(feed.exchange, str(feed.token))] = feed
        if self.stream:
            self._subscribe(feed)

    def _subscribe(self, f):
        # spot indices / VIX have no OI or depth, so the lighter Quote mode is enough
        light = f.kind == "index" or (f.kind == "extra" and not f.contract)
        self.stream.subscribe(f.exchange, [f.token], MODE_QUOTE if light else MODE_SNAPQUOTE)

    def _on_tick(self, tick):
        self.last_ltp[(tick["exchange"], tick["token"])] = tick["ltp"]
        self.builder.on_tick(tick, datetime.now(IST).replace(tzinfo=None))

    def _spot_prices(self):
        """Index spot prices for picking ATM strikes: REST quote, else the live stream."""
        by_exch = {}
        for f in self.spot_feeds.values():
            by_exch.setdefault(f.exchange, []).append(f.token)
        try:
            quotes = {str(q.get("symbolToken")): q for q in self.client.quotes(by_exch)}
            spots = {name: quotes.get(str(f.token), {}).get("ltp") for name, f in self.spot_feeds.items()}
            if all(spots.values()):
                return spots
        except Exception as e:
            log.warning("Spot quote failed (%s)%s", e, " - using the live stream" if self.stream else "")
            spots = {}
        if not self.stream:
            return spots
        deadline = _time.monotonic() + SPOT_WAIT
        while _time.monotonic() < deadline:
            spots = {name: spots.get(name) or self.last_ltp.get((f.exchange, str(f.token)))
                     for name, f in self.spot_feeds.items()}
            if all(spots.values()):
                break
            _time.sleep(0.5)
        return spots

    def _add_option_strikes(self, name, spot):
        if not spot:
            log.warning("No spot price for %s - option contracts not selected", name)
            return
        ocfg = self.cfg.options[name]
        chain = self.chains[name]
        types = ocfg.get("option_types", ["CE", "PE"])
        for expiry in select_expiries(chain, ocfg.get("expiries", 1)):
            contracts = chain[expiry]
            for strike in strikes_around(contracts, spot, int(ocfg.get("strikes_each_side", 10)),
                                         ocfg.get("strike_multiple")):
                for t in types:
                    c = contracts.get((strike, t))
                    if c is None or c.token in self.option_keys:
                        continue
                    base = self.cfg.data_dir / name / expiry.isoformat() / c.symbol
                    self._add_feed(Feed(c.exchange, c.token, self.store.get(base, OPTION_COLS),
                                           kind="option", contract=c, underlying=name))
                    self.option_keys.add(c.token)
                    log.info("Tracking %s", c.symbol)

    # ------------------------------------------------------------ cycle
    def cycle(self, now: datetime, full: bool = False):
        now = now.replace(tzinfo=None, microsecond=0)
        if full:
            self._candles(now, full=True)
        elif self.stream:
            self._stream_cycle(now)
        else:
            snap_ts = pd.Timestamp(now).floor("min") - pd.Timedelta(minutes=1)
            if snap_ts.time() < CASH_CLOSE:
                self._snapshot(now, snap_ts)
            self._candles(now, full=False)
        self.store.flush_all()

    def stop(self):
        if self.stream:
            self.stream.stop()

    def _stream_cycle(self, now):
        upto = now.replace(second=0)
        bars_by_key = self.builder.collect(upto)
        if not bars_by_key:
            state = "connected" if self.stream.connected.is_set() else "NOT connected"
            log.warning("No live ticks for the last minute (stream %s, %d ticks so far)",
                        state, self.stream.ticks)
            return
        day_start = datetime.combine(now.date(), self.cfg.market_start)
        latest = upto - timedelta(minutes=1)

        spot_at = {}                     # (index name, minute) -> spot close
        for name, f in self.spot_feeds.items():
            for b in bars_by_key.get((f.exchange, str(f.token)), []):
                spot_at[(name, b["minute"])] = b["close"]
                self.last_spot[name] = b["close"]
        needs_greeks = any(self.by_key[k].kind == "option" for k in bars_by_key if k in self.by_key)
        api_greeks = self._api_greeks(now) if needs_greeks and latest.time() < CASH_CLOSE else {}

        rows = 0
        for key, bars in bars_by_key.items():
            f = self.by_key.get(key)
            if f is None:
                continue
            for b in bars:
                m = b["minute"]
                if m < day_start or m <= self.stream_start_minute                         or (m.time() >= CASH_CLOSE and f.exchange != "MCX"):
                    continue             # pre-open / partial first minute / after the F&O close
                f.series.update(m, self._candle_vals(f, b["open"], b["high"], b["low"],
                                                     b["close"], b["volume"]))
                q = dict(_tick_as_quote(b["last"]), ltp=b["close"])
                spot = spot_at.get((f.underlying, m), self.last_spot.get(f.underlying))
                self._write_snapshot(f, m, q, spot, now, api_greeks if m == latest else {})
                rows += 1
        log.info("Live candles: %d rows for %d instruments up to %s (%d ticks so far)",
                 rows, len(bars_by_key), latest.strftime("%H:%M"), self.stream.ticks)

        if self.cfg.track_new_atm:
            for name in self.chains:
                if self.last_spot.get(name):
                    self._add_option_strikes(name, self.last_spot[name])

    def _snapshot(self, now, snap_ts):
        by_exch = {}
        for f in self.feeds:
            by_exch.setdefault(f.exchange, []).append(f.token)
        try:
            quotes = {(q.get("exchange"), str(q.get("symbolToken"))): q
                      for q in self.client.quotes(by_exch)}
        except Exception as e:
            log.error("Quote snapshot failed: %s", e)
            return

        spots = {}
        for name, f in self.spot_feeds.items():
            q = quotes.get((f.exchange, str(f.token)))
            if q:
                spots[name] = q.get("ltp")
                f.series.update(snap_ts, {"spot_ltp": q.get("ltp")})

        api_greeks = self._api_greeks(now)

        for f in self.feeds:
            q = quotes.get((f.exchange, str(f.token)))
            if q and f.kind != "index":
                self._write_snapshot(f, snap_ts, q, spots.get(f.underlying), now, api_greeks)

        if self.cfg.track_new_atm:
            for name in self.chains:
                if spots.get(name):
                    self._add_option_strikes(name, spots[name])

    def _write_snapshot(self, f, ts, q, spot, now, api_greeks):
        """OI / bids / depth / greeks columns from a REST quote (or a tick shaped like one)."""
        if f.kind == "index":
            f.series.update(ts, {"spot_ltp": q.get("ltp")})
        elif f.kind == "future":
            f.series.update(ts, {
                "fut_symbol": f.contract.symbol, "fut_ltp": q.get("ltp"),
                "fut_oi": q.get("opnInterest"),
                "fut_total_buy_qty": q.get("totBuyQuan"), "fut_total_sell_qty": q.get("totSellQuan")})
        elif f.kind == "extra":
            f.series.update(ts, {
                "symbol": f.contract.symbol if f.contract else q.get("tradingSymbol"),
                "ltp": q.get("ltp"), "oi": q.get("opnInterest") or None})
        else:
            row = {"ltp": q.get("ltp"), "oi": q.get("opnInterest"),
                   "total_buy_qty": q.get("totBuyQuan"), "total_sell_qty": q.get("totSellQuan"),
                   "underlying_spot": spot}
            row.update(_depth_stats(q.get("depth")))
            row.update(self._greeks_for(f.contract, q.get("ltp"), spot, now, api_greeks))
            f.series.update(ts, row)

    def _candle_vals(self, f, o, h, l, c, v):
        vals = {f.prefix + "open": o, f.prefix + "high": h, f.prefix + "low": l, f.prefix + "close": c}
        if f.with_volume and v is not None:
            vals[f.prefix + "volume"] = v
        if f.kind == "future":
            vals["fut_symbol"] = f.contract.symbol
        elif f.kind == "extra" and f.contract:
            vals["symbol"] = f.contract.symbol
        return vals

    def _api_greeks(self, now):
        out = {}
        if self.cfg.greeks_source == "local":
            return out
        pairs = {(f.underlying, f.contract.expiry_api) for f in self.feeds if f.kind == "option"}
        for name, expiry in sorted(pairs):
            try:
                for g in self.client.option_greeks(name, expiry):
                    key = (name, expiry, float(g.get("strikePrice") or 0), g.get("optionType"))
                    out[key] = {
                        "iv": _num(g.get("impliedVolatility")), "delta": _num(g.get("delta")),
                        "gamma": _num(g.get("gamma")), "theta": _num(g.get("theta")),
                        "vega": _num(g.get("vega")), "greeks_source": "api"}
            except Exception as e:
                log.debug("Greeks API failed for %s %s: %s", name, expiry, e)
        return out

    def _greeks_for(self, c, ltp, spot, now, api_greeks):
        g = api_greeks.get((c.name, c.expiry_api, c.strike, c.option_type))
        if g:
            return g
        if self.cfg.greeks_source == "api" or not ltp or not spot:
            return {}
        expiry_close = datetime.combine(c.expiry, datetime.min.time()) + timedelta(hours=15, minutes=30)
        T = max((expiry_close - now).total_seconds(), 60) / (365 * 24 * 3600)
        g = bs_greeks(float(ltp), float(spot), c.strike, T,
                      self.cfg.risk_free_rate, self.cfg.dividend_yield, c.option_type)
        if not g:
            return {}
        g["greeks_source"] = "local"
        return g

    def _candles(self, now, full):
        day_start = datetime.combine(now.date(), self.cfg.market_start)
        options = sorted((f for f in self.feeds if f.kind == "option"), key=lambda f: f.last_fetch)
        if full:
            selected = [f for f in self.feeds if f.kind != "option"] + options
        else:
            n = math.ceil(len(options) / self.cfg.option_refresh_min)
            selected = [f for f in self.feeds if f.kind != "option"] + options[:n]

        backfill_deadline = _time.monotonic() + FULL_BACKFILL_MAX_WAIT
        pending = list(selected)
        total, done, empty, failed = len(selected), 0, 0, 0
        if full:
            log.info("Backfill: downloading today's candles for %d feeds ...", total)
        while pending:
            f = pending[0]
            last = f.series.last_candle_ts(f.prefix + "close")
            start = day_start if full or last is None else max(last.to_pydatetime(), day_start)
            try:
                rows = self.client.candles(f.exchange, f.token, "ONE_MINUTE", start, now)
            except RateLimited as e:
                # Skipped feeds keep their old last_fetch, so they go first next cycle
                if not full or e.until > backfill_deadline:
                    log.warning("Candle downloads paused - %d feeds left for a later cycle", len(pending))
                    break
                log.info("Backfill waiting %.0fs for Angel One rate limit", e.until - _time.monotonic())
                _time.sleep(max(0.0, e.until - _time.monotonic()) + 1)
                continue
            except Exception as e:
                log.warning("Candles failed for %s: %s", f.label, e)
                pending.pop(0)
                failed += 1
                continue
            pending.pop(0)
            done += 1
            empty += not rows
            f.last_fetch = _time.monotonic()
            if full and done % BACKFILL_SAVE_EVERY == 0:
                self.store.flush_all()   # save progress, so an interrupted backfill keeps what it got
                log.info("Backfill: %d/%d feeds done", done, total)
            for r in rows:
                ts = _ts(r[0])
                if ts + pd.Timedelta(minutes=1) > pd.Timestamp(now) or ts < pd.Timestamp(day_start):
                    continue   # skip the still-forming candle / pre-open
                f.series.update(ts, self._candle_vals(f, r[1], r[2], r[3], r[4], r[5]))

        log.info("Candles: %d/%d feeds downloaded (%d returned no candles), %d failed, %d left for later",
                 done, total, empty, failed, len(pending))

    def has_index_data_today(self) -> bool:
        day = pd.Timestamp(self.today)
        return any(any(ts >= day and "close" in row for ts, row in f.series.rows.items())
                   for f in self.spot_feeds.values())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
