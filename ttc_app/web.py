# Flask application: routes and request-side data assembly.
#
# Serves the bundled ui/ files (no more runtime-generated resources). Shared
# runtime objects (database, IBKR manager, webview window) live on `state`,
# set up by main.py before the server starts.

import json
import logging
import math
import os
import platform
import secrets
import threading
import time

from datetime import datetime
from types import SimpleNamespace

import pytz
from flask import Flask, jsonify, render_template, request

from ttc_app import app_update, flex_client
from ttc_app.config import (
    APP_DIR, APP_NAME, APP_VERSION, STATIC_DIR, TEMPLATE_DIR, USER_AGENT,
)
from ttc_app.ibkr_manager import IBKRUnavailableError, probe_ib_ports
from ttc_app.price_sources import fetch_cboe_prices, fetch_yahoo_prices, is_cusip
from ttc_app.tranches import income_summary, rebuild_tranches

logger = logging.getLogger(__name__)

DEFAULT_BUYBACK_THRESHOLD_PCT = 15.0

# Shared runtime state, populated by main.py
state = SimpleNamespace(
    db=None,               # ttc_app.db.Database
    ibkr=None,             # ttc_app.ibkr_manager.IBKRManager
    webview_window=None,
    pending_update=None,
    startup_messages=[],   # (message, kind) toasts queued before the window exists
    flex_import={'running': False, 'started_ts': None, 'result': None},
    cleanup=lambda: None,
)

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.config['SECRET_KEY'] = secrets.token_hex(32)


# ============================================
# User-Friendly Error Messages
# ============================================
FRIENDLY_ERRORS = {
    'connection refused': "Please make sure Trader Workstation is running, then click Refresh.",
    'failed to connect': "Please make sure Trader Workstation is running, then click Refresh.",
    'not connected': "IBKR isn't responding. Is Trader Workstation open?",
    'timeout': "IBKR is taking too long to respond. Please try again.",
    'no market data': "Couldn't get market data. The market may be closed.",
    'rate limit': "Too many requests. Please wait a moment and try again.",
}

DIAGNOSTIC_MESSAGES = {
    'no_listener': (
        'No IBKR client (TWS or Gateway) is listening on any standard port. '
        'Start Trader Workstation, then in TWS go to: '
        'File → Global Configuration → API → Settings, '
        'and enable “Enable ActiveX and Socket Clients”. '
        'Make sure 127.0.0.1 is in the Trusted IPs list, then click Refresh.'
    ),
    'handshake_timeout': (
        'TWS/Gateway is running but the API handshake timed out. '
        'Open File → Global Configuration → API → Settings and confirm: '
        '“Enable ActiveX and Socket Clients” is checked, the Socket port matches, '
        'and 127.0.0.1 is in the Trusted IPs list. Then click Refresh.'
    ),
    'client_id_in_use': (
        'TWS/Gateway is running but every client ID this app tried is already in use. '
        'Close any other API client (or another copy of this app), then click Refresh.'
    ),
    'not_connected': (
        'Not connected to IBKR yet. The app reconnects automatically; '
        'click Refresh to retry now.'
    ),
    'unknown': (
        'TWS/Gateway is running but the API connection failed for an unknown reason. '
        'Try restarting TWS, then click Refresh.'
    ),
    'ok': 'IBKR is connected.',
}


def get_friendly_error(error_message):
    error_lower = str(error_message).lower()
    for key, friendly in FRIENDLY_ERRORS.items():
        if key in error_lower:
            return friendly
    if 'ibkr' in error_lower or 'ib ' in error_lower or 'tws' in error_lower:
        return "There was a problem connecting to IBKR. Please make sure Trader Workstation is running."
    return f"Something went wrong: {error_message}"


# ============================================
# Utilities
# ============================================
def is_market_open():
    eastern = pytz.timezone('US/Eastern')
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0).time()
    market_close = now.replace(hour=16, minute=0, second=0).time()
    return market_open <= now.time() <= market_close


def calculate_shares_available(shares, np, cc, uc):
    cc_shares = cc * 100 if cc else 0
    uc_shares = uc * 100 if uc else 0
    return shares - cc_shares - uc_shares


def format_data_age(timestamp_str):
    if not timestamp_str:
        return 'Unknown'
    try:
        ts = datetime.fromisoformat(timestamp_str)
        delta = datetime.now() - ts
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return 'Just now'
        elif total_seconds < 3600:
            return f'{total_seconds // 60}m ago'
        elif total_seconds < 86400:
            return f'{total_seconds // 3600}h ago'
        else:
            return f'{total_seconds // 86400}d ago'
    except Exception:
        return 'Unknown'


def safe_number(value):
    if value is None:
        return 0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if math.isnan(f) or math.isinf(f):
        return 0
    return f


def buyback_threshold_pct():
    return safe_number(state.db.get_setting(
        'buyback_threshold_pct', DEFAULT_BUYBACK_THRESHOLD_PCT)) or DEFAULT_BUYBACK_THRESHOLD_PCT


# ============================================
# Price fallbacks (Yahoo -> Cboe -> DB history)
# ============================================
def apply_price_fallbacks(market_data, wanted_symbols, missing_symbols):
    """Fill missing prices: Yahoo -> Cboe -> latest DB price. Mutates
    market_data, returns data_sources for symbols served by a fallback."""
    data_sources = {}

    missing = sorted(set(missing_symbols))
    if missing:
        logger.info(
            f'Attempting Yahoo Finance fallback for {len(missing)} symbols: '
            f'{", ".join(missing[:10])}{"..." if len(missing) > 10 else ""}'
        )
        yahoo_data = fetch_yahoo_prices(missing, USER_AGENT)
        for symbol, ydata in yahoo_data.items():
            market_data[symbol] = ydata
            data_sources[symbol] = 'yahoo'

        still_missing = [s for s in missing if s not in yahoo_data]
        if still_missing:
            cboe_data = fetch_cboe_prices(still_missing, USER_AGENT)
            for symbol, cdata in cboe_data.items():
                market_data[symbol] = cdata
                data_sources[symbol] = 'cboe'

    stored = None
    for symbol in wanted_symbols:
        if symbol not in market_data or market_data[symbol].get('last', 0) == 0:
            if stored is None:
                stored = state.db.latest_prices()
            cached = stored.get(symbol)
            if cached and (cached.get('last') or 0) > 0:
                market_data[symbol] = {**cached, 'source': 'cached'}
                data_sources[symbol] = 'cached'

    return data_sources


# ============================================
# Positions data assembly
# ============================================
def get_ibkr_data():
    """Get positions + prices via the persistent IBKR manager, with
    Yahoo/Cboe/DB fallback for symbols IBKR couldn't price."""
    if state.ibkr is None:
        raise IBKRUnavailableError('IBKR connection manager is not running.')

    watchlist = state.db.get_watchlist()
    snapshot = state.ibkr.get_snapshot([s for s in watchlist if not is_cusip(s)])
    positions = snapshot['positions_raw']
    market_data = dict(snapshot['market_data'])
    options = snapshot.get('options', [])

    stock_symbols = {p['symbol'] for p in positions if p['secType'] in ('STK', 'OPT')}
    stock_symbols.update(s for s in watchlist if not is_cusip(s))
    stock_symbols = {s for s in stock_symbols if not is_cusip(s)}

    symbols_needing_fallback = list(snapshot['failed_symbols'])
    data_sources = {}
    for symbol in stock_symbols:
        if market_data.get(symbol, {}).get('last', 0) > 0:
            data_sources[symbol] = 'ibkr'
        else:
            symbols_needing_fallback.append(symbol)

    data_sources.update(
        apply_price_fallbacks(market_data, stock_symbols, symbols_needing_fallback))

    # Persist history (throttled inside the DB layer)
    state.db.record_prices(market_data)
    if options:
        state.db.record_option_snapshots(options)

    now_str = datetime.now().isoformat()
    for symbol in market_data:
        if 'source' not in market_data[symbol]:
            market_data[symbol]['source'] = data_sources.get(symbol, 'ibkr')
        if 'timestamp' not in market_data[symbol]:
            market_data[symbol]['timestamp'] = now_str

    stock_positions = {}
    option_positions = {}
    watchlist_updated = False

    for position in positions:
        symbol = position['symbol']
        sec_type = position['secType']

        if sec_type in ('STK', 'OPT') and not is_cusip(symbol):
            if symbol not in watchlist:
                watchlist.append(symbol)
                watchlist_updated = True

        if sec_type == 'STK':
            stock_positions[symbol] = {
                'symbol': symbol,
                'shares': position['position'],
                'avgCost': position['avgCost'],
                'marketPrice': market_data.get(symbol, {}).get('last', 0),
            }
        elif sec_type == 'OPT':
            option_positions.setdefault(symbol, []).append({
                'symbol': symbol,
                'right': position['right'],
                'position': position['position'],
            })

    if watchlist_updated:
        state.db.set_watchlist(watchlist)

    basic_data = {
        'positions': [],
        'incomplete_lots': [],
        'watchlist': [s for s in watchlist if s not in stock_positions and not is_cusip(s)],
        'market_data': market_data,
        'data_sources': data_sources,
        'connection_source': 'ibkr',
        'options': options,
    }

    for symbol, stock_data in stock_positions.items():
        stock_quantity = stock_data['shares']
        complete_lots = (abs(stock_quantity) // 100) * 100
        incomplete_lot = abs(stock_quantity) % 100

        calls = [opt for opt in option_positions.get(symbol, []) if opt['right'] == 'C']
        puts = [opt for opt in option_positions.get(symbol, []) if opt['right'] == 'P']

        call_quantity = sum(opt['position'] for opt in calls)
        put_quantity = sum(opt['position'] for opt in puts)

        if stock_quantity > 0:
            covered_calls = min((complete_lots // 100), abs(call_quantity))
            uncovered_calls = max(0, abs(call_quantity) - (complete_lots // 100))
        else:
            covered_calls = 0
            uncovered_calls = abs(call_quantity)

        naked_puts = abs(put_quantity)

        basic_data['positions'].append({
            'symbol': symbol,
            'shares': stock_quantity,
            'avgCost': stock_data['avgCost'],
            'marketPrice': stock_data['marketPrice'],
            'naked_puts': naked_puts,
            'covered_calls': covered_calls,
            'uncovered_calls': uncovered_calls
        })

        if incomplete_lot > 0:
            basic_data['incomplete_lots'].append({
                'symbol': symbol,
                'shares': incomplete_lot,
                'avgCost': stock_data['avgCost'],
                'marketPrice': stock_data['marketPrice'],
            })

    return basic_data


def enhance_with_market_data(basic_data):
    """Process market data into the legacy positional-array format the
    Positions tab renders, plus the new object-keyed options payload."""
    market_data = basic_data['market_data']
    data_sources = basic_data.get('data_sources', {})
    connection_source = basic_data.get('connection_source', 'ibkr')

    def safe_divide(a, b):
        try:
            if b == 0 or a == 0:
                return 0
            result = a / b
            return 0 if math.isnan(result) or math.isinf(result) else result
        except Exception:
            return 0

    def process_market_data(symbol, mkt_data):
        current_price = safe_number(mkt_data.get('last', 0))
        daily_change = safe_number(mkt_data.get('change', 0))
        close_price = safe_number(mkt_data.get('close', current_price))
        open_price = safe_number(mkt_data.get('open', current_price))

        base_price = current_price - daily_change if current_price != daily_change else current_price
        daily_change_pct = safe_divide(daily_change, base_price)
        opening_gap = safe_number(open_price - close_price if close_price else 0)

        source = mkt_data.get('source', data_sources.get(symbol, 'ibkr'))
        timestamp = mkt_data.get('timestamp', '')
        data_age = format_data_age(timestamp) if source == 'cached' else ''

        return {
            'current_price': current_price,
            'daily_change': daily_change,
            'daily_change_pct': daily_change_pct,
            'close_price': close_price,
            'open_price': open_price,
            'opening_gap': opening_gap,
            'source': source,
            'data_age': data_age
        }

    enhanced_positions = []
    for pos in basic_data['positions']:
        symbol = pos['symbol']
        data = process_market_data(symbol, market_data.get(symbol, {}))
        enhanced_positions.append([
            symbol,
            safe_number(pos['shares']),
            data['current_price'],
            safe_number(pos['avgCost']),
            data['daily_change'],
            data['daily_change_pct'],
            data['close_price'],
            data['open_price'],
            data['opening_gap'],
            safe_number(pos['naked_puts']),
            safe_number(pos['covered_calls']),
            safe_number(pos['uncovered_calls']),
            safe_number(calculate_shares_available(
                pos['shares'], pos['naked_puts'],
                pos['covered_calls'], pos['uncovered_calls'])),
            data['source'],
            data['data_age']
        ])

    enhanced_incomplete = []
    for lot in basic_data['incomplete_lots']:
        symbol = lot['symbol']
        data = process_market_data(symbol, market_data.get(symbol, {}))
        enhanced_incomplete.append([
            symbol, safe_number(lot['shares']), data['current_price'],
            safe_number(lot['avgCost']), data['daily_change'],
            data['daily_change_pct'], data['close_price'], data['open_price'],
            data['opening_gap'], data['source'], data['data_age']
        ])

    enhanced_watchlist = []
    for symbol in basic_data['watchlist']:
        data = process_market_data(symbol, market_data.get(symbol, {}))
        enhanced_watchlist.append([
            symbol, data['current_price'], data['daily_change'],
            data['daily_change_pct'], data['close_price'], data['open_price'],
            data['opening_gap'], data['source'], data['data_age']
        ])

    # Options grouped by underlying, with premium-remaining for shorts
    threshold = buyback_threshold_pct()
    options_by_symbol = {}
    for opt in basic_data.get('options', []):
        entry = safe_number(opt.get('entry_price'))
        mark = safe_number(opt.get('mark'))
        premium_remaining_pct = None
        if entry > 0 and mark >= 0:
            premium_remaining_pct = round(100 * mark / entry, 1)
        row = {
            'localSymbol': opt.get('localSymbol'),
            'right': opt.get('right'),
            'strike': opt.get('strike'),
            'expiry': opt.get('expiry'),
            'dte': opt.get('dte'),
            'position': opt.get('position'),
            'entry_price': round(entry, 4),
            'mark': round(mark, 4),
            'premium_remaining_pct': premium_remaining_pct,
            'buyback_target_hit': bool(
                opt.get('position', 0) < 0
                and premium_remaining_pct is not None
                and premium_remaining_pct <= threshold),
            'delta': opt.get('delta'),
            'theta': opt.get('theta'),
            'iv': opt.get('iv'),
        }
        options_by_symbol.setdefault(opt.get('symbol'), []).append(row)
    for rows in options_by_symbol.values():
        rows.sort(key=lambda r: (r.get('expiry') or '', r.get('strike') or 0))

    return {
        'positions': enhanced_positions,
        'incomplete_lots': enhanced_incomplete,
        'watchlist': enhanced_watchlist,
        'connection_source': connection_source,
        'options_by_symbol': options_by_symbol,
        'buyback_threshold_pct': threshold,
    }


def _serve_external_fallback(reason):
    """Yahoo/Cboe + DB-history fallback response when IBKR is unavailable."""
    try:
        logger.info(f'Serving fallback (reason: {reason})...')
        stored = state.db.latest_prices()
        watchlist = [s for s in state.db.get_watchlist() if not is_cusip(s)]
        all_symbols = sorted(set(list(stored.keys()) + watchlist))
        if not all_symbols:
            return None, None

        market_data = {}
        data_sources = apply_price_fallbacks(market_data, all_symbols, all_symbols)
        live_sources = {s for s in data_sources.values() if s in ('yahoo', 'cboe')}

        fresh = {s: d for s, d in market_data.items() if d.get('source') != 'cached'}
        if fresh:
            state.db.record_prices(fresh)

        fallback_data = {
            'positions': [],
            'incomplete_lots': [],
            'watchlist': all_symbols,
            'market_data': market_data,
            'data_sources': data_sources,
            'connection_source': ('yahoo' if 'yahoo' in live_sources
                                  else 'cboe' if 'cboe' in live_sources
                                  else 'cached'),
            'options': [],
        }

        enhanced_data = enhance_with_market_data(fallback_data)
        last_update = state.db.last_price_update()
        cache_age = format_data_age(last_update or '')
        logger.info(f'Serving fallback data: {len(all_symbols)} symbols')

        enhanced_data['fallback'] = True
        enhanced_data['fallback_reason'] = reason
        if live_sources:
            source_label = 'Yahoo Finance' if 'yahoo' in live_sources else 'Cboe'
            enhanced_data['fallback_message'] = (
                f'IBKR disconnected. Showing current {source_label} prices.')
        else:
            enhanced_data['fallback_message'] = (
                f'IBKR disconnected. Showing cached prices ({cache_age}).')
        if state.ibkr is not None:
            retry_in = state.ibkr.retry_in_seconds()
            if retry_in > 0:
                enhanced_data['fallback_message'] += f' Reconnecting in {retry_in}s.'
            else:
                enhanced_data['fallback_message'] += ' Reconnecting...'
        return enhanced_data, 200
    except Exception as fallback_error:
        logger.error(f'Fallback also failed: {fallback_error}', exc_info=True)
        return None, None


# ============================================
# Core routes
# ============================================
@app.route('/')
def index():
    return render_template('index.html', version=APP_VERSION)


@app.route('/api/data')
def get_data():
    logger.info('API /api/data called - starting data fetch')
    try:
        ibkr_data = get_ibkr_data()
        enhanced_data = enhance_with_market_data(ibkr_data)
        logger.info(f'Data fetch complete: {len(enhanced_data.get("positions", []))} positions')
        return jsonify(enhanced_data)
    except IBKRUnavailableError as e:
        logger.warning(f'IBKR unavailable [{e.verdict}]: {e}')
        enhanced_data, _ = _serve_external_fallback(e.verdict)
        if enhanced_data is not None:
            return jsonify(enhanced_data)
        return jsonify({
            'error': get_friendly_error(str(e)),
            'technical_error': str(e),
            'verdict': e.verdict,
            'positions': [], 'incomplete_lots': [], 'watchlist': [],
            'connection_source': 'unavailable',
        }), 503
    except Exception as e:
        logger.error(f'Error in get_data: {str(e)}', exc_info=True)
        enhanced_data, _ = _serve_external_fallback('unknown')
        if enhanced_data is not None:
            return jsonify(enhanced_data)
        return jsonify({
            'error': get_friendly_error(str(e)),
            'technical_error': str(e),
            'positions': [], 'incomplete_lots': [], 'watchlist': [],
            'connection_source': 'unavailable',
        }), 500


@app.route('/api/test')
def api_test():
    return jsonify({'status': 'ok', 'message': 'Server is running'})


@app.route('/api/status')
def get_status():
    return jsonify({
        'app_name': APP_NAME,
        'version': APP_VERSION,
        'market_open': is_market_open(),
    })


@app.route('/api/version')
def get_version():
    return jsonify({'version': APP_VERSION})


@app.route('/api/diagnostics')
def api_diagnostics():
    try:
        return jsonify(build_diagnostics())
    except Exception as e:
        logger.error(f'Error building diagnostics: {e}', exc_info=True)
        return jsonify({'error': 'Could not build diagnostics',
                        'technical_error': str(e)}), 500


def build_diagnostics():
    probes = probe_ib_ports()
    open_endpoints = [p for p in probes if p['reachable']]
    status = state.ibkr.status() if state.ibkr is not None else {}

    if status.get('connected'):
        verdict = 'ok'
    elif status.get('last_error'):
        verdict = status['last_error']
    elif not open_endpoints:
        verdict = 'no_listener'
    else:
        verdict = 'not_connected'

    stored = state.db.latest_prices()
    last_update = state.db.last_price_update()
    endpoint = status.get('endpoint', {})
    last_success = status.get('last_success')

    return {
        'platform': platform.platform(),
        'app_version': APP_VERSION,
        'client_id': status.get('client_id'),
        'endpoints': probes,
        'breaker': {
            'open': not status.get('connected', False),
            'consecutive_failures': status.get('consecutive_failures', 0),
            'retry_in_seconds': status.get('retry_in_seconds', 0),
            'last_error': status.get('last_error'),
            'last_error_message': status.get('last_error_message'),
        },
        'connection': {
            'state': status.get('state'),
            'subscriptions': status.get('subscriptions', 0),
        },
        'last_success': {
            'timestamp': last_success,
            'age': format_data_age(last_success) if last_success else None,
            'host': endpoint.get('host'),
            'port': endpoint.get('port'),
            'label': endpoint.get('label'),
        },
        'cache': {
            'symbols': len(stored),
            'last_updated': last_update,
            'age': format_data_age(last_update or ''),
        },
        'verdict': verdict,
        'user_message': DIAGNOSTIC_MESSAGES.get(verdict, DIAGNOSTIC_MESSAGES['unknown']),
    }


# ============================================
# Update routes
# ============================================
@app.route('/api/update/check')
def api_check_updates():
    update_info = app_update.check_for_updates(APP_VERSION, USER_AGENT)
    if update_info.get('available'):
        state.pending_update = update_info
    return jsonify(update_info)


@app.route('/api/update/download')
def api_download_update():
    pending = state.pending_update
    if not pending or not pending.get('download_url'):
        return jsonify({'success': False, 'error': 'No update available'})

    try:
        download_path = app_update.download_update(
            pending['download_url'], pending['asset_name'], USER_AGENT)
        if not download_path:
            return jsonify({'success': False, 'error': 'Download failed'})

        if not app_update.verify_download(
                download_path, pending['asset_name'],
                pending.get('checksums_url'), USER_AGENT):
            try:
                os.remove(download_path)
            except OSError:
                pass
            return jsonify({'success': False,
                            'error': 'Update failed verification and was discarded.'})

        install_thread = threading.Thread(
            target=app_update.install_update,
            args=(download_path, state.cleanup), daemon=True)
        install_thread.start()
        return jsonify({'success': True, 'message': 'Update installing...'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================
# Tranches / income / settings / flex import
# ============================================
def rebuild_and_store_tranches():
    """Rebuild tranches from the trades table (+ live positions for seeding)
    and persist the result. Returns (tranches, events)."""
    trades = state.db.get_trades()
    current_positions = None
    if state.ibkr is not None and state.ibkr.is_connected():
        try:
            watchlist = state.db.get_watchlist()
            snapshot = state.ibkr.get_snapshot(
                [s for s in watchlist if not is_cusip(s)])
            current_positions = [
                {'symbol': p['symbol'], 'shares': p['position'],
                 'avgCost': p['avgCost']}
                for p in snapshot['positions_raw'] if p['secType'] == 'STK']
        except Exception as e:
            logger.debug(f'Seeding skipped (IBKR unavailable): {e}')

    tranches, events = rebuild_tranches(trades, current_positions)
    state.db.replace_tranches(tranches, events)
    return tranches, events


@app.route('/api/tranches')
def api_tranches():
    try:
        tranches, _ = rebuild_and_store_tranches()

        prices = {}
        if state.ibkr is not None and state.ibkr.is_connected():
            try:
                snapshot = state.ibkr.get_snapshot(
                    [s for s in state.db.get_watchlist() if not is_cusip(s)])
                prices = {s: d.get('last', 0) for s, d in snapshot['market_data'].items()}
            except Exception:
                pass
        if not prices:
            prices = {s: (d.get('last') or 0) for s, d in state.db.latest_prices().items()}

        by_symbol = {}
        for t in tranches:
            symbol = t['symbol']
            current = safe_number(prices.get(symbol, 0))
            row = dict(t)
            row['current_price'] = current
            if t['status'] == 'OPEN':
                stock_pl = (current - (t['open_price'] or 0)) * t['qty'] if current else None
                row['unrealized_pl'] = (
                    round(stock_pl + t['premium'], 2) if stock_pl is not None else None)
                qty = t['qty'] or 1
                row['net_basis'] = round((t['open_price'] or 0) - (t['premium'] / qty), 4)
                row['sell_would_uncover'] = t.get('covering_call') is not None
            by_symbol.setdefault(symbol, []).append(row)

        groups = []
        for symbol in sorted(by_symbol.keys()):
            rows = by_symbol[symbol]
            open_rows = [r for r in rows if r['status'] == 'OPEN']
            closed_rows = [r for r in rows if r['status'] == 'CLOSED']
            groups.append({
                'symbol': symbol,
                'open': open_rows,
                'closed': closed_rows,
                'open_shares': sum(r['qty'] for r in open_rows),
                'total_premium': round(sum(r['premium'] or 0 for r in rows), 2),
                'realized_pl': round(sum(r['realized_pl'] or 0 for r in closed_rows), 2),
            })

        return jsonify({
            'groups': groups,
            'trade_count': state.db.trade_count(),
            'last_import': state.db.last_flex_import(),
            'flex_configured': bool(state.db.get_setting('flex_token')
                                    and state.db.get_setting('flex_query_id')),
        })
    except Exception as e:
        logger.error(f'Error in /api/tranches: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/income')
def api_income():
    try:
        tranches = state.db.get_tranches()
        events = state.db.get_events()
        closed = [t for t in tranches if t['status'] == 'CLOSED']
        summary = income_summary(events, closed)
        summary['weekly_goal'] = safe_number(state.db.get_setting('weekly_premium_goal', 0))
        summary['trade_count'] = state.db.trade_count()
        summary['flex_configured'] = bool(state.db.get_setting('flex_token')
                                          and state.db.get_setting('flex_query_id'))
        return jsonify(summary)
    except Exception as e:
        logger.error(f'Error in /api/income: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    token = state.db.get_setting('flex_token') or ''
    return jsonify({
        'flex_query_id': state.db.get_setting('flex_query_id') or '',
        'flex_token_masked': (token[:4] + '...' + token[-4:]) if len(token) > 8 else ('set' if token else ''),
        'flex_token_set': bool(token),
        'buyback_threshold_pct': buyback_threshold_pct(),
        'weekly_premium_goal': safe_number(state.db.get_setting('weekly_premium_goal', 0)),
        'data_dir': os.path.dirname(state.db.path),
        'trade_count': state.db.trade_count(),
        'last_import': state.db.last_flex_import(),
    })


@app.route('/api/settings', methods=['POST'])
def api_post_settings():
    payload = request.get_json(silent=True) or {}
    if 'flex_query_id' in payload:
        state.db.set_setting('flex_query_id', str(payload['flex_query_id']).strip())
    # Only overwrite the token when a new one is actually entered
    if payload.get('flex_token'):
        state.db.set_setting('flex_token', str(payload['flex_token']).strip())
    if 'buyback_threshold_pct' in payload:
        state.db.set_setting('buyback_threshold_pct',
                             safe_number(payload['buyback_threshold_pct']) or DEFAULT_BUYBACK_THRESHOLD_PCT)
    if 'weekly_premium_goal' in payload:
        state.db.set_setting('weekly_premium_goal',
                             safe_number(payload['weekly_premium_goal']))
    return jsonify({'success': True})


def _run_flex_import():
    try:
        token = state.db.get_setting('flex_token')
        query_id = state.db.get_setting('flex_query_id')
        result = flex_client.run_import(state.db, token, query_id, USER_AGENT)
        if result.get('ok'):
            rebuild_and_store_tranches()
        state.flex_import['result'] = result
    except Exception as e:
        logger.error(f'Flex import thread failed: {e}', exc_info=True)
        state.flex_import['result'] = {'ok': False, 'error': str(e)}
    finally:
        state.flex_import['running'] = False


@app.route('/api/flex/import', methods=['POST'])
def api_flex_import():
    if state.flex_import['running']:
        return jsonify({'success': False, 'error': 'An import is already running.'})
    token = state.db.get_setting('flex_token')
    query_id = state.db.get_setting('flex_query_id')
    if not token or not query_id:
        return jsonify({'success': False,
                        'error': 'Enter your Flex token and query ID in Settings first.'})
    state.flex_import.update({'running': True,
                              'started_ts': datetime.now().isoformat(),
                              'result': None})
    threading.Thread(target=_run_flex_import, daemon=True).start()
    return jsonify({'success': True, 'message': 'Import started.'})


@app.route('/api/flex/status')
def api_flex_status():
    return jsonify({
        'running': state.flex_import['running'],
        'started_ts': state.flex_import['started_ts'],
        'result': state.flex_import['result'],
        'last_import': state.db.last_flex_import(),
        'trade_count': state.db.trade_count(),
    })


@app.route('/api/export', methods=['POST'])
def api_export():
    try:
        export_dir = os.path.join(APP_DIR, 'export')
        written = state.db.export_to(export_dir)
        return jsonify({'success': True, 'files': [os.path.basename(p) for p in written],
                        'directory': export_dir})
    except Exception as e:
        logger.error(f'Export failed: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Webview helpers + background threads
# ============================================
def show_update_dialog(update_info):
    if not state.webview_window:
        logger.info(f"Update available: {update_info['latest_version']}")
        return
    try:
        version_js = json.dumps(update_info.get('latest_version', ''))
        notes_js = json.dumps(update_info.get('release_notes', ''))
        state.webview_window.evaluate_js(
            f'if (typeof showUpdateNotification === "function") '
            f'{{ showUpdateNotification({version_js}, {notes_js}); }}')
    except Exception as e:
        logger.warning(f"Could not show update dialog: {e}")


def show_startup_toast(message, kind='info'):
    if not state.webview_window:
        return
    try:
        state.webview_window.evaluate_js(
            'if (typeof showToast === "function") { showToast('
            + json.dumps(message) + ', ' + json.dumps(kind) + ', 6000); }')
    except Exception as e:
        logger.debug(f'Could not show startup toast: {e}')


def check_updates_background():
    time.sleep(3)
    for message, kind in state.startup_messages:
        show_startup_toast(message, kind)
    update_info = app_update.check_for_updates(APP_VERSION, USER_AGENT)
    if update_info.get('available'):
        state.pending_update = update_info
        show_update_dialog(update_info)


def auto_flex_import_background():
    """Daily automatic Flex import shortly after startup, if configured."""
    time.sleep(10)
    try:
        token = state.db.get_setting('flex_token')
        query_id = state.db.get_setting('flex_query_id')
        if not token or not query_id:
            return
        last = state.db.last_flex_import()
        if last and last.get('status') == 'ok':
            try:
                age_hours = (datetime.now()
                             - datetime.fromisoformat(last['requested_ts'])).total_seconds() / 3600
                if age_hours < 20:
                    return
            except (TypeError, ValueError):
                pass
        if state.flex_import['running']:
            return
        logger.info('Starting automatic daily Flex import...')
        state.flex_import.update({'running': True,
                                  'started_ts': datetime.now().isoformat(),
                                  'result': None})
        _run_flex_import()
    except Exception as e:
        logger.warning(f'Auto Flex import failed: {e}')
