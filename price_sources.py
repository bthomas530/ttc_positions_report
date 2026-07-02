# External price sources and symbol helpers.
# Fallback chain (after IBKR): Yahoo Finance -> Cboe delayed quotes -> disk cache.

import json
import logging
import os
import ssl
import urllib.error
import urllib.request

from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

USER_AGENT = 'TTC-Positions-Report'

QUAL_FAILURE_TTL_HOURS = 24

# Use certifi's CA bundle when available (needed on macOS dev setups where
# Python lacks system certs); production Windows uses the OS cert store.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)


def is_cusip(symbol):
    """Check if a symbol looks like a CUSIP identifier (bonds).
    CUSIPs are 9-character alphanumeric with digits mixed in."""
    if len(symbol) < 8:
        return False
    digit_count = sum(1 for c in symbol if c.isdigit())
    return digit_count >= 3  # Real stock tickers rarely have 3+ digits


def fetch_yahoo_prices(symbols, user_agent=USER_AGENT):
    """Fetch price data from Yahoo Finance for multiple symbols.
    Returns dict of {symbol: {last, open, close, high, low, change, source}}"""
    if not symbols:
        return {}

    results = {}
    # Filter out non-stock symbols (CUSIPs, bonds)
    valid_symbols = [s for s in symbols if not is_cusip(s)]
    if not valid_symbols:
        return {}

    # Use the v8 chart endpoint for each symbol (more reliable than v7 quote)
    for symbol in valid_symbols:
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d'
            req = urllib.request.Request(url, headers={
                'User-Agent': user_agent,
                'Accept': 'application/json'
            })
            with _urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))

            chart = data.get('chart', {}).get('result', [])
            if chart:
                meta = chart[0].get('meta', {})
                current_price = meta.get('regularMarketPrice', 0)
                prev_close = meta.get('chartPreviousClose', 0) or meta.get('previousClose', 0)

                # Try to get OHLC from indicators
                indicators = chart[0].get('indicators', {}).get('quote', [{}])
                quote = indicators[0] if indicators else {}
                opens = quote.get('open', [])
                highs = quote.get('high', [])
                lows = quote.get('low', [])

                open_price = opens[-1] if opens and opens[-1] is not None else current_price
                high_price = highs[-1] if highs and highs[-1] is not None else current_price
                low_price = lows[-1] if lows and lows[-1] is not None else current_price
                close_price = prev_close if prev_close else current_price

                change = current_price - close_price if close_price else 0

                if current_price and current_price > 0:
                    results[symbol] = {
                        'last': current_price,
                        'open': open_price,
                        'close': close_price,
                        'high': high_price,
                        'low': low_price,
                        'change': change,
                        'source': 'yahoo'
                    }
        except urllib.error.HTTPError as e:
            logger.debug(f'Yahoo Finance HTTP error for {symbol}: {e.code}')
        except Exception as e:
            logger.debug(f'Yahoo Finance error for {symbol}: {e}')

    if results:
        logger.info(f'Yahoo Finance fallback: got prices for {len(results)}/{len(valid_symbols)} symbols')

    return results


def parse_cboe_quote(payload):
    """Parse Cboe delayed-quote JSON into our price dict, or None if unusable."""
    data = payload.get('data') or {}
    last = data.get('current_price')
    if not last or last <= 0:
        return None
    prev_close = data.get('prev_day_close') or 0
    return {
        'last': last,
        'open': data.get('open') or last,
        'close': prev_close or last,
        'high': data.get('high') or last,
        'low': data.get('low') or last,
        'change': data.get('price_change')
                  if data.get('price_change') is not None
                  else (last - prev_close if prev_close else 0),
        'source': 'cboe'
    }


def fetch_cboe_prices(symbols, user_agent=USER_AGENT):
    """Fetch delayed quotes from Cboe's public CDN (keyless, ~15 min delayed).
    Returns dict of {symbol: {last, open, close, high, low, change, source}}"""
    if not symbols:
        return {}

    results = {}
    valid_symbols = [s for s in symbols if not is_cusip(s)]
    for symbol in valid_symbols:
        try:
            # Request the symbol as-is: stripping suffixes like .TEN would
            # silently return the price of a different security.
            cboe_symbol = symbol.upper()
            url = f'https://cdn.cboe.com/api/global/delayed_quotes/quotes/{cboe_symbol}.json'
            req = urllib.request.Request(url, headers={
                'User-Agent': user_agent,
                'Accept': 'application/json',
            })
            with _urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode('utf-8'))
            parsed = parse_cboe_quote(payload)
            if parsed:
                results[symbol] = parsed
        except urllib.error.HTTPError as e:
            logger.debug(f'Cboe HTTP error for {symbol}: {e.code}')
        except Exception as e:
            logger.debug(f'Cboe error for {symbol}: {e}')

    if results:
        logger.info(f'Cboe fallback: got prices for {len(results)}/{len(valid_symbols)} symbols')

    return results


class QualFailureCache:
    """Remembers symbols that failed IBKR contract qualification so they are
    skipped (and sent straight to Yahoo/Stooq) until the TTL expires."""

    def __init__(self, path, ttl_hours=QUAL_FAILURE_TTL_HOURS):
        self.path = path
        self.ttl = timedelta(hours=ttl_hours)
        self._entries = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    self._entries = json.load(f)
            except Exception as e:
                logger.warning(f'Could not load qualification-failure cache: {e}')
                self._entries = {}

    def _save(self):
        try:
            with open(self.path, 'w') as f:
                json.dump(self._entries, f, indent=2)
        except Exception as e:
            logger.warning(f'Could not save qualification-failure cache: {e}')

    def is_failed(self, symbol, now=None):
        entry = self._entries.get(symbol)
        if not entry:
            return False
        now = now or datetime.now()
        try:
            last_failed = datetime.fromisoformat(entry['last_failed'])
        except (KeyError, ValueError):
            return False
        if now - last_failed >= self.ttl:
            return False
        return True

    def record_failure(self, symbol, reason='', now=None):
        now = now or datetime.now()
        entry = self._entries.get(symbol, {'fail_count': 0})
        entry['fail_count'] = entry.get('fail_count', 0) + 1
        entry['last_failed'] = now.isoformat()
        entry['reason'] = str(reason)[:200]
        self._entries[symbol] = entry
        self._save()

    def record_success(self, symbol):
        if symbol in self._entries:
            del self._entries[symbol]
            self._save()

    def failed_symbols(self, now=None):
        return [s for s in self._entries if self.is_failed(s, now)]
