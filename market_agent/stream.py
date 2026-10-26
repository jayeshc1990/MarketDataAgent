"""Angel One SmartStream (WebSocket v2): live ticks for many tokens on one connection."""
import json
import logging
import struct
import threading
import time as _time

import websocket

log = logging.getLogger(__name__)

WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
EXCHANGE_TYPES = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
EXCHANGE_NAMES = {v: k for k, v in EXCHANGE_TYPES.items()}
MODE_QUOTE = 2        # LTP, day volume, total buy/sell qty, day OHLC
MODE_SNAPQUOTE = 3    # + OI and best-5 depth
PING_EVERY = 25       # seconds; server drops idle connections after ~30s
MAX_BACKOFF = 60


def parse_tick(data: bytes):
    """Decode one binary SmartStream packet (little-endian). Prices arrive in paise."""
    if len(data) < 51:
        return None
    mode, exch_type = data[0], data[1]
    token = data[2:27].split(b"\x00", 1)[0].decode()
    _seq, exch_ts, ltp = struct.unpack_from("<qqq", data, 27)
    tick = {"mode": mode, "exchange": EXCHANGE_NAMES.get(exch_type, str(exch_type)),
            "token": token, "exchange_ts": exch_ts, "ltp": ltp / 100}
    if mode >= MODE_QUOTE and len(data) >= 123:
        _ltq, _atp, volume = struct.unpack_from("<qqq", data, 51)
        tbq, tsq = struct.unpack_from("<dd", data, 75)
        tick.update(volume=volume, total_buy_qty=int(tbq), total_sell_qty=int(tsq))
    if mode >= MODE_SNAPQUOTE and len(data) >= 347:
        _last_trade_ts, oi = struct.unpack_from("<qq", data, 123)
        buy, sell = [], []
        for i in range(10):
            flag, qty, price, orders = struct.unpack_from("<hqqh", data, 147 + 20 * i)
            level = {"price": price / 100, "quantity": qty, "orders": orders}
            (buy if flag == 1 else sell).append(level)
        tick.update(oi=oi, depth={"buy": buy, "sell": sell})
    return tick


class AngelStream:
    """Keeps one WebSocket open (reconnecting as needed) and calls on_tick(tick) per packet."""

    def __init__(self, auth: callable, on_tick: callable):
        self.auth = auth            # -> dict(jwt, api_key, client_code, feed_token); re-read on reconnect
        self.on_tick = on_tick
        self.subs = {}              # (exchange, mode) -> set(tokens)
        self.ws = None
        self.connected = threading.Event()
        self.stopping = False
        self.lock = threading.Lock()
        self.ticks = 0

    def subscribe(self, exchange: str, tokens, mode=MODE_SNAPQUOTE):
        with self.lock:
            new = set(map(str, tokens)) - self.subs.setdefault((exchange, mode), set())
            self.subs[(exchange, mode)] |= new
        if new and self.connected.is_set():
            self._send_subscribe({(exchange, mode): new})

    def start(self):
        threading.Thread(target=self._run, name="angel-stream", daemon=True).start()

    def stop(self):
        self.stopping = True
        if self.ws:
            self.ws.close()

    # ------------------------------------------------------------ internals
    def _send_subscribe(self, subs):
        by_mode = {}
        for (exch, mode), toks in subs.items():
            by_mode.setdefault(mode, []).append({"exchangeType": EXCHANGE_TYPES[exch], "tokens": sorted(toks)})
        for mode, token_list in by_mode.items():
            self.ws.send(json.dumps({"correlationID": "mda", "action": 1,
                                     "params": {"mode": mode, "tokenList": token_list}}))
        log.info("Stream subscribed %d tokens", sum(len(t) for t in subs.values()))

    def _run(self):
        backoff = 1
        while not self.stopping:
            a = self.auth()
            headers = [f"Authorization: {a['jwt']}", f"x-api-key: {a['api_key']}",
                       f"x-client-code: {a['client_code']}", f"x-feed-token: {a['feed_token']}"]
            self.ws = websocket.WebSocketApp(WS_URL, header=headers, on_open=self._on_open,
                                             on_message=self._on_message, on_error=self._on_error,
                                             on_close=self._on_close)
            started = _time.monotonic()
            self.ws.run_forever()
            self.connected.clear()
            if self.stopping:
                break
            if _time.monotonic() - started > 120:
                backoff = 1          # it had been up for a while; reconnect quickly
            log.warning("Stream disconnected - reconnecting in %ds", backoff)
            _time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _on_open(self, ws):
        log.info("Stream connected")
        self.connected.set()
        with self.lock:
            subs = {k: set(v) for k, v in self.subs.items() if v}
        if subs:
            self._send_subscribe(subs)
        threading.Thread(target=self._ping, args=(ws,), daemon=True).start()

    def _ping(self, ws):
        while self.connected.is_set() and ws is self.ws:
            _time.sleep(PING_EVERY)
            try:
                ws.send("ping")
            except Exception:
                return

    def _on_message(self, ws, message):
        if isinstance(message, str):
            if message != "pong":
                log.warning("Stream message: %s", message[:200])
            return
        try:
            tick = parse_tick(message)
        except Exception as e:
            log.debug("Bad packet (%s): %r", e, message[:60])
            return
        if tick:
            self.ticks += 1
            self.on_tick(tick)

    def _on_error(self, ws, error):
        log.warning("Stream error: %s", error)

    def _on_close(self, ws, code, msg):
        self.connected.clear()
        if not self.stopping:
            log.info("Stream closed (%s %s)", code, msg)
