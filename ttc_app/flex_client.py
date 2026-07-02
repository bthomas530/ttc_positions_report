# IBKR Flex Query client (stdlib only).
#
# Two-step flow against FlexStatementService v3:
#   1. SendRequest with token + query id -> reference code + statement URL
#   2. GetStatement with reference code   -> statement XML (poll while 1019)
# The user creates the Trades Flex Query and web-service token once in IBKR
# Client Portal; the Settings tab documents the exact steps.

import logging
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime

logger = logging.getLogger(__name__)

SEND_REQUEST_URL = ('https://gdcdyn.interactivebrokers.com/Universal/servlet/'
                    'FlexStatementService.SendRequest')

POLL_DELAYS = [5, 5, 10, 10, 20, 30]  # seconds between GetStatement retries
RETRYABLE_CODES = {'1009', '1018', '1019'}

# Official Flex web-service error codes -> plain English
ERROR_MESSAGES = {
    '1001': 'IBKR could not generate the statement. Try again in a few minutes.',
    '1003': 'The statement is not available yet. Try again in a few minutes.',
    '1004': 'The statement is incomplete at IBKR. Try again later.',
    '1009': 'The IBKR server is busy. Try again in a few minutes.',
    '1010': 'This Flex query format is no longer supported. Recreate the query in Client Portal.',
    '1011': 'The Flex web service is inactive for this account. Enable it in Client Portal.',
    '1012': 'The Flex token has expired. Generate a new token in Client Portal and update Settings.',
    '1013': 'IBKR rejected the request because of an IP restriction on the token.',
    '1014': 'The query ID is invalid. Check the Query ID in Settings.',
    '1015': 'The token is invalid. Check the Token in Settings.',
    '1016': 'The account is invalid for this token.',
    '1017': 'The reference code is invalid. Try the import again.',
    '1018': 'Too many requests to IBKR. Wait a minute and try again.',
    '1019': 'IBKR is still generating the statement.',
    '1020': 'IBKR could not validate the request. Check the token and query ID.',
    '1021': 'The statement could not be retrieved. Try again later.',
}

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


class FlexError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def friendly_error(code, fallback=''):
    return ERROR_MESSAGES.get(str(code), fallback or f'IBKR Flex error {code}.')


def _http_get(url, user_agent, timeout=30):
    request = urllib.request.Request(url, headers={'User-Agent': user_agent})
    with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
        return response.read().decode('utf-8', errors='replace')


def send_request(token, query_id, user_agent, http_get=_http_get):
    """Step 1: request statement generation. Returns (reference_code, base_url)."""
    params = urllib.parse.urlencode({'t': token, 'q': query_id, 'v': '3'})
    text = http_get(f'{SEND_REQUEST_URL}?{params}', user_agent)
    root = ET.fromstring(text)
    status = (root.findtext('Status') or '').strip()
    if status.lower() != 'success':
        code = (root.findtext('ErrorCode') or '').strip()
        message = (root.findtext('ErrorMessage') or '').strip()
        raise FlexError(code, friendly_error(code, message))
    reference_code = (root.findtext('ReferenceCode') or '').strip()
    url = (root.findtext('Url') or '').strip()
    if not reference_code or not url:
        raise FlexError('', 'IBKR returned an unexpected response to the Flex request.')
    return reference_code, url


def fetch_statement(base_url, token, reference_code, user_agent,
                    http_get=_http_get, sleep=time.sleep):
    """Step 2: poll for the generated statement. Returns raw statement XML."""
    params = urllib.parse.urlencode({'q': reference_code, 't': token, 'v': '3'})
    url = f'{base_url}?{params}'
    last_error = None
    for attempt, delay in enumerate([0] + POLL_DELAYS):
        if delay:
            sleep(delay)
        text = http_get(url, user_agent)
        stripped = text.lstrip()
        if stripped.startswith('<FlexQueryResponse'):
            return text
        # Otherwise it's a FlexStatementResponse status/error document
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise FlexError('', 'IBKR returned an unreadable Flex response.')
        code = (root.findtext('ErrorCode') or '').strip()
        message = (root.findtext('ErrorMessage') or '').strip()
        if code in RETRYABLE_CODES:
            last_error = FlexError(code, friendly_error(code, message))
            logger.info(f'Flex statement not ready (code {code}), retrying...')
            continue
        raise FlexError(code, friendly_error(code, message))
    raise last_error or FlexError('', 'Timed out waiting for the Flex statement.')


def _parse_flex_datetime(value):
    """Flex dateTime shows up in several formats depending on query config."""
    if not value:
        return None
    value = value.strip()
    for fmt in ('%Y%m%d;%H%M%S', '%Y%m%d %H%M%S', '%Y-%m-%d, %H:%M:%S',
                '%Y-%m-%d;%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y%m%d'):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
    return None


def _parse_flex_date(value):
    if not value:
        return None
    value = value.strip()
    for fmt in ('%Y%m%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_trades(statement_xml):
    """Extract <Trade> elements into trade-row dicts matching the trades table."""
    root = ET.fromstring(statement_xml)
    trades = []
    for el in root.iter('Trade'):
        asset_category = (el.get('assetCategory') or '').strip()
        if asset_category not in ('STK', 'OPT'):
            continue
        symbol = (el.get('underlyingSymbol') or '').strip() if asset_category == 'OPT' else ''
        if not symbol:
            symbol = (el.get('symbol') or '').strip()
            if asset_category == 'OPT' and ' ' in symbol:
                # OCC-style option symbol like "AA 240816C00032500"
                symbol = symbol.split(' ')[0]

        exec_id = (el.get('tradeID') or el.get('transactionID') or '').strip()
        if not exec_id:
            continue

        trade_ts = (_parse_flex_datetime(el.get('dateTime'))
                    or _parse_flex_date(el.get('tradeDate')))

        trades.append({
            'exec_id': exec_id,
            'order_id': (el.get('ibOrderID') or '').strip(),
            'account': (el.get('accountId') or '').strip(),
            'symbol': symbol,
            'local_symbol': (el.get('symbol') or '').strip(),
            'sec_type': asset_category,
            'put_call': (el.get('putCall') or '').strip(),
            'strike': _to_float(el.get('strike'), None),
            'expiry': _parse_flex_date(el.get('expiry')),
            'multiplier': _to_float(el.get('multiplier'), 100 if asset_category == 'OPT' else 1),
            'buy_sell': (el.get('buySell') or '').strip().upper(),
            'open_close': (el.get('openCloseIndicator') or '').strip().upper(),
            'quantity': _to_float(el.get('quantity')),
            'price': _to_float(el.get('tradePrice')),
            'proceeds': _to_float(el.get('proceeds')),
            'commission': _to_float(el.get('ibCommission')),
            'trade_ts': trade_ts,
            'codes': (el.get('notes') or '').strip(),
        })
    trades.sort(key=lambda t: (t['trade_ts'] or '', t['exec_id']))
    return trades


def run_import(database, token, query_id, user_agent,
               http_get=_http_get, sleep=time.sleep):
    """Full import: request -> poll -> parse -> insert. Returns a result dict."""
    reference_code = None
    try:
        reference_code, base_url = send_request(token, query_id, user_agent, http_get)
        statement = fetch_statement(base_url, token, reference_code, user_agent,
                                    http_get, sleep)
        trades = parse_trades(statement)
        new_count = database.insert_trades(trades)
        database.record_flex_import(reference_code, len(trades), new_count, 'ok')
        logger.info(f'Flex import complete: {len(trades)} trades, {new_count} new')
        return {'ok': True, 'trade_count': len(trades), 'new_count': new_count}
    except FlexError as e:
        logger.warning(f'Flex import failed [{e.code}]: {e}')
        database.record_flex_import(reference_code, 0, 0, 'error', str(e))
        return {'ok': False, 'error': str(e), 'code': e.code}
    except Exception as e:
        logger.error(f'Flex import failed: {e}', exc_info=True)
        database.record_flex_import(reference_code, 0, 0, 'error', str(e))
        return {'ok': False, 'error': f'Import failed: {e}'}
