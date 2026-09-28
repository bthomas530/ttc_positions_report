# Persistent IBKR connection manager.
#
# Owns a single IB() instance on a dedicated background thread with its own
# asyncio event loop. Replaces the old per-request connect/disconnect pattern
# that caused intermittent handshake timeouts (a fresh clientId=1 connection
# every 60s refresh). The connection stays up between refreshes, market-data
# subscriptions stay active so prices are instantly available, and a watchdog
# reconnects with backoff when TWS goes away.

import asyncio
import logging
import math
import random
import select
import socket
import threading
import time

from datetime import datetime, timedelta

import pytz

from ib_async import IB, Contract, LimitOrder, Stock

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = [
    ("127.0.0.1", 7496, "TWS Live"),
    ("127.0.0.1", 7497, "TWS Paper"),
    ("127.0.0.1", 4001, "Gateway Live"),
    ("127.0.0.1", 4002, "Gateway Paper"),
]

CONNECT_TIMEOUT = 4       # seconds for ib_async handshake
PROBE_TIMEOUT = 1.0       # seconds for socket pre-check
HEARTBEAT_INTERVAL = 30   # seconds between reqCurrentTime keepalives
HEARTBEAT_TIMEOUT = 5
BACKOFF_BASE = 2          # reconnect backoff: 2s, 4s, 8s ... capped
BACKOFF_CAP = 60
CLIENT_ID_RETRIES = 3     # fresh random ids to try on 'client id in use'
FIRST_PRICE_DEADLINE = 5  # seconds to wait for a new ticker's first price
SNAPSHOT_MAX_AGE = 5      # seconds a snapshot stays fresh for coalescing
# Generic tick 456 = IB Dividends (next ex-date + amount) -- rides along on
# the stock subscriptions we already hold, no extra request or data feed.
STOCK_GENERIC_TICKS = '456'
ORDERS_FILLS_TIMEOUT = 5  # seconds; orders/fills are extras, never block prices
STAGED_ORDER_REF = 'TTC staged close'
# Order statuses that mean "still working" (anything else is done/dead).
ACTIVE_ORDER_STATUSES = {'ApiPending', 'PendingSubmit', 'PreSubmitted',
                         'Submitted', 'PendingCancel', 'Inactive'}
UNSET_DOUBLE = 1.7976931348623157e+308  # IBKR's "no value" for doubles


def order_to_dict(trade):
    """Flatten an ib_async Trade (open order) for the API."""
    c, o, st = trade.contract, trade.order, trade.orderStatus
    lmt = safe_price(o.lmtPrice)
    return {
        'order_id': o.orderId,
        'perm_id': o.permId,
        'con_id': c.conId,
        'symbol': c.symbol,
        'sec_type': c.secType,
        'right': getattr(c, 'right', '') or '',
        'strike': safe_price(getattr(c, 'strike', 0)) or None,
        'expiry': _iso_expiry(getattr(c, 'lastTradeDateOrContractMonth', '')),
        'action': o.action,
        'quantity': safe_price(o.totalQuantity),
        'filled': safe_price(st.filled),
        'order_type': o.orderType,
        'limit_price': lmt if 0 < lmt < UNSET_DOUBLE else None,
        'tif': o.tif,
        'status': st.status,
        # False = sitting in TWS waiting for Dad to hit Transmit
        'transmitted': bool(o.transmit) and st.status != 'Inactive',
        'staged_by_app': o.orderRef == STAGED_ORDER_REF,
    }


def fill_to_dict(fill):
    c, e, cr = fill.contract, fill.execution, fill.commissionReport
    realized = safe_price(getattr(cr, 'realizedPNL', None)) if cr else 0
    commission = safe_price(getattr(cr, 'commission', None)) if cr else 0
    return {
        'exec_id': e.execId,
        'time': e.time.isoformat() if e.time else None,
        'symbol': c.symbol,
        'sec_type': c.secType,
        'right': getattr(c, 'right', '') or '',
        'strike': safe_price(getattr(c, 'strike', 0)) or None,
        'expiry': _iso_expiry(getattr(c, 'lastTradeDateOrContractMonth', '')),
        'side': 'BUY' if e.side == 'BOT' else 'SELL',
        'quantity': safe_price(e.shares),
        'price': safe_price(e.price),
        # The commission report trails the execution by a moment; until it
        # lands the field is 0, which would read as "free trade".
        'commission': commission if 0 < commission < UNSET_DOUBLE else None,
        'realized_pl': realized if 0 < abs(realized) < UNSET_DOUBLE else None,
        'order_ref': e.orderRef or '',
    }


def _iso_expiry(raw):
    if raw and len(raw) >= 8:
        return f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}'
    return None


def parse_liquid_hours(liquid_hours, tz_name, now):
    """Is the regular session open at `now`, per IBKR's own calendar?

    liquidHours looks like '20260928:0930-20260928:1600;20260929:CLOSED'
    in the contract's exchange time zone. Unlike a 9:30-4:00 weekday clock
    this knows about holidays and half days, so a quiet market on Good
    Friday isn't mistaken for a frozen data feed. Returns None if the
    string doesn't cover today (caller falls back to the clock)."""
    try:
        tz = pytz.timezone(tz_name or 'US/Eastern')
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone('US/Eastern')
    local = now.astimezone(tz)
    today = local.strftime('%Y%m%d')
    covered = False
    for part in (liquid_hours or '').split(';'):
        part = part.strip()
        if not part:
            continue
        if part.endswith(':CLOSED'):
            if part.startswith(today):
                covered = True
            continue
        try:
            start_s, end_s = part.split('-')
            start = tz.localize(datetime.strptime(start_s, '%Y%m%d:%H%M'))
            end = tz.localize(datetime.strptime(end_s, '%Y%m%d:%H%M'))
        except ValueError:
            continue
        if start_s.startswith(today):
            covered = True
        if start <= local < end:
            return True
    return False if covered else None


def staged_close_price(right_side, bid, ask, mark):
    """Starting limit price for a staged closing order -- Dad edits it in
    TWS before transmitting anyway, so this just needs to be sensible and a
    valid increment: buying to close starts at the ask (else the mark),
    selling at the bid; rounded away from the market to $0.01 under $3 and
    $0.05 above, which every US option class accepts."""
    if right_side == 'BUY':
        price = ask if ask > 0 else mark
    else:
        price = bid if bid > 0 else mark
    if not price or price <= 0:
        return 0.01 if right_side == 'BUY' else None
    tick = 0.01 if price < 3 else 0.05
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if right_side == 'BUY' else math.floor(steps + 1e-9)
    return max(0.01, round(steps * tick, 2))


ACCOUNT_TAGS = {
    'NetLiquidation': 'net_liquidation',
    'TotalCashValue': 'cash',
    'AvailableFunds': 'available_funds',
    'BuyingPower': 'buying_power',
    'ExcessLiquidity': 'excess_liquidity',
}


def account_summary(account_values):
    """Pick the handful of account balances the UI shows out of IBKR's
    streamed account values (already subscribed by connectAsync for a
    single-account login -- no extra request). Prefers USD, falls back to
    the BASE-currency row. Missing tags are simply absent."""
    picked = {}
    for av in account_values or []:
        key = ACCOUNT_TAGS.get(getattr(av, 'tag', None))
        if not key:
            continue
        currency = getattr(av, 'currency', '')
        if currency not in ('USD', 'BASE'):
            continue
        try:
            value = float(av.value)
        except (TypeError, ValueError):
            continue
        if key not in picked or currency == 'USD':
            picked[key] = value
    return picked


def option_mark(ticker):
    """Best available per-share price for an option, plus where it came from.

    Returns (mark, source) with source in 'live' | 'ask' | 'last' |
    'prev_close', or (None, None) when there is no quote at all.

    Why this isn't just marketPrice(): a nearly-worthless option (e.g. a far
    OTM put on expiration day) usually has NO bid, so ib_async's
    hasBidAsk() is false, marketPrice() falls through to `last` (NaN when it
    hasn't traded today), and the old code coerced that to 0 -> "0% premium
    left" -> a BUYBACK alert driven purely by missing data, while a sibling
    contract that happened to have a stale prev close showed none. The ask
    is the real cost to buy back a short when there's no bid, so it comes
    before last/close; and "no quote" stays None rather than posing as $0.
    """
    # marketPrice() quietly returns `last` when there's no two-sided quote,
    # so only trust it as "live" when a real bid and ask exist.
    if ticker.hasBidAsk():
        price = safe_price(ticker.marketPrice())
        if price > 0:
            return price, 'live'
    ask = safe_price(ticker.ask)
    if ask > 0 and safe_price(ticker.askSize) > 0:
        return ask, 'ask'
    last = safe_price(ticker.last)
    if last > 0:
        return last, 'last'
    close = safe_price(ticker.close)
    if close > 0:
        return close, 'prev_close'
    return None, None


class IBKRUnavailableError(Exception):
    """Base class for IBKR connection failures classified by root cause."""
    verdict = 'unknown'

    def __init__(self, message, probes=None, attempts=None):
        super().__init__(message)
        self.probes = probes or []
        self.attempts = attempts or []


class NoListenerError(IBKRUnavailableError):
    """No IBKR client (TWS or Gateway) is listening on any known port."""
    verdict = 'no_listener'


class HandshakeTimeoutError(IBKRUnavailableError):
    """A port was open but the API handshake timed out (API likely disabled)."""
    verdict = 'handshake_timeout'


class ClientIdInUseError(IBKRUnavailableError):
    """Another client is already connected with this clientId."""
    verdict = 'client_id_in_use'


class NotConnectedError(IBKRUnavailableError):
    """The manager is currently disconnected from IBKR."""
    verdict = 'not_connected'


def probe_ib_ports(endpoints=None, timeout=None):
    """Fast TCP pre-check for each IBKR endpoint.

    Returns a list of dicts: [{host, port, label, reachable, latency_ms, error}].

    Uses a non-blocking connect + select() rather than socket.settimeout(),
    which is unreliable for this on Windows: connect_ex() on a blocking
    socket with a timeout can return WSAEWOULDBLOCK (10035) -- "still in
    progress" -- for both open and closed ports once the timeout elapses,
    making it impossible to tell them apart from the return code alone.
    select() lets us wait for the socket to become writable (or, on
    Windows, show up as exceptional -- how Windows signals a failed
    connect) and then read the real outcome via SO_ERROR.
    """
    if endpoints is None:
        endpoints = DEFAULT_ENDPOINTS
    if timeout is None:
        timeout = PROBE_TIMEOUT

    results = []
    for host, port, label in endpoints:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        reachable = False
        err_code = None
        err_msg = None
        try:
            err_code = sock.connect_ex((host, port))
            if err_code == 0:
                reachable = True
            else:
                _, writable, exceptional = select.select([], [sock], [sock], timeout)
                if writable or exceptional:
                    err_code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    reachable = (err_code == 0)
                else:
                    err_code = -1
                    err_msg = 'timeout'
        except Exception as e:
            err_code = -1
            err_msg = str(e)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        latency_ms = int((time.time() - start) * 1000)

        if reachable:
            results.append({
                'host': host, 'port': port, 'label': label,
                'reachable': True, 'latency_ms': latency_ms, 'error': None,
            })
        else:
            if err_msg is None:
                # WSAECONNREFUSED (Windows), ECONNREFUSED (Linux=111, macOS=61)
                if err_code in (10061, 111, 61):
                    err_msg = 'connection refused'
                elif err_code == -1:
                    err_msg = 'timeout'
                else:
                    err_msg = f'errno {err_code}'
            results.append({
                'host': host, 'port': port, 'label': label,
                'reachable': False, 'latency_ms': latency_ms, 'error': err_msg,
            })
    return results


def classify_handshake_error(exc):
    """Map an ib_async exception to a verdict string."""
    msg = str(exc).lower()
    if 'clientid' in msg or 'client id' in msg or 'already in use' in msg or 'peer closed' in msg:
        return 'client_id_in_use'
    if isinstance(exc, asyncio.TimeoutError) or 'timeout' in msg or 'timed out' in msg:
        return 'handshake_timeout'
    return 'unknown'


def compute_backoff(consecutive_failures, base=BACKOFF_BASE, cap=BACKOFF_CAP):
    """Exponential backoff with jitter: 2, 4, 8 ... capped at 60s."""
    if consecutive_failures <= 0:
        return 0
    delay = min(cap, base * (2 ** (consecutive_failures - 1)))
    return delay + random.uniform(0, delay * 0.25)


def safe_price(value):
    """Coerce None/NaN/inf ticker values to 0."""
    if value is None:
        return 0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if math.isnan(f) or math.isinf(f):
        return 0
    return f


class IBKRManager:
    """Singleton owner of the IBKR connection and market-data subscriptions."""

    def __init__(self, endpoints=None, client_id=None, on_client_id_change=None,
                 qual_failure_cache=None):
        self.endpoints = endpoints or DEFAULT_ENDPOINTS
        self.client_id = client_id or random.randint(100, 999)
        self._on_client_id_change = on_client_id_change
        self._qual_cache = qual_failure_cache

        self._loop = None
        self._thread = None
        self._ib = None
        self._stop_event = None          # asyncio.Event on the loop
        self._retry_now_event = None     # asyncio.Event: skip remaining backoff
        self._snapshot_lock = None       # asyncio.Lock for single-flight
        self._started = threading.Event()

        # Status, read from Flask threads (attribute reads are GIL-atomic)
        self.state = 'starting'          # starting|connecting|connected|reconnecting|stopped
        self.consecutive_failures = 0
        self.last_error = None           # verdict string
        self.last_error_message = None
        self.last_probes = []
        self.last_attempts = []
        self.last_success = None         # datetime
        self.connected_endpoint = None   # (host, port, label)
        self.next_retry_at = None        # datetime

        self._contracts = {}             # symbol -> qualified stock Contract
        self._tickers = {}               # symbol -> stock Ticker
        self._opt_contracts = {}         # conId -> qualified option Contract
        self._opt_tickers = {}           # conId -> option Ticker
        self._last_snapshot = None
        self._last_snapshot_time = 0

    # ---------- lifecycle ----------

    def start(self):
        self._thread = threading.Thread(target=self._run, name='ibkr-manager', daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._retry_now_event = asyncio.Event()
        self._snapshot_lock = asyncio.Lock()
        self._ib = IB()
        self._ib.disconnectedEvent += self._on_disconnected
        self._started.set()
        try:
            self._loop.run_until_complete(self._connection_loop())
        except Exception as e:
            logger.error(f'IBKR manager loop crashed: {e}', exc_info=True)
        finally:
            try:
                if self._ib.isConnected():
                    self._ib.disconnect()
            except Exception:
                pass
            self._loop.close()
            self.state = 'stopped'

    def stop(self):
        if self._loop and self._stop_event:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=10)

    def _on_disconnected(self):
        if self.state == 'connected':
            logger.warning('IBKR connection lost')
            self.state = 'reconnecting'

    # ---------- connection loop ----------

    async def _connection_loop(self):
        while not self._stop_event.is_set():
            if not self._ib.isConnected():
                connected = await self._try_connect_all()
                if not connected:
                    delay = compute_backoff(self.consecutive_failures)
                    self.next_retry_at = datetime.now() + timedelta(seconds=delay)
                    await self._interruptible_sleep(delay)
                    continue
            # Connected: heartbeat, then idle until the next check
            try:
                await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), timeout=HEARTBEAT_TIMEOUT)
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.warning(f'IBKR heartbeat failed ({e}); reconnecting')
                self._safe_disconnect()
                self.state = 'reconnecting'
                continue
            await self._interruptible_sleep(HEARTBEAT_INTERVAL)

    async def _interruptible_sleep(self, seconds):
        """Sleep, but wake early on stop or an explicit retry request."""
        self._retry_now_event.clear()
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        retry_task = asyncio.ensure_future(self._retry_now_event.wait())
        done, pending = await asyncio.wait(
            [stop_task, retry_task], timeout=seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    def poke(self):
        """Ask the manager to retry connecting now (e.g. user clicked Refresh)."""
        if self._loop and self._retry_now_event:
            try:
                self._loop.call_soon_threadsafe(self._retry_now_event.set)
            except RuntimeError:
                pass

    async def _try_connect_all(self):
        """One full connection attempt across all reachable endpoints."""
        self.state = 'connecting' if self.last_success is None else 'reconnecting'
        probes = probe_ib_ports(self.endpoints)
        self.last_probes = probes
        open_endpoints = [p for p in probes if p['reachable']]

        if not open_endpoints:
            self._record_failure(NoListenerError(
                'No IBKR client (TWS or Gateway) is listening on any standard port.',
                probes=probes,
            ))
            return False

        attempts = []
        for probe in open_endpoints:
            host, port, label = probe['host'], probe['port'], probe['label']
            client_ids = [self.client_id] + [
                random.randint(100, 999) for _ in range(CLIENT_ID_RETRIES)
            ]
            for attempt_idx, client_id in enumerate(client_ids):
                try:
                    await self._ib.connectAsync(host, port, clientId=client_id,
                                                timeout=CONNECT_TIMEOUT)
                    logger.info(f'Connected to IBKR on {host}:{port} ({label}) clientId={client_id}')
                    if client_id != self.client_id:
                        self.client_id = client_id
                        if self._on_client_id_change:
                            try:
                                self._on_client_id_change(client_id)
                            except Exception:
                                pass
                    attempts.append({'host': host, 'port': port, 'label': label,
                                     'connected': True, 'error': None})
                    self.last_attempts = attempts
                    await self._on_connected(host, port, label)
                    return True
                except Exception as e:
                    verdict = classify_handshake_error(e)
                    logger.warning(f'Handshake failed on {host}:{port} ({label}) '
                                   f'clientId={client_id} [{verdict}]: {e}')
                    attempts.append({'host': host, 'port': port, 'label': label,
                                     'connected': False, 'error': str(e), 'verdict': verdict})
                    self._safe_disconnect()
                    if verdict != 'client_id_in_use':
                        break  # only retry alternate ids for id conflicts

        self.last_attempts = attempts
        verdicts = [a.get('verdict') for a in attempts]
        if 'client_id_in_use' in verdicts:
            error = ClientIdInUseError(
                'IBKR is reachable but every clientId we tried is already in use.',
                probes=probes, attempts=attempts)
        elif 'handshake_timeout' in verdicts:
            error = HandshakeTimeoutError(
                'IBKR port is open but the API handshake timed out. Is the API enabled in TWS?',
                probes=probes, attempts=attempts)
        else:
            error = IBKRUnavailableError(
                'IBKR port is open but the API handshake failed for an unknown reason.',
                probes=probes, attempts=attempts)
        self._record_failure(error)
        return False

    async def _on_connected(self, host, port, label):
        self.state = 'connected'
        self.consecutive_failures = 0
        self.last_error = None
        self.last_error_message = None
        self.last_success = datetime.now()
        self.connected_endpoint = (host, port, label)
        self.next_retry_at = None
        # Live data where subscribed, delayed everywhere else — removes the
        # "no market data subscription -> zero price" hole.
        try:
            self._ib.reqMarketDataType(3)
        except Exception as e:
            logger.warning(f'Could not set market data type: {e}')
        # Re-establish standing subscriptions after a reconnect
        symbols = list(self._contracts.keys())
        self._tickers.clear()
        for symbol in symbols:
            try:
                self._tickers[symbol] = self._ib.reqMktData(
                    self._contracts[symbol], STOCK_GENERIC_TICKS)
            except Exception as e:
                logger.warning(f'Could not resubscribe {symbol}: {e}')
        conids = list(self._opt_contracts.keys())
        self._opt_tickers.clear()
        for conid in conids:
            try:
                self._opt_tickers[conid] = self._ib.reqMktData(self._opt_contracts[conid])
            except Exception as e:
                logger.warning(f'Could not resubscribe option {conid}: {e}')

    def _record_failure(self, error):
        self.consecutive_failures += 1
        self.last_error = error.verdict
        self.last_error_message = str(error)
        if error.probes:
            self.last_probes = error.probes
        if error.attempts:
            self.last_attempts = error.attempts

    def _safe_disconnect(self):
        try:
            self._ib.disconnect()
        except Exception:
            pass

    # ---------- market data ----------

    def is_connected(self):
        return bool(self._ib and self._ib.isConnected())

    def retry_in_seconds(self):
        if not self.next_retry_at:
            return 0
        return max(0, int((self.next_retry_at - datetime.now()).total_seconds()))

    def get_snapshot(self, watchlist_symbols, timeout=25):
        """Fetch positions + prices from the manager thread (called from Flask).

        Returns {'positions_raw': [...], 'market_data': {...}, 'failed_symbols': [...]}.
        Raises IBKRUnavailableError when disconnected.
        """
        if not self.is_connected():
            self.poke()
            error_cls = {
                'no_listener': NoListenerError,
                'handshake_timeout': HandshakeTimeoutError,
                'client_id_in_use': ClientIdInUseError,
            }.get(self.last_error, NotConnectedError)
            raise error_cls(
                self.last_error_message or 'Not connected to IBKR.',
                probes=self.last_probes, attempts=self.last_attempts,
            )
        future = asyncio.run_coroutine_threadsafe(
            self._build_snapshot(list(watchlist_symbols)), self._loop)
        return future.result(timeout)

    async def _build_snapshot(self, watchlist_symbols):
        async with self._snapshot_lock:
            # Coalesce: auto-refresh racing a manual refresh reuses a fresh result
            if (self._last_snapshot is not None
                    and time.time() - self._last_snapshot_time < SNAPSHOT_MAX_AGE):
                return self._last_snapshot

            positions = await self._ib.reqPositionsAsync()

            positions_raw = []
            desired_symbols = set(watchlist_symbols)
            option_positions = {}  # conId -> position info
            for position in positions:
                contract = position.contract
                positions_raw.append({
                    'symbol': contract.symbol,
                    'secType': contract.secType,
                    'right': getattr(contract, 'right', ''),
                    'position': position.position,
                    'avgCost': float(position.avgCost) if position.avgCost else 0,
                    'conId': contract.conId,
                })
                if contract.secType in ('STK', 'OPT'):
                    desired_symbols.add(contract.symbol)
                if contract.secType == 'OPT' and position.position and contract.conId:
                    option_positions[contract.conId] = {
                        'position': position.position,
                        'avgCost': float(position.avgCost) if position.avgCost else 0,
                        'symbol': contract.symbol,
                    }

            failed_symbols = await self._ensure_subscriptions(desired_symbols)
            options = await self._ensure_option_subscriptions(option_positions)

            now_str = datetime.now().isoformat()
            market_data = {}
            for symbol, ticker in self._tickers.items():
                last = safe_price(ticker.marketPrice())
                if last <= 0:
                    last = safe_price(ticker.last) or safe_price(ticker.close)
                close = safe_price(ticker.close)
                div = ticker.dividends
                market_data[symbol] = {
                    'dividend': ({'next_date': div.nextDate.isoformat() if div.nextDate else None,
                                  'next_amount': div.nextAmount}
                                 if div and div.nextDate else None),
                    'last': last,
                    'open': safe_price(ticker.open),
                    'close': close,
                    'high': safe_price(ticker.high),
                    'low': safe_price(ticker.low),
                    'change': (last - close) if (last and close) else 0,
                    'source': 'ibkr',
                    'timestamp': now_str,
                }

            tick_times = [t.time for t in self._tickers.values() if t.time]
            snapshot = {
                'positions_raw': positions_raw,
                'market_data': market_data,
                'failed_symbols': failed_symbols,
                'options': options,
                'account': account_summary(self._ib.accountValues()),
                'open_orders': await self._fetch_open_orders(),
                'fills_today': await self._fetch_fills_today(),
                'market_session': await self._market_session(),
                # Newest price tick across all stock subscriptions: if this
                # stops moving during market hours, prices on screen are
                # frozen even though the socket still says "connected"
                # (e.g. Warning 2103, market data farm connection broken).
                'last_tick': max(tick_times).isoformat() if tick_times else None,
            }
            self._last_snapshot = snapshot
            self._last_snapshot_time = time.time()
            return snapshot

    async def _fetch_open_orders(self):
        """All working orders on the account -- including ones Dad typed into
        TWS by hand (reqAllOpenOrders, not just this API client's)."""
        try:
            trades = await asyncio.wait_for(
                self._ib.reqAllOpenOrdersAsync(), ORDERS_FILLS_TIMEOUT)
        except Exception as e:
            logger.warning(f'Could not fetch open orders: {e}')
            return None
        return [order_to_dict(t) for t in trades
                if t.orderStatus.status in ACTIVE_ORDER_STATUSES]

    async def _fetch_fills_today(self):
        """Today's executions straight from TWS, so trades show up now rather
        than after the next day's Flex import."""
        try:
            fills = await asyncio.wait_for(
                self._ib.reqExecutionsAsync(), ORDERS_FILLS_TIMEOUT)
        except Exception as e:
            logger.warning(f'Could not fetch executions: {e}')
            return None
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).date()
        rows = []
        for f in fills:
            t = f.execution.time
            if t and t.astimezone(eastern).date() != today:
                continue
            rows.append(fill_to_dict(f))
        rows.sort(key=lambda r: r['time'] or '', reverse=True)
        return rows

    async def _market_session(self):
        """{'open': bool|None, 'source': 'ibkr'} from SPY's liquidHours,
        fetched once per day."""
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).date()
        cached = getattr(self, '_session_cache', None)
        if not cached or cached[0] != today:
            try:
                details = await asyncio.wait_for(
                    self._ib.reqContractDetailsAsync(Stock('SPY', 'SMART', 'USD')),
                    ORDERS_FILLS_TIMEOUT)
                d = details[0] if details else None
                cached = (today, d.liquidHours if d else '', d.timeZoneId if d else '')
            except Exception as e:
                logger.warning(f'Could not fetch trading hours: {e}')
                cached = (today, '', '')
            self._session_cache = cached
        is_open = parse_liquid_hours(cached[1], cached[2], datetime.now(pytz.utc))
        return {'open': is_open, 'source': 'ibkr' if is_open is not None else None}

    # ---------- staged orders ----------

    def stage_close_order(self, conid, timeout=10):
        """Put an UNTRANSMITTED limit order closing the whole option position
        `conid` into TWS. It shows up in TWS's Orders panel with a Transmit
        button; nothing reaches the exchange until Dad clicks it there.
        Quantity and side come from the live position, never from the caller,
        so this can only ever close what's actually held."""
        if not self.is_connected():
            raise NotConnectedError('Not connected to IBKR.')
        future = asyncio.run_coroutine_threadsafe(self._stage_close(conid), self._loop)
        return future.result(timeout)

    async def _stage_close(self, conid):
        positions = await self._ib.reqPositionsAsync()
        held = next((p for p in positions
                     if p.contract.conId == conid and p.contract.secType == 'OPT'
                     and p.position), None)
        if held is None:
            return {'ok': False, 'error': 'That option position is no longer open.'}
        contract = self._opt_contracts.get(conid)
        if contract is None:
            contract = Contract(conId=conid, exchange='SMART')
            await self._ib.qualifyContractsAsync(contract)
        ticker = self._opt_tickers.get(conid)
        action = 'BUY' if held.position < 0 else 'SELL'
        mark, _ = option_mark(ticker) if ticker else (None, None)
        price = staged_close_price(
            action,
            safe_price(ticker.bid) if ticker else 0,
            safe_price(ticker.ask) if ticker else 0,
            mark or 0)
        if price is None:
            return {'ok': False, 'error': 'No price to start the order from.'}
        order = LimitOrder(action, abs(held.position), price,
                           transmit=False, tif='DAY', orderRef=STAGED_ORDER_REF)
        trade = self._ib.placeOrder(contract, order)
        # TWS answers an untransmitted order with openOrder (success) or an
        # error (e.g. 321 read-only API). Give it a moment to do either.
        for _ in range(20):
            await asyncio.sleep(0.1)
            errors = [e for e in trade.log if e.errorCode]
            if errors:
                code, msg = errors[-1].errorCode, errors[-1].message
                if code == 321 or 'read-only' in (msg or '').lower():
                    msg = ('TWS is in Read-Only API mode. In TWS: File > Global '
                           'Configuration > API > Settings, uncheck "Read-Only API".')
                return {'ok': False, 'error': msg, 'code': code}
            if trade.orderStatus.status or trade.order.permId:
                break
        self._last_snapshot = None  # next refresh should show the new order
        return {
            'ok': True, 'order_id': trade.order.orderId, 'action': action,
            'quantity': abs(held.position), 'limit_price': price,
            'description': f'{action} {abs(held.position):g} {contract.localSymbol or contract.symbol} @ {price:.2f} LMT',
        }

    async def _ensure_option_subscriptions(self, option_positions):
        """Maintain standing subscriptions for option positions (by conId) and
        read mark + model greeks. Returns a list of option row dicts."""
        # Drop subscriptions for options no longer held
        for conid in list(self._opt_tickers.keys()):
            if conid not in option_positions:
                try:
                    self._ib.cancelMktData(self._opt_contracts[conid])
                except Exception:
                    pass
                self._opt_tickers.pop(conid, None)
                self._opt_contracts.pop(conid, None)

        # Qualify new option positions concurrently (one batched call instead
        # of one round-trip per conId -- serial qualification of a large
        # portfolio was blowing past the get_snapshot() timeout on cold start)
        to_qualify = {
            conid: Contract(conId=conid, exchange='SMART')
            for conid in option_positions
            if conid not in self._opt_tickers and conid not in self._opt_contracts
        }
        if to_qualify:
            try:
                await self._ib.qualifyContractsAsync(*to_qualify.values())
            except Exception as e:
                logger.warning(f'Error qualifying option contracts: {e}')
            for conid, stub in to_qualify.items():
                if stub.conId:
                    self._opt_contracts[conid] = stub
                else:
                    logger.info(f'Could not qualify option conId={conid} '
                                f'({option_positions[conid].get("symbol")})')

        # Subscribe new option positions
        new_tickers = []
        for conid, info in option_positions.items():
            if conid in self._opt_tickers:
                continue
            contract = self._opt_contracts.get(conid)
            if contract is None:
                continue
            try:
                ticker = self._ib.reqMktData(contract)
                self._opt_tickers[conid] = ticker
                new_tickers.append(ticker)
            except Exception as e:
                logger.warning(f'Error requesting option market data conId={conid}: {e}')

        # Wait for first data on new option tickers (greeks can lag the price)
        if new_tickers:
            deadline = time.time() + FIRST_PRICE_DEADLINE
            while time.time() < deadline:
                if all(safe_price(t.marketPrice()) > 0 or t.modelGreeks
                       for t in new_tickers):
                    break
                await asyncio.sleep(0.2)

        options = []
        today = datetime.now().date()
        for conid, info in option_positions.items():
            ticker = self._opt_tickers.get(conid)
            contract = self._opt_contracts.get(conid)
            if ticker is None or contract is None:
                continue
            greeks = ticker.modelGreeks
            expiry_raw = contract.lastTradeDateOrContractMonth or ''
            expiry = None
            dte = None
            if len(expiry_raw) >= 8:
                try:
                    expiry_date = datetime.strptime(expiry_raw[:8], '%Y%m%d').date()
                    expiry = expiry_date.isoformat()
                    dte = (expiry_date - today).days
                except ValueError:
                    pass
            multiplier = safe_price(contract.multiplier) or 100
            mark, mark_source = option_mark(ticker)
            options.append({
                'conId': conid,
                'symbol': contract.symbol,
                'localSymbol': contract.localSymbol,
                'right': contract.right,
                'strike': safe_price(contract.strike),
                'expiry': expiry,
                'dte': dte,
                'position': info['position'],
                'multiplier': multiplier,
                'entry_price': (info['avgCost'] / multiplier) if multiplier else 0,
                'mark': mark,
                'mark_source': mark_source,
                'delta': safe_price(greeks.delta) if greeks else None,
                'gamma': safe_price(greeks.gamma) if greeks else None,
                'theta': safe_price(greeks.theta) if greeks else None,
                'vega': safe_price(greeks.vega) if greeks else None,
                'iv': safe_price(greeks.impliedVol) if greeks else None,
                'und_price': safe_price(greeks.undPrice) if greeks else None,
            })
        return options

    async def _ensure_subscriptions(self, desired_symbols):
        """Diff standing subscriptions against the desired set.
        Returns symbols that could not be qualified (for external fallback)."""
        failed = []

        # Drop subscriptions we no longer need
        for symbol in list(self._tickers.keys()):
            if symbol not in desired_symbols:
                try:
                    self._ib.cancelMktData(self._contracts[symbol])
                except Exception:
                    pass
                self._tickers.pop(symbol, None)
                self._contracts.pop(symbol, None)

        # Qualify new symbols concurrently (one batched call instead of one
        # round-trip per symbol -- serial qualification of a large portfolio
        # was blowing past the get_snapshot() timeout on cold start)
        to_qualify = []
        for symbol in sorted(desired_symbols):
            if symbol in self._tickers or symbol in self._contracts:
                continue
            if self._qual_cache and self._qual_cache.is_failed(symbol):
                failed.append(symbol)
                continue
            to_qualify.append(symbol)
        if to_qualify:
            await self._qualify_many(to_qualify, failed)

        # Subscribe new symbols
        new_tickers = []
        for symbol in sorted(desired_symbols):
            if symbol in self._tickers or symbol in failed:
                continue
            contract = self._contracts.get(symbol)
            if contract is None:
                continue
            try:
                ticker = self._ib.reqMktData(contract, STOCK_GENERIC_TICKS)
                self._tickers[symbol] = ticker
                new_tickers.append(ticker)
            except Exception as e:
                logger.warning(f'Error requesting market data for {symbol}: {e}')
                failed.append(symbol)

        # Event-paced wait for first prices on newly subscribed tickers only
        if new_tickers:
            deadline = time.time() + FIRST_PRICE_DEADLINE
            while time.time() < deadline:
                if all(safe_price(t.marketPrice()) > 0 for t in new_tickers):
                    break
                await asyncio.sleep(0.2)

        return failed

    async def _qualify_many(self, symbols, failed):
        """Qualify many stock contracts in one batched, concurrent call;
        retry stragglers once without SMART for odd listings."""
        contracts = {symbol: Stock(symbol, 'SMART', 'USD') for symbol in symbols}
        try:
            await self._ib.qualifyContractsAsync(*contracts.values())
        except Exception as e:
            logger.debug(f'Error qualifying contracts (SMART): {e}')

        stragglers = [s for s, c in contracts.items() if not c.conId]
        if stragglers:
            retry = {s: Stock(s, '', 'USD') for s in stragglers}
            try:
                await self._ib.qualifyContractsAsync(*retry.values())
            except Exception as e:
                logger.debug(f'Error qualifying contracts (no exchange): {e}')
            contracts.update(retry)

        for symbol, contract in contracts.items():
            if contract.conId:
                self._contracts[symbol] = contract
                if self._qual_cache:
                    self._qual_cache.record_success(symbol)
            else:
                logger.info(f'Contract qualification failed for {symbol}')
                if self._qual_cache:
                    self._qual_cache.record_failure(symbol, 'qualification failed')
                failed.append(symbol)

    # ---------- diagnostics ----------

    def status(self):
        last_endpoint = self.connected_endpoint
        return {
            'state': self.state,
            'connected': self.is_connected(),
            'client_id': self.client_id,
            'consecutive_failures': self.consecutive_failures,
            'retry_in_seconds': self.retry_in_seconds(),
            'last_error': self.last_error,
            'last_error_message': self.last_error_message,
            'last_success': self.last_success.isoformat() if self.last_success else None,
            'endpoint': {
                'host': last_endpoint[0] if last_endpoint else None,
                'port': last_endpoint[1] if last_endpoint else None,
                'label': last_endpoint[2] if last_endpoint else None,
            },
            'subscriptions': len(self._tickers) + len(self._opt_tickers),
            'probes': list(self.last_probes),
        }
