"""Per-minute collection cycle: candles, quotes (OI / bids / depth) and greeks."""
import logging
import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import pandas as pd

from .config import IST
from .greeks import greeks as bs_greeks
from .instruments import ScripMaster, select_expiries, strikes_around

log = logging.getLogger(__name__)
CASH_CLOSE = time(15, 30)   # NSE/BSE F&O close; no snapshots for minutes at/after this

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
        self.today = None

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
            self.feeds.append(spot)
            self.spot_feeds[name] = spot
            try:
                fut = master.nearest_future(name, spec["fno_exchange"], self.today)
                self.feeds.append(Feed(fut.exchange, fut.token, series, prefix="fut_",
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
                self.feeds.append(Feed(fut.exchange, fut.token, series, kind="extra", contract=fut))
                log.info("%s contract: %s", name, fut.symbol)
            else:
                self.feeds.append(Feed(spec["exchange"], spec["token"], series,
                                       with_volume=False, kind="extra"))

        spots = self._spot_prices()
        for name in self.chains:
            self._add_option_strikes(name, spots.get(name))
        log.info("Tracking %d feeds (%d option contracts)",
                 len(self.feeds), sum(f.kind == "option" for f in self.feeds))

    def _spot_prices(self):
        by_exch = {}
        for f in self.spot_feeds.values():
            by_exch.setdefault(f.exchange, []).append(f.token)
        quotes = {str(q.get("symbolToken")): q for q in self.client.quotes(by_exch)}
        return {name: quotes.get(str(f.token), {}).get("ltp") for name, f in self.spot_feeds.items()}

    def _add_option_strikes(self, name, spot):
        if not spot:
            log.warning("No spot price for %s - option contracts not selected", name)
            return
        ocfg = self.cfg.options[name]
        chain = self.chains[name]
        types = ocfg.get("option_types", ["CE", "PE"])
        for expiry in select_expiries(chain, ocfg.get("expiries", 1)):
            contracts = chain[expiry]
            for strike in strikes_around(contracts, spot, int(ocfg.get("strikes_each_side", 10))):
                for t in types:
                    c = contracts.get((strike, t))
                    if c is None or c.token in self.option_keys:
                        continue
                    base = self.cfg.data_dir / name / expiry.isoformat() / c.symbol
                    self.feeds.append(Feed(c.exchange, c.token, self.store.get(base, OPTION_COLS),
                                           kind="option", contract=c, underlying=name))
                    self.option_keys.add(c.token)
                    log.info("Tracking %s", c.symbol)

    # ------------------------------------------------------------ cycle
    def cycle(self, now: datetime, full: bool = False):
        now = now.replace(tzinfo=None, microsecond=0)
        snap_ts = pd.Timestamp(now).floor("min") - pd.Timedelta(minutes=1)
        if not full and snap_ts.time() < CASH_CLOSE:
            self._snapshot(now, snap_ts)
        self._candles(now, full)
        self.store.flush_all()

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
            if not q or f.kind == "index":
                continue
            if f.kind == "future":
                f.series.update(snap_ts, {
                    "fut_symbol": f.contract.symbol, "fut_ltp": q.get("ltp"),
                    "fut_oi": q.get("opnInterest"),
                    "fut_total_buy_qty": q.get("totBuyQuan"), "fut_total_sell_qty": q.get("totSellQuan")})
            elif f.kind == "extra":
                f.series.update(snap_ts, {
                    "symbol": f.contract.symbol if f.contract else q.get("tradingSymbol"),
                    "ltp": q.get("ltp"), "oi": q.get("opnInterest") or None})
            else:
                c = f.contract
                row = {"ltp": q.get("ltp"), "oi": q.get("opnInterest"),
                       "total_buy_qty": q.get("totBuyQuan"), "total_sell_qty": q.get("totSellQuan"),
                       "underlying_spot": spots.get(f.underlying)}
                row.update(_depth_stats(q.get("depth")))
                row.update(self._greeks_for(c, q.get("ltp"), spots.get(f.underlying), now, api_greeks))
                f.series.update(snap_ts, row)

        if self.cfg.track_new_atm:
            for name in self.chains:
                if spots.get(name):
                    self._add_option_strikes(name, spots[name])

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

        for f in selected:
            last = f.series.last_candle_ts(f.prefix + "close")
            start = day_start if full or last is None else max(last.to_pydatetime(), day_start)
            try:
                rows = self.client.candles(f.exchange, f.token, "ONE_MINUTE", start, now)
            except Exception as e:
                log.warning("Candles failed for %s/%s: %s", f.exchange, f.token, e)
                continue
            f.last_fetch = _time.monotonic()
            for r in rows:
                ts = _ts(r[0])
                if ts + pd.Timedelta(minutes=1) > pd.Timestamp(now) or ts < pd.Timestamp(day_start):
                    continue   # skip the still-forming candle / pre-open
                vals = {f.prefix + "open": r[1], f.prefix + "high": r[2],
                        f.prefix + "low": r[3], f.prefix + "close": r[4]}
                if f.with_volume:
                    vals[f.prefix + "volume"] = r[5]
                if f.kind == "future":
                    vals["fut_symbol"] = f.contract.symbol
                elif f.kind == "extra" and f.contract:
                    vals["symbol"] = f.contract.symbol
                f.series.update(ts, vals)

    def has_index_data_today(self) -> bool:
        day = pd.Timestamp(self.today)
        return any(any(ts >= day and "close" in row for ts, row in f.series.rows.items())
                   for f in self.spot_feeds.values())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
