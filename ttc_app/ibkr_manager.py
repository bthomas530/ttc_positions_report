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

from ib_async import IB, Stock, Contract

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
                self._tickers[symbol] = self._ib.reqMktData(self._contracts[symbol])
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
                market_data[symbol] = {
                    'last': last,
                    'open': safe_price(ticker.open),
                    'close': close,
                    'high': safe_price(ticker.high),
                    'low': safe_price(ticker.low),
                    'change': (last - close) if (last and close) else 0,
                    'source': 'ibkr',
                    'timestamp': now_str,
                }

            snapshot = {
                'positions_raw': positions_raw,
                'market_data': market_data,
                'failed_symbols': failed_symbols,
                'options': options,
            }
            self._last_snapshot = snapshot
            self._last_snapshot_time = time.time()
            return snapshot

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
            mark = safe_price(ticker.marketPrice())
            if mark <= 0:
                mark = safe_price(ticker.last) or safe_price(ticker.close)
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
                ticker = self._ib.reqMktData(contract)
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
