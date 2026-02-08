# TTC Positions Report - Desktop Application
# v2.0.4 - User-friendly desktop app with auto-updates
# Communicates with IBKR TWS and provides a native UI

import asyncio
import atexit
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import hashlib
import urllib.request
import urllib.error

from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, send_from_directory
from functools import wraps
from ib_async import IB, Stock
import json
import pytz

# Try to import webview for native window
try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False
    print("Note: pywebview not installed. Will open in browser instead.")
    print("Install with: pip install pywebview")

# ============================================
# Configuration
# ============================================
APP_NAME = "TTC Positions Report"
APP_VERSION = "2.0.4"
DEFAULT_PORT = 8082
MAX_PORT_TRIES = 10

# GitHub configuration for auto-updates
# Set these to your repository details
GITHUB_OWNER = "bthomas530"  # Change this to your GitHub username
GITHUB_REPO = "ttc_positions_report"   # Change this to your repository name
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# Determine directories
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    APP_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    # Running as script
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = APP_DIR

# Resource directories - check external first, then bundled
EXTERNAL_RESOURCES = os.path.join(APP_DIR, 'resources')
BUNDLED_RESOURCES = os.path.join(BUNDLE_DIR, 'resources')

# Use external resources if they exist, otherwise use bundled
if os.path.exists(EXTERNAL_RESOURCES):
    RESOURCES_DIR = EXTERNAL_RESOURCES
else:
    RESOURCES_DIR = BUNDLED_RESOURCES

# ============================================
# Logging Setup
# ============================================
log_dir = os.path.join(APP_DIR, 'log', datetime.now().strftime('%Y-%m-%d'))
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

log_file = os.path.join(log_dir, 'ttc_positions_app.log')
handler = RotatingFileHandler(log_file, maxBytes=1*1024*1024, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# ============================================
# Version and Update Checking
# ============================================
VERSION_FILE = os.path.join(APP_DIR, 'version.json')

def get_version_info():
    """Get current version information"""
    info = {
        'version': APP_VERSION,
        'resources_version': '1.0.0',
        'resources_dir': RESOURCES_DIR,
        'using_external': RESOURCES_DIR == EXTERNAL_RESOURCES
    }
    
    # Check if version file exists with resource version
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, 'r') as f:
                saved = json.load(f)
                info['resources_version'] = saved.get('resources_version', '1.0.0')
        except:
            pass
    
    return info

def save_version_info(resources_version):
    """Save version information"""
    info = {
        'app_version': APP_VERSION,
        'resources_version': resources_version,
        'updated_at': datetime.now().isoformat()
    }
    try:
        with open(VERSION_FILE, 'w') as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save version info: {e}")

# ============================================
# Auto-Update System
# ============================================
def parse_version(version_str):
    """Parse version string into tuple for comparison"""
    try:
        # Remove 'v' prefix if present
        v = version_str.lstrip('v')
        parts = v.split('.')
        return tuple(int(p) for p in parts[:3])
    except:
        return (0, 0, 0)

def check_for_updates():
    """Check GitHub for available updates (non-blocking)"""
    try:
        logger.info("Checking for updates...")
        
        # Create request with proper headers
        request = urllib.request.Request(
            GITHUB_API_URL,
            headers={'User-Agent': f'{APP_NAME}/{APP_VERSION}'}
        )
        
        # Set a short timeout so it doesn't block the app
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        latest_version = data.get('tag_name', '0.0.0')
        current = parse_version(APP_VERSION)
        latest = parse_version(latest_version)
        
        if latest > current:
            logger.info(f"Update available: {APP_VERSION} -> {latest_version}")
            
            # Find the right asset for this platform
            assets = data.get('assets', [])
            download_url = None
            asset_name = None
            
            if platform.system() == 'Windows':
                # Look for Windows installer
                for asset in assets:
                    name = asset.get('name', '').lower()
                    if 'setup' in name and name.endswith('.exe'):
                        download_url = asset.get('browser_download_url')
                        asset_name = asset.get('name')
                        break
                    elif name.endswith('.exe'):
                        download_url = asset.get('browser_download_url')
                        asset_name = asset.get('name')
            else:
                # Look for Mac app
                for asset in assets:
                    name = asset.get('name', '').lower()
                    if name.endswith('.dmg') or name.endswith('.zip'):
                        download_url = asset.get('browser_download_url')
                        asset_name = asset.get('name')
                        break
            
            return {
                'available': True,
                'current_version': APP_VERSION,
                'latest_version': latest_version,
                'download_url': download_url,
                'asset_name': asset_name,
                'release_notes': data.get('body', ''),
                'release_url': data.get('html_url', '')
            }
        else:
            logger.info(f"App is up to date (v{APP_VERSION})")
            return {'available': False, 'current_version': APP_VERSION}
            
    except urllib.error.URLError as e:
        logger.warning(f"Could not check for updates (network error): {e}")
        return {'available': False, 'error': 'network', 'message': "Couldn't check for updates. No worries, we'll try again next time."}
    except Exception as e:
        logger.warning(f"Could not check for updates: {e}")
        return {'available': False, 'error': 'unknown', 'message': str(e)}

def download_update(download_url, asset_name):
    """Download update file to temp directory"""
    try:
        logger.info(f"Downloading update: {asset_name}")
        
        # Create temp directory
        temp_dir = tempfile.gettempdir()
        download_path = os.path.join(temp_dir, asset_name)
        
        # Download with progress
        request = urllib.request.Request(
            download_url,
            headers={'User-Agent': f'{APP_NAME}/{APP_VERSION}'}
        )
        
        with urllib.request.urlopen(request) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 8192
            
            with open(download_path, 'wb') as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress = int((downloaded / total_size) * 100)
                        logger.info(f"Download progress: {progress}%")
        
        logger.info(f"Download complete: {download_path}")
        return download_path
        
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None

def install_update(installer_path):
    """Install the update and restart"""
    try:
        logger.info(f"Installing update: {installer_path}")
        
        if platform.system() == 'Windows':
            # Run the installer silently and exit this app
            # /SILENT = silent install, /CLOSEAPPLICATIONS = close running instances
            subprocess.Popen([installer_path, '/SILENT', '/CLOSEAPPLICATIONS'], 
                           creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
        else:
            # For Mac, open the DMG or unzip and replace
            if installer_path.endswith('.dmg'):
                subprocess.Popen(['open', installer_path])
            else:
                subprocess.Popen(['open', installer_path])
        
        # Exit this instance
        logger.info("Update initiated, exiting current instance...")
        cleanup()
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Install failed: {e}")
        return False

def show_update_dialog(update_info):
    """Show update dialog to user using webview"""
    global webview_window
    
    if not HAS_WEBVIEW or not webview_window:
        # Fallback: just log and continue
        logger.info(f"Update available: {update_info['latest_version']}")
        return
    
    try:
        # Use JavaScript to show update notification in the web UI
        # Prepare release notes (escape quotes and newlines)
        release_notes = update_info.get('release_notes', '')
        release_notes = release_notes.replace('"', '\\"').replace('\n', ' ')
        
        js_code = f'''
        if (typeof showUpdateNotification === 'function') {{
            showUpdateNotification("{update_info['latest_version']}", "{release_notes}");
        }} else {{
            if (confirm("A new version ({update_info['latest_version']}) is available!\\n\\nWould you like to update now?")) {{
                window.location.href = "/api/update/install";
            }}
        }}
        '''
        webview_window.evaluate_js(js_code)
    except Exception as e:
        logger.warning(f"Could not show update dialog: {e}")

# Global to store update info
pending_update = None

def check_updates_background():
    """Check for updates in background thread"""
    global pending_update
    time.sleep(3)  # Wait for app to fully start
    update_info = check_for_updates()
    if update_info.get('available'):
        pending_update = update_info
        # Try to show notification
        if HAS_WEBVIEW and webview_window:
            try:
                show_update_dialog(update_info)
            except:
                pass

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

def get_friendly_error(error_message):
    """Convert technical error to user-friendly message"""
    error_lower = str(error_message).lower()
    
    for key, friendly in FRIENDLY_ERRORS.items():
        if key in error_lower:
            return friendly
    
    # If no match, return a generic friendly message
    if 'ibkr' in error_lower or 'ib ' in error_lower or 'tws' in error_lower:
        return "There was a problem connecting to IBKR. Please make sure Trader Workstation is running."
    
    return f"Something went wrong: {error_message}"

# ============================================
# Config file
# ============================================
CONFIG_FILE = os.path.join(APP_DIR, 'ttc_watchlist.json')

def load_watchlist():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            return sorted(config.get('WATCHLIST', []))
    return sorted(["AAPL", "NVDA"])

def save_watchlist(watchlist):
    os.makedirs(os.path.dirname(CONFIG_FILE) if os.path.dirname(CONFIG_FILE) else '.', exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump({'WATCHLIST': sorted(watchlist)}, f, indent=4)

WATCHLIST = load_watchlist()

# ============================================
# Flask App Setup
# ============================================
template_dir = os.path.join(RESOURCES_DIR, 'templates')
static_dir = os.path.join(RESOURCES_DIR, 'static')

app = Flask(__name__, 
            template_folder=template_dir,
            static_folder=static_dir)
app.config['SECRET_KEY'] = 'ttc-positions-app-secret-key'

# Global state
server_thread = None
webview_window = None
shutdown_event = threading.Event()
ib_connection = None

# ============================================
# Utility Functions
# ============================================
def find_available_port(start_port=DEFAULT_PORT, max_tries=MAX_PORT_TRIES):
    """Find an available port"""
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise OSError("Could not find an available port")

def async_route(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()
    return wrapped

# ============================================
# IBKR Connection
# ============================================
async def connect_to_ib():
    """Connect to IBKR TWS or Gateway"""
    ib = IB()
    connected = False
    
    try:
        await ib.connectAsync('127.0.0.1', 7496, clientId=1)
        connected = True
        logger.info('Connected to IBKR on port 7496 (TWS)')
    except Exception as e:
        logger.warning(f'Connection to TWS port 7496 failed: {e}')
    
    if not connected:
        try:
            await ib.connectAsync('127.0.0.1', 7497, clientId=1)
            connected = True
            logger.info('Connected to IBKR on port 7497 (Gateway)')
        except Exception as e:
            logger.error(f'Connection to Gateway port 7497 failed: {e}')
            # User-friendly error message
            raise ConnectionError("Please make sure Trader Workstation is running, then click Refresh.")
    
    return ib

def is_market_open():
    """Check if US stock market is open"""
    eastern = pytz.timezone('US/Eastern')
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0).time()
    market_close = now.replace(hour=16, minute=0, second=0).time()
    return market_open <= now.time() <= market_close

def calculate_shares_available(shares, np, cc, uc):
    """Calculate available shares"""
    cc_shares = cc * 100 if cc else 0
    uc_shares = uc * 100 if uc else 0
    return shares - cc_shares - uc_shares

# ============================================
# Price Cache (Memory)
# ============================================
PRICE_CACHE_FILE = os.path.join(APP_DIR, 'price_cache.json')
CACHE_MAX_AGE_DAYS = 7

def load_price_cache():
    """Load cached price data from disk"""
    if os.path.exists(PRICE_CACHE_FILE):
        try:
            with open(PRICE_CACHE_FILE, 'r') as f:
                cache = json.load(f)
            logger.info(f'Loaded price cache with {len(cache.get("prices", {}))} symbols')
            return cache
        except Exception as e:
            logger.warning(f'Could not load price cache: {e}')
    return {'last_updated': None, 'prices': {}}

def save_price_cache(market_data):
    """Save current price data to disk cache, merging with existing data"""
    try:
        # Load existing cache and merge
        existing = load_price_cache()
        now_str = datetime.now().isoformat()
        
        for symbol, data in market_data.items():
            # Only cache if we have a real price
            if data.get('last', 0) > 0:
                existing['prices'][symbol] = {
                    **data,
                    'timestamp': now_str
                }
        
        # Prune entries older than CACHE_MAX_AGE_DAYS
        cutoff = (datetime.now() - timedelta(days=CACHE_MAX_AGE_DAYS)).isoformat()
        pruned = {}
        for sym, entry in existing['prices'].items():
            if entry.get('timestamp', '') >= cutoff:
                pruned[sym] = entry
        
        cache = {
            'last_updated': now_str,
            'prices': pruned
        }
        
        with open(PRICE_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
        
        logger.info(f'Saved price cache: {len(pruned)} symbols (pruned {len(existing["prices"]) - len(pruned)} stale entries)')
    except Exception as e:
        logger.warning(f'Could not save price cache: {e}')

def get_cached_price(symbol, cache=None):
    """Get a single symbol's cached price data, or None if not available"""
    if cache is None:
        cache = load_price_cache()
    return cache.get('prices', {}).get(symbol, None)

def format_data_age(timestamp_str):
    """Convert an ISO timestamp string into a human-readable age like '2h ago'"""
    if not timestamp_str:
        return 'Unknown'
    try:
        ts = datetime.fromisoformat(timestamp_str)
        delta = datetime.now() - ts
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return 'Just now'
        elif total_seconds < 3600:
            mins = total_seconds // 60
            return f'{mins}m ago'
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f'{hours}h ago'
        else:
            days = total_seconds // 86400
            return f'{days}d ago'
    except Exception:
        return 'Unknown'

# ============================================
# Yahoo Finance Fallback
# ============================================
def is_cusip(symbol):
    """Check if a symbol looks like a CUSIP identifier (bonds).
    CUSIPs are 9-character alphanumeric with digits mixed in."""
    if len(symbol) < 8:
        return False
    digit_count = sum(1 for c in symbol if c.isdigit())
    return digit_count >= 3  # Real stock tickers rarely have 3+ digits

def fetch_yahoo_prices(symbols):
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
    # Process in batches to avoid overwhelming Yahoo
    for symbol in valid_symbols:
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d'
            req = urllib.request.Request(url, headers={
                'User-Agent': f'{APP_NAME}/{APP_VERSION}',
                'Accept': 'application/json'
            })
            with urllib.request.urlopen(req, timeout=5) as response:
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
                closes = quote.get('close', [])
                
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
                    logger.debug(f'Yahoo Finance price for {symbol}: ${current_price:.2f}')
        except urllib.error.HTTPError as e:
            logger.debug(f'Yahoo Finance HTTP error for {symbol}: {e.code}')
        except Exception as e:
            logger.debug(f'Yahoo Finance error for {symbol}: {e}')
    
    if results:
        logger.info(f'Yahoo Finance fallback: got prices for {len(results)}/{len(valid_symbols)} symbols')
    
    return results

# ============================================
# Data Functions
# ============================================
async def get_ibkr_data():
    """Get position data from IBKR with Yahoo Finance fallback and price caching"""
    global WATCHLIST
    
    try:
        logger.info('Connecting to IBKR...')
        ib = await connect_to_ib()
        
        logger.info('Requesting positions...')
        positions = await ib.reqPositionsAsync()
        await asyncio.sleep(0.5)
        
        # Only collect symbols from STK and OPT positions (skip bonds, etc.)
        stock_symbols = set()
        skipped_types = {}
        for position in positions:
            contract = position.contract
            if contract.secType in ('STK', 'OPT'):
                stock_symbols.add(contract.symbol)
            else:
                skipped_types.setdefault(contract.secType, []).append(contract.symbol)
        
        # Log skipped non-stock position types once (not per-refresh spam)
        for sec_type, syms in skipped_types.items():
            logger.debug(f'Skipping {len(syms)} {sec_type} positions: {", ".join(syms[:5])}{"..." if len(syms) > 5 else ""}')
        
        # Add watchlist symbols, filtering out CUSIPs
        watchlist_stock_symbols = [s for s in WATCHLIST if not is_cusip(s)]
        stock_symbols.update(watchlist_stock_symbols)
        
        market_data = {}
        data_sources = {}  # Track source per symbol
        tickers = []
        
        # Create and qualify contracts only for stock-like symbols
        contracts = []
        for symbol in stock_symbols:
            if not is_cusip(symbol):
                contract = Stock(symbol, 'SMART', 'USD')
                contracts.append((symbol, contract))
        
        # Qualify all contracts in batch (this populates conId)
        logger.info(f'Qualifying {len(contracts)} contracts...')
        qualified_contracts = []
        failed_symbols = []
        for symbol, contract in contracts:
            try:
                qualified = await ib.qualifyContractsAsync(contract)
                if qualified and qualified[0].conId:
                    qualified_contracts.append((symbol, qualified[0]))
                else:
                    logger.debug(f'Could not qualify contract for {symbol}')
                    failed_symbols.append(symbol)
            except Exception as e:
                logger.debug(f'Error qualifying {symbol}: {e}')
                failed_symbols.append(symbol)
        
        if failed_symbols:
            logger.info(f'Contract qualification failed for {len(failed_symbols)} symbols, will try Yahoo Finance fallback')
        
        # Request market data for qualified contracts
        logger.info(f'Requesting market data for {len(qualified_contracts)} contracts...')
        for symbol, contract in qualified_contracts:
            try:
                ticker = ib.reqMktData(contract)
                tickers.append((symbol, ticker))
            except Exception as e:
                logger.warning(f'Error requesting market data for {symbol}: {e}')
                failed_symbols.append(symbol)
        
        await asyncio.sleep(1)
        
        # Collect IBKR data and track which symbols got valid prices
        symbols_needing_fallback = list(failed_symbols)
        now_str = datetime.now().isoformat()
        
        for symbol, ticker in tickers:
            last_price = ticker.last if hasattr(ticker, 'last') and ticker.last else 0
            market_data[symbol] = {
                'last': last_price,
                'open': ticker.open if hasattr(ticker, 'open') and ticker.open else 0,
                'close': ticker.close if hasattr(ticker, 'close') and ticker.close else 0,
                'high': ticker.high if hasattr(ticker, 'high') and ticker.high else 0,
                'low': ticker.low if hasattr(ticker, 'low') and ticker.low else 0,
                'change': ticker.change if hasattr(ticker, 'change') and ticker.change else 0,
                'source': 'ibkr',
                'timestamp': now_str
            }
            # If IBKR returned 0 price, we need fallback
            if last_price == 0 or last_price is None:
                symbols_needing_fallback.append(symbol)
            else:
                data_sources[symbol] = 'ibkr'
            
            try:
                ib.cancelMktData(ticker.contract)
            except:
                pass
        
        # Yahoo Finance fallback for symbols with missing prices
        if symbols_needing_fallback:
            unique_fallback = list(set(symbols_needing_fallback))
            logger.info(f'Attempting Yahoo Finance fallback for {len(unique_fallback)} symbols: {", ".join(unique_fallback[:10])}{"..." if len(unique_fallback) > 10 else ""}')
            yahoo_data = fetch_yahoo_prices(unique_fallback)
            for symbol, ydata in yahoo_data.items():
                market_data[symbol] = ydata
                data_sources[symbol] = 'yahoo'
        
        # Cache fallback for any remaining symbols with no price
        cache = load_price_cache()
        for symbol in stock_symbols:
            if symbol not in market_data or market_data[symbol].get('last', 0) == 0:
                cached = get_cached_price(symbol, cache)
                if cached and cached.get('last', 0) > 0:
                    market_data[symbol] = {**cached, 'source': 'cached'}
                    data_sources[symbol] = 'cached'
                    logger.debug(f'Using cached price for {symbol}: ${cached["last"]:.2f} ({format_data_age(cached.get("timestamp"))})')
        
        # Save fresh data to cache
        save_price_cache(market_data)
        
        # Add source tracking to market_data
        for symbol in market_data:
            if 'source' not in market_data[symbol]:
                market_data[symbol]['source'] = data_sources.get(symbol, 'ibkr')
            if 'timestamp' not in market_data[symbol]:
                market_data[symbol]['timestamp'] = now_str
        
        stock_positions = {}
        option_positions = {}
        watchlist_updated = False
        
        for position in positions:
            contract = position.contract
            symbol = contract.symbol
            
            # Only add stock/option symbols to watchlist, not bonds
            if contract.secType in ('STK', 'OPT') and not is_cusip(symbol):
                if symbol not in WATCHLIST:
                    WATCHLIST.append(symbol)
                    watchlist_updated = True
            
            if contract.secType == 'STK':
                stock_positions[symbol] = {
                    'symbol': symbol,
                    'shares': position.position,
                    'avgCost': float(position.avgCost) if hasattr(position, 'avgCost') else 0,
                    'marketPrice': market_data.get(symbol, {}).get('last', 0),
                }
            elif contract.secType == 'OPT':
                option_positions.setdefault(symbol, []).append({
                    'symbol': symbol,
                    'right': contract.right,
                    'position': position.position,
                })
        
        if watchlist_updated:
            save_watchlist(WATCHLIST)
        
        basic_data = {
            'positions': [],
            'incomplete_lots': [],
            'watchlist': [s for s in WATCHLIST if s not in stock_positions and not is_cusip(s)],
            'market_data': market_data,
            'data_sources': data_sources,
            'connection_source': 'ibkr'
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
        
        ib.disconnect()
        logger.info('Disconnected from IBKR')
        
        return basic_data
        
    except Exception as e:
        logger.error(f'Error getting IBKR data: {e}')
        raise

async def enhance_with_market_data(basic_data):
    """Process market data into final format with source metadata"""
    try:
        market_data = basic_data['market_data']
        data_sources = basic_data.get('data_sources', {})
        connection_source = basic_data.get('connection_source', 'ibkr')
        
        def safe_divide(a, b):
            try:
                if b == 0 or a == 0:
                    return 0
                result = a / b
                return 0 if math.isnan(result) or math.isinf(result) else result
            except:
                return 0

        def safe_number(value):
            if value is None:
                return 0
            try:
                float_val = float(value)
                if math.isnan(float_val) or math.isinf(float_val):
                    return 0
                return float_val
            except:
                return 0

        def process_market_data(symbol, mkt_data):
            current_price = safe_number(mkt_data.get('last', 0))
            daily_change = safe_number(mkt_data.get('change', 0))
            close_price = safe_number(mkt_data.get('close', current_price))
            open_price = safe_number(mkt_data.get('open', current_price))
            
            base_price = current_price - daily_change if current_price != daily_change else current_price
            daily_change_pct = safe_divide(daily_change, base_price)
            opening_gap = safe_number(open_price - close_price if close_price else 0)
            
            # Source metadata
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
            mkt_data = market_data.get(symbol, {})
            data = process_market_data(symbol, mkt_data)
            
            enhanced_positions.append([
                symbol,                                        # 0
                safe_number(pos['shares']),                    # 1
                data['current_price'],                         # 2
                safe_number(pos['avgCost']),                   # 3
                data['daily_change'],                          # 4
                data['daily_change_pct'],                      # 5
                data['close_price'],                           # 6
                data['open_price'],                            # 7
                data['opening_gap'],                           # 8
                safe_number(pos['naked_puts']),                # 9
                safe_number(pos['covered_calls']),             # 10
                safe_number(pos['uncovered_calls']),           # 11
                safe_number(calculate_shares_available(        # 12
                    pos['shares'], 
                    pos['naked_puts'], 
                    pos['covered_calls'], 
                    pos['uncovered_calls']
                )),
                data['source'],                                # 13
                data['data_age']                               # 14
            ])
        
        enhanced_incomplete = []
        for lot in basic_data['incomplete_lots']:
            symbol = lot['symbol']
            mkt_data = market_data.get(symbol, {})
            data = process_market_data(symbol, mkt_data)
            
            enhanced_incomplete.append([
                symbol,                                        # 0
                safe_number(lot['shares']),                    # 1
                data['current_price'],                         # 2
                safe_number(lot['avgCost']),                   # 3
                data['daily_change'],                          # 4
                data['daily_change_pct'],                      # 5
                data['close_price'],                           # 6
                data['open_price'],                            # 7
                data['opening_gap'],                           # 8
                data['source'],                                # 9
                data['data_age']                               # 10
            ])
        
        enhanced_watchlist = []
        for symbol in basic_data['watchlist']:
            mkt_data = market_data.get(symbol, {})
            data = process_market_data(symbol, mkt_data)
            
            enhanced_watchlist.append([
                symbol,                                        # 0
                data['current_price'],                         # 1
                data['daily_change'],                          # 2
                data['daily_change_pct'],                      # 3
                data['close_price'],                           # 4
                data['open_price'],                            # 5
                data['opening_gap'],                           # 6
                data['source'],                                # 7
                data['data_age']                               # 8
            ])
        
        return {
            'positions': enhanced_positions,
            'incomplete_lots': enhanced_incomplete,
            'watchlist': enhanced_watchlist,
            'connection_source': connection_source
        }
        
    except Exception as e:
        logger.error(f'Error formatting market data: {e}')
        raise

# ============================================
# Flask Routes
# ============================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
@async_route
async def get_data():
    logger.info('API /api/data called - starting data fetch')
    try:
        logger.info('Fetching IBKR data...')
        ibkr_data = await get_ibkr_data()
        logger.info('Enhancing with market data...')
        enhanced_data = await enhance_with_market_data(ibkr_data)
        logger.info(f'Data fetch complete: {len(enhanced_data.get("positions", []))} positions')
        return jsonify(enhanced_data)
    except Exception as e:
        logger.error(f'Error in get_data: {str(e)}', exc_info=True)
        
        # Try to serve cached data with Yahoo Finance updates when IBKR is down
        try:
            logger.info('IBKR unavailable, attempting fallback with Yahoo Finance + cache...')
            cache = load_price_cache()
            cached_prices = cache.get('prices', {})
            
            if cached_prices:
                # We have cached data - try to refresh via Yahoo, then serve
                all_symbols = list(cached_prices.keys())
                yahoo_data = fetch_yahoo_prices(all_symbols)
                
                # Merge: prefer Yahoo fresh data, fall back to cache
                market_data = {}
                data_sources = {}
                for symbol in all_symbols:
                    if symbol in yahoo_data:
                        market_data[symbol] = yahoo_data[symbol]
                        data_sources[symbol] = 'yahoo'
                    elif symbol in cached_prices and cached_prices[symbol].get('last', 0) > 0:
                        market_data[symbol] = {**cached_prices[symbol], 'source': 'cached'}
                        data_sources[symbol] = 'cached'
                
                # Save any fresh Yahoo data to cache
                if yahoo_data:
                    save_price_cache(yahoo_data)
                
                # Reconstruct basic_data from cache (we don't have positions from IBKR)
                # We can only serve watchlist-style data for all cached symbols
                fallback_data = {
                    'positions': [],
                    'incomplete_lots': [],
                    'watchlist': all_symbols,
                    'market_data': market_data,
                    'data_sources': data_sources,
                    'connection_source': 'yahoo' if yahoo_data else 'cached'
                }
                
                enhanced_data = await enhance_with_market_data(fallback_data)
                cache_age = format_data_age(cache.get('last_updated', ''))
                logger.info(f'Serving fallback data: {len(enhanced_data.get("watchlist", []))} symbols (cache age: {cache_age})')
                
                enhanced_data['fallback'] = True
                enhanced_data['fallback_message'] = f'IBKR disconnected. Showing {"Yahoo Finance" if yahoo_data else "cached"} data ({cache_age}).'
                return jsonify(enhanced_data)
        except Exception as fallback_error:
            logger.error(f'Fallback also failed: {fallback_error}')
        
        # If everything fails, return error
        friendly_error = get_friendly_error(str(e))
        return jsonify({
            'error': friendly_error,
            'technical_error': str(e),
            'positions': [],
            'incomplete_lots': [],
            'watchlist': [],
            'connection_source': 'unavailable'
        }), 500

@app.route('/api/test')
def api_test():
    """Simple test endpoint to verify server is responding"""
    logger.info('API /api/test called')
    return jsonify({'status': 'ok', 'message': 'Server is running'})

@app.route('/api/status')
def get_status():
    return jsonify({
        'app_name': APP_NAME,
        'version': APP_VERSION,
        'market_open': is_market_open(),
        'resources_dir': RESOURCES_DIR,
        'using_external_resources': RESOURCES_DIR == EXTERNAL_RESOURCES
    })

@app.route('/api/version')
def get_version():
    return jsonify(get_version_info())

@app.route('/api/update/check')
def api_check_updates():
    """API endpoint to check for updates"""
    update_info = check_for_updates()
    return jsonify(update_info)

@app.route('/api/update/download')
def api_download_update():
    """API endpoint to download and install update"""
    global pending_update
    
    if not pending_update or not pending_update.get('download_url'):
        return jsonify({'success': False, 'error': 'No update available'})
    
    try:
        # Download the update
        download_path = download_update(
            pending_update['download_url'],
            pending_update['asset_name']
        )
        
        if download_path:
            # Install it
            install_update(download_path)
            return jsonify({'success': True, 'message': 'Update installing...'})
        else:
            return jsonify({'success': False, 'error': 'Download failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ============================================
# Server Management
# ============================================
def run_server(port):
    """Run the Flask server"""
    from waitress import serve
    logger.info(f'Starting server on port {port}...')
    serve(app, host='127.0.0.1', port=port, threads=4)

def start_server(port):
    """Start the server in a background thread"""
    global server_thread
    server_thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    server_thread.start()
    logger.info(f'Server thread started on port {port}')
    return server_thread

# ============================================
# Cleanup and Shutdown
# ============================================
def cleanup():
    """Clean up all resources"""
    global ib_connection, webview_window
    
    logger.info('Cleaning up resources...')
    shutdown_event.set()
    
    if ib_connection and ib_connection.isConnected():
        try:
            ib_connection.disconnect()
            logger.info('IBKR connection closed')
        except:
            pass
    
    if webview_window:
        try:
            webview_window.destroy()
        except:
            pass
    
    logger.info('Cleanup complete')

def signal_handler(signum, frame):
    """Handle termination signals"""
    logger.info(f'Received signal {signum}, shutting down...')
    cleanup()
    sys.exit(0)

# ============================================
# Native Window (pywebview)
# ============================================
def create_native_window(port):
    """Create a native window using pywebview"""
    global webview_window
    
    url = f'http://127.0.0.1:{port}'
    logger.info(f'Creating native window for URL: {url}')
    time.sleep(1)
    
    # On Windows, try to use Edge WebView2 for better compatibility
    # Falls back to other renderers if not available
    try:
        webview_window = webview.create_window(
            APP_NAME,
            url,
            width=1400,
            height=900,
            min_size=(800, 600),
            confirm_close=False,
            text_select=True
        )
        logger.info('Native window created successfully')
    except Exception as e:
        logger.error(f'Failed to create native window: {e}')
        raise
    
    def on_closed():
        logger.info('Window closed, initiating shutdown...')
        cleanup()
    
    webview_window.events.closed += on_closed
    webview.start()

def open_in_browser(port):
    """Fallback: open in default browser"""
    url = f'http://127.0.0.1:{port}'
    time.sleep(1)
    webbrowser.open(url)
    
    try:
        print(f"\n{'='*50}")
        print(f"TTC Positions Report is running at: {url}")
        print(f"Press Ctrl+C to stop the server")
        print(f"{'='*50}\n")
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass

# ============================================
# Resource Creation
# ============================================
def ensure_resources():
    """Ensure all required resource files exist"""
    global RESOURCES_DIR, template_dir, static_dir
    
    # Create resources directory structure
    os.makedirs(os.path.join(RESOURCES_DIR, 'templates'), exist_ok=True)
    os.makedirs(os.path.join(RESOURCES_DIR, 'static', 'css'), exist_ok=True)
    os.makedirs(os.path.join(RESOURCES_DIR, 'static', 'js'), exist_ok=True)
    
    template_dir = os.path.join(RESOURCES_DIR, 'templates')
    static_dir = os.path.join(RESOURCES_DIR, 'static')
    
    # Update Flask paths
    app.template_folder = template_dir
    app.static_folder = static_dir
    
    # Check if resources need to be created or updated
    index_path = os.path.join(template_dir, 'index.html')
    css_path = os.path.join(static_dir, 'css', 'styles.css')
    js_path = os.path.join(static_dir, 'js', 'script.js')
    
    # Check version marker to detect when resources need regeneration
    version_marker = os.path.join(RESOURCES_DIR, '.version')
    needs_update = True
    if os.path.exists(version_marker):
        try:
            with open(version_marker, 'r') as f:
                if f.read().strip() == APP_VERSION:
                    needs_update = False
        except:
            pass
    
    if needs_update or not os.path.exists(index_path):
        create_html_template(index_path)
    
    if needs_update or not os.path.exists(css_path):
        create_css_file(css_path)
    
    if needs_update or not os.path.exists(js_path):
        create_js_file(js_path)
    
    # Write version marker
    if needs_update:
        try:
            with open(version_marker, 'w') as f:
                f.write(APP_VERSION)
            logger.info(f'Resources updated to v{APP_VERSION}')
        except:
            pass
    
    logger.info(f'Resources directory: {RESOURCES_DIR}')

def create_html_template(path):
    """Create the HTML template"""
    html = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TTC Positions Report</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='css/styles.css') }}">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
</head>
<body>
    <div id="toast-container"></div>
    <div id="shortcuts-modal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Keyboard Shortcuts</h3>
                <button class="modal-close" onclick="closeShortcutsModal()">&times;</button>
            </div>
            <div class="shortcuts-list">
                <div class="shortcut"><kbd>R</kbd> <span>Refresh data</span></div>
                <div class="shortcut"><kbd>/</kbd> <span>Focus search</span></div>
                <div class="shortcut"><kbd>D</kbd> <span>Toggle dark mode</span></div>
                <div class="shortcut"><kbd>C</kbd> <span>Toggle compact view</span></div>
                <div class="shortcut"><kbd>E</kbd> <span>Export to CSV</span></div>
                <div class="shortcut"><kbd>?</kbd> <span>Show shortcuts</span></div>
                <div class="shortcut"><kbd>Esc</kbd> <span>Clear search / Close modal</span></div>
            </div>
        </div>
    </div>
    <div class="container">
        <header class="header">
            <div class="header-top">
                <div class="brand">
                    <h1>TTC Positions</h1>
                    <span class="version-badge">v2.0.4</span>
                </div>
                <div class="header-actions">
                    <div class="market-status" id="marketStatus">
                        <span class="status-dot"></span>
                        <span class="status-text">Checking...</span>
                        <span class="status-countdown" id="marketCountdown"></span>
                    </div>
                    <button id="darkModeToggle" class="icon-btn" title="Toggle dark mode (D)"><i class="fas fa-moon"></i></button>
                    <button id="compactToggle" class="icon-btn" title="Toggle compact view (C)"><i class="fas fa-compress-alt"></i></button>
                    <button id="exportBtn" class="icon-btn" title="Export to CSV (E)"><i class="fas fa-download"></i></button>
                    <button id="shortcutsBtn" class="icon-btn" title="Keyboard shortcuts (?)"><i class="fas fa-keyboard"></i></button>
                </div>
            </div>
            <div class="summary-bar" id="summaryBar">
                <div class="stat-card"><span class="stat-label">Positions</span><span class="stat-value" id="statPositions">--</span></div>
                <div class="stat-card"><span class="stat-label">Daily P/L</span><span class="stat-value" id="statDailyPL">--</span></div>
                <div class="stat-card"><span class="stat-label">Gainers</span><span class="stat-value positive" id="statGainers">--</span></div>
                <div class="stat-card"><span class="stat-label">Losers</span><span class="stat-value negative" id="statLosers">--</span></div>
                <div class="stat-card"><span class="stat-label">Watchlist</span><span class="stat-value" id="statWatchlist">--</span></div>
            </div>
            <div class="controls-row">
                <div class="search-container">
                    <i class="fas fa-search search-icon"></i>
                    <input type="text" id="searchInput" placeholder="Search symbols... ( / )">
                    <button id="clearSearch" class="clear-search" title="Clear search (Esc)"><i class="fas fa-times"></i></button>
                </div>
                <div class="control-group">
                    <select id="refreshRate" class="refresh-rate" title="Auto-refresh interval">
                        <option value="0">Manual</option>
                        <option value="15">15 sec</option>
                        <option value="30">30 sec</option>
                        <option value="60" selected>1 min</option>
                        <option value="300">5 min</option>
                    </select>
                    <button id="refreshButton" class="refresh-btn" title="Refresh now (R)">
                        <i class="fa-solid fa-arrows-rotate refresh-icon"></i>
                        <span>Refresh</span>
                    </button>
                </div>
            </div>
            <div class="last-update" id="lastUpdate"><i class="far fa-clock"></i><span>Waiting for data...</span></div>
        </header>
        <div class="sections-container">
            <section class="section" id="positions-section">
                <div class="section-header" onclick="toggleSection('positions')">
                    <h2><i class="fas fa-briefcase"></i> Positions</h2>
                    <div class="section-controls"><span class="section-count" id="positions-count">0</span><i class="fas fa-chevron-down section-toggle"></i></div>
                </div>
                <div class="section-content"><div class="table-container" id="positions-table"><div class="skeleton-loader"><div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div></div></div></div>
            </section>
            <section class="section" id="incomplete-section">
                <div class="section-header" onclick="toggleSection('incomplete')">
                    <h2><i class="fas fa-puzzle-piece"></i> Incomplete Lots</h2>
                    <div class="section-controls"><span class="section-count" id="incomplete-count">0</span><i class="fas fa-chevron-down section-toggle"></i></div>
                </div>
                <div class="section-content"><div class="table-container" id="incomplete-table"><div class="skeleton-loader"><div class="skeleton-row"></div><div class="skeleton-row"></div></div></div></div>
            </section>
            <section class="section" id="watchlist-section">
                <div class="section-header" onclick="toggleSection('watchlist')">
                    <h2><i class="fas fa-eye"></i> Watchlist</h2>
                    <div class="section-controls"><span class="section-count" id="watchlist-count">0</span><i class="fas fa-chevron-down section-toggle"></i></div>
                </div>
                <div class="section-content"><div class="table-container" id="watchlist-table"><div class="skeleton-loader"><div class="skeleton-row"></div><div class="skeleton-row"></div></div></div></div>
            </section>
        </div>
        <footer class="footer">
            <span>TTC Positions Report v2.0.4</span>
            <span class="footer-sep">|</span>
            <span id="connectionStatus"><i class="fas fa-plug"></i> IBKR</span>
        </footer>
    </div>
    <script src="{{ url_for('static', filename='js/script.js') }}"></script>
</body>
</html>'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info(f'Created: {path}')

def create_css_file(path):
    """Create CSS file - can be edited without rebuilding!"""
    css = ''':root{--bg-primary:#f8fafc;--bg-secondary:#fff;--bg-tertiary:#f1f5f9;--text-primary:#1e293b;--text-secondary:#64748b;--text-muted:#94a3b8;--border-color:#e2e8f0;--border-light:#f1f5f9;--accent-primary:#3b82f6;--accent-secondary:#8b5cf6;--positive:#10b981;--positive-bg:#d1fae5;--negative:#ef4444;--negative-bg:#fee2e2;--warning:#f59e0b;--warning-bg:#fef3c7;--shadow-sm:0 1px 2px rgba(0,0,0,.05);--shadow-md:0 4px 6px -1px rgba(0,0,0,.1);--shadow-lg:0 10px 15px -3px rgba(0,0,0,.1);--radius-sm:6px;--radius-md:10px;--radius-lg:16px;--font-sans:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,sans-serif;--font-mono:'JetBrains Mono','SF Mono',monospace}[data-theme=dark]{--bg-primary:#0f172a;--bg-secondary:#1e293b;--bg-tertiary:#334155;--text-primary:#f1f5f9;--text-secondary:#94a3b8;--text-muted:#64748b;--border-color:#334155;--border-light:#1e293b;--positive:#34d399;--positive-bg:rgba(16,185,129,.15);--negative:#f87171;--negative-bg:rgba(239,68,68,.15);--warning-bg:rgba(245,158,11,.15)}*{margin:0;padding:0;box-sizing:border-box}body{font-family:var(--font-sans);background:var(--bg-primary);color:var(--text-primary);min-height:100vh;transition:background-color .3s,color .3s}body.compact table td,body.compact table th{padding:6px 8px;font-size:13px}.container{max-width:1800px;margin:0 auto;padding:16px 20px}.header{background:var(--bg-secondary);border-radius:var(--radius-lg);padding:20px 24px;margin-bottom:20px;box-shadow:var(--shadow-md);border:1px solid var(--border-color)}.header-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}.brand{display:flex;align-items:center;gap:12px}.brand h1{font-size:1.75rem;font-weight:700;background:linear-gradient(135deg,var(--accent-primary),var(--accent-secondary));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}.version-badge{background:var(--bg-tertiary);color:var(--text-secondary);padding:4px 10px;border-radius:20px;font-size:12px;font-weight:500;font-family:var(--font-mono)}.header-actions{display:flex;align-items:center;gap:12px}.market-status{display:flex;align-items:center;gap:8px;padding:8px 14px;background:var(--bg-tertiary);border-radius:var(--radius-md);font-size:13px;font-weight:500}.market-status.open .status-dot{background:var(--positive);box-shadow:0 0 8px var(--positive)}.market-status.closed .status-dot{background:var(--negative)}.status-dot{width:8px;height:8px;border-radius:50%;background:var(--text-muted);animation:pulse 2s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}.status-countdown{font-family:var(--font-mono);color:var(--text-muted);font-size:12px}.icon-btn{width:40px;height:40px;display:flex;align-items:center;justify-content:center;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:var(--radius-md);color:var(--text-secondary);cursor:pointer;transition:all .2s}.icon-btn:hover{background:var(--accent-primary);color:#fff;border-color:var(--accent-primary);transform:translateY(-1px)}.summary-bar{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}.stat-card{background:var(--bg-tertiary);border-radius:var(--radius-md);padding:12px 16px;text-align:center;border:1px solid var(--border-color)}.stat-label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);margin-bottom:4px}.stat-value{font-size:1.25rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary)}.stat-value.positive{color:var(--positive)}.stat-value.negative{color:var(--negative)}.controls-row{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:12px}.search-container{position:relative;flex:1;max-width:400px}.search-container .search-icon{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:14px}#searchInput{width:100%;padding:10px 40px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:var(--radius-md);font-size:14px;color:var(--text-primary);transition:all .2s}#searchInput:focus{outline:0;border-color:var(--accent-primary);box-shadow:0 0 0 3px rgba(59,130,246,.15)}#searchInput::placeholder{color:var(--text-muted)}.clear-search{position:absolute;right:10px;top:50%;transform:translateY(-50%);background:0 0;border:none;color:var(--text-muted);cursor:pointer;padding:4px;display:none}#searchInput:not(:placeholder-shown)+.clear-search{display:block}.control-group{display:flex;align-items:center;gap:10px}.refresh-rate{padding:10px 14px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:var(--radius-md);font-size:13px;color:var(--text-primary);cursor:pointer}.refresh-btn{display:flex;align-items:center;gap:8px;padding:10px 18px;background:linear-gradient(135deg,var(--accent-primary),var(--accent-secondary));border:none;border-radius:var(--radius-md);color:#fff;font-weight:600;font-size:14px;cursor:pointer;transition:all .2s}.refresh-btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(59,130,246,.4)}.refresh-icon.refreshing{animation:spin 1s linear infinite}@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}.last-update{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-muted)}.section{background:var(--bg-secondary);border-radius:var(--radius-lg);margin-bottom:16px;box-shadow:var(--shadow-md);border:1px solid var(--border-color);overflow:hidden}.section-header{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;background:var(--bg-tertiary);cursor:pointer;user-select:none;transition:background .2s}.section-header:hover{background:var(--border-color)}.section-header h2{display:flex;align-items:center;gap:10px;font-size:1rem;font-weight:600;color:var(--text-primary)}.section-header h2 i{color:var(--accent-primary)}.section-controls{display:flex;align-items:center;gap:10px}.section-count{background:var(--accent-primary);color:#fff;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;font-family:var(--font-mono)}.section-toggle{color:var(--text-muted);transition:transform .3s}.section.collapsed .section-toggle{transform:rotate(-90deg)}.section.collapsed .section-content{display:none}.section-content{padding:0}.table-container{overflow-x:auto}table{width:100%;border-collapse:collapse}thead{position:sticky;top:0;z-index:10}th{padding:12px 14px;background:var(--bg-tertiary);color:var(--text-secondary);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px;text-align:right;border-bottom:2px solid var(--border-color);white-space:nowrap;position:relative}th:first-child{text-align:left;padding-left:20px}th.sortable{cursor:pointer;padding-right:28px}th.sortable:hover{color:var(--accent-primary)}th.sortable::after{content:'↕';position:absolute;right:10px;top:50%;transform:translateY(-50%);font-size:10px;color:var(--text-muted)}th.sortable.asc::after{content:'↑';color:var(--accent-primary)}th.sortable.desc::after{content:'↓';color:var(--accent-primary)}td{padding:14px;text-align:right;border-bottom:1px solid var(--border-light);font-size:14px;font-family:var(--font-mono)}td:first-child{text-align:left;padding-left:20px;font-weight:600;font-family:var(--font-sans)}tr{transition:background .15s}tr:hover{background:var(--bg-tertiary)}.symbol-link{color:var(--text-primary);text-decoration:none;display:inline-flex;align-items:center;gap:6px;transition:color .2s}.symbol-link:hover{color:var(--accent-primary)}.symbol-link .external-icon{opacity:0;font-size:10px;transition:opacity .2s}.symbol-link:hover .external-icon{opacity:1}.positive{color:var(--positive)!important}.negative{color:var(--negative)!important}.zero-shares{color:var(--negative);font-weight:700}.naked-puts{background:var(--warning-bg);color:var(--warning);font-weight:600;border-radius:4px;padding:4px 8px}.covered-calls{background:var(--positive-bg);color:var(--positive);font-weight:600;border-radius:4px;padding:4px 8px}.uncovered-calls{background:var(--negative-bg);color:var(--negative);font-weight:600;border-radius:4px;padding:4px 8px}.shares-available{background:var(--warning-bg);border-radius:4px;padding:4px 8px}.shares-negative{background:var(--negative-bg);color:var(--negative);font-weight:600;border-radius:4px;padding:4px 8px}tr.hidden{display:none}.no-results{padding:40px;text-align:center;color:var(--text-muted)}.skeleton-loader{padding:20px}.skeleton-row{height:48px;background:linear-gradient(90deg,var(--bg-tertiary) 25%,var(--border-color) 50%,var(--bg-tertiary) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:var(--radius-sm);margin-bottom:8px}@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}#toast-container{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px}.toast{display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:var(--radius-md);box-shadow:var(--shadow-lg);animation:slideIn .3s ease;min-width:280px}.toast.success{border-left:4px solid var(--positive)}.toast.error{border-left:4px solid var(--negative)}.toast.info{border-left:4px solid var(--accent-primary)}.toast-icon{font-size:18px}.toast.success .toast-icon{color:var(--positive)}.toast.error .toast-icon{color:var(--negative)}.toast.info .toast-icon{color:var(--accent-primary)}.toast-message{flex:1;font-size:14px;color:var(--text-primary)}.toast-close{background:0 0;border:none;color:var(--text-muted);cursor:pointer;padding:4px}@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}@keyframes slideOut{from{transform:translateX(0);opacity:1}to{transform:translateX(100%);opacity:0}}.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);backdrop-filter:blur(4px);z-index:10000;align-items:center;justify-content:center}.modal.active{display:flex}.modal-content{background:var(--bg-secondary);border-radius:var(--radius-lg);padding:24px;min-width:360px;box-shadow:var(--shadow-lg);border:1px solid var(--border-color)}.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.modal-header h3{font-size:1.25rem;font-weight:600}.modal-close{background:0 0;border:none;font-size:24px;color:var(--text-muted);cursor:pointer}.shortcuts-list{display:flex;flex-direction:column;gap:12px}.shortcut{display:flex;align-items:center;gap:16px}kbd{display:inline-flex;align-items:center;justify-content:center;min-width:32px;padding:4px 10px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--text-primary)}.footer{display:flex;align-items:center;justify-content:center;gap:12px;padding:16px;color:var(--text-muted);font-size:12px}.footer-sep{color:var(--border-color)}#connectionStatus{display:flex;align-items:center;gap:6px}#connectionStatus.connected{color:var(--positive)}#connectionStatus.disconnected{color:var(--negative)}#connectionStatus.fallback{color:var(--warning)}.price-cell{display:inline-flex;align-items:center;gap:6px}.source-dot{display:inline-block;width:7px;height:7px;border-radius:50%;flex-shrink:0;position:relative;cursor:help}.source-dot.ibkr{background:var(--positive);box-shadow:0 0 4px var(--positive)}.source-dot.yahoo{background:var(--warning);box-shadow:0 0 4px var(--warning)}.source-dot.cached{background:var(--text-muted);box-shadow:0 0 4px var(--text-muted)}.source-dot.unavailable{background:var(--negative);box-shadow:0 0 4px var(--negative)}.price-stale{opacity:.7;font-style:italic}.price-tooltip{position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:var(--radius-sm);padding:8px 12px;font-size:11px;font-family:var(--font-sans);font-style:normal;white-space:nowrap;box-shadow:var(--shadow-lg);z-index:100;pointer-events:none;opacity:0;transition:opacity .15s}.source-dot:hover .price-tooltip{opacity:1}.tooltip-source{font-weight:600;margin-bottom:2px}.tooltip-age{color:var(--text-muted)}.fallback-banner{background:linear-gradient(135deg,var(--warning),#d97706);color:#fff;padding:10px 20px;text-align:center;font-size:13px;font-weight:500;border-radius:var(--radius-md);margin-bottom:12px;display:flex;align-items:center;justify-content:center;gap:8px}@media (max-width:1024px){.summary-bar{grid-template-columns:repeat(3,1fr)}}@media (max-width:768px){.header-top{flex-direction:column;gap:16px}.summary-bar{grid-template-columns:repeat(2,1fr)}.controls-row{flex-direction:column}.search-container{max-width:100%}}'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(css)
    logger.info(f'Created: {path}')

def create_js_file(path):
    """Create JavaScript file - can be edited without rebuilding!"""
    # Using readable JS for debugging - will help identify issues
    js = '''
// TTC Positions Report - Frontend JavaScript
// Version 2.0.4

let isRefreshing = false;
let currentSort = { column: null, direction: "asc" };
let refreshInterval;
let cachedData = null;
let marketStatusInterval;
const PREFS_KEY = "ttc_positions_prefs";

// Debug logging
function log(msg) {
    console.log("[TTC] " + msg);
}

function loadPreferences() {
    try {
        return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    } catch (e) {
        return {};
    }
}

function savePreferences(prefs) {
    try {
        localStorage.setItem(PREFS_KEY, JSON.stringify({ ...loadPreferences(), ...prefs }));
    } catch (e) {
        log("Failed to save preferences: " + e);
    }
}

function applyPreferences() {
    const prefs = loadPreferences();
    if (prefs.darkMode) {
        document.documentElement.setAttribute("data-theme", "dark");
        document.getElementById("darkModeToggle").innerHTML = '<i class="fas fa-sun"></i>';
    }
    if (prefs.compactView) {
        document.body.classList.add("compact");
    }
    if (prefs.refreshRate !== undefined) {
        document.getElementById("refreshRate").value = prefs.refreshRate;
    }
    if (prefs.collapsedSections) {
        prefs.collapsedSections.forEach(section => {
            const el = document.getElementById(section + "-section");
            if (el) el.classList.add("collapsed");
        });
    }
}

function showToast(message, type = "info", duration = 3000) {
    const container = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = "toast " + type;
    const icons = { success: "fa-check-circle", error: "fa-exclamation-circle", info: "fa-info-circle" };
    toast.innerHTML = '<i class="fas ' + icons[type] + ' toast-icon"></i><span class="toast-message">' + message + '</span><button class="toast-close" onclick="this.parentElement.remove()"><i class="fas fa-times"></i></button>';
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = "slideOut 0.3s ease forwards";
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

function updateMarketStatus() {
    const now = new Date();
    const eastern = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
    const day = eastern.getDay();
    const hours = eastern.getHours();
    const minutes = eastern.getMinutes();
    const totalMinutes = hours * 60 + minutes;
    const marketOpen = 570; // 9:30 AM
    const marketClose = 960; // 4:00 PM
    
    const statusEl = document.getElementById("marketStatus");
    const countdownEl = document.getElementById("marketCountdown");
    const textEl = statusEl.querySelector(".status-text");
    
    const isWeekend = day === 0 || day === 6;
    const isOpen = !isWeekend && totalMinutes >= marketOpen && totalMinutes < marketClose;
    
    statusEl.classList.remove("open", "closed");
    statusEl.classList.add(isOpen ? "open" : "closed");
    
    if (isOpen) {
        textEl.textContent = "Market Open";
        const remaining = marketClose - totalMinutes;
        countdownEl.textContent = "Closes in " + Math.floor(remaining / 60) + "h " + (remaining % 60) + "m";
    } else {
        textEl.textContent = "Market Closed";
        let minutesUntilOpen;
        if (isWeekend) {
            minutesUntilOpen = (day === 0 ? 1 : 2) * 24 * 60 + marketOpen - totalMinutes;
        } else if (totalMinutes < marketOpen) {
            minutesUntilOpen = marketOpen - totalMinutes;
        } else {
            minutesUntilOpen = 1440 - totalMinutes + marketOpen;
        }
        const hoursUntil = Math.floor(minutesUntilOpen / 60);
        countdownEl.textContent = hoursUntil > 24 
            ? "Opens in " + Math.floor(hoursUntil / 24) + "d " + (hoursUntil % 24) + "h"
            : "Opens in " + hoursUntil + "h " + (minutesUntilOpen % 60) + "m";
    }
}

function toggleDarkMode() {
    const html = document.documentElement;
    const isDark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", isDark ? "light" : "dark");
    document.getElementById("darkModeToggle").innerHTML = isDark ? '<i class="fas fa-moon"></i>' : '<i class="fas fa-sun"></i>';
    savePreferences({ darkMode: !isDark });
    showToast((isDark ? "Light" : "Dark") + " mode enabled", "info", 1500);
}

function toggleCompactView() {
    const isCompact = document.body.classList.toggle("compact");
    savePreferences({ compactView: isCompact });
    showToast((isCompact ? "Compact" : "Normal") + " view enabled", "info", 1500);
}

function toggleSection(section) {
    const el = document.getElementById(section + "-section");
    el.classList.toggle("collapsed");
    const prefs = loadPreferences();
    const collapsed = prefs.collapsedSections || [];
    if (el.classList.contains("collapsed")) {
        if (!collapsed.includes(section)) collapsed.push(section);
    } else {
        const idx = collapsed.indexOf(section);
        if (idx > -1) collapsed.splice(idx, 1);
    }
    savePreferences({ collapsedSections: collapsed });
}

function openShortcutsModal() {
    document.getElementById("shortcuts-modal").classList.add("active");
}

function closeShortcutsModal() {
    document.getElementById("shortcuts-modal").classList.remove("active");
}

function exportToCSV() {
    if (!cachedData) {
        showToast("No data to export", "error");
        return;
    }
    let csv = ["Symbol","Shares","Current Price","Avg Price","Daily Change $","Daily Change %","Last Price","Open","OGap","NP","CC","UC","Shares Available","Data Source"].join(",") + "\\n";
    cachedData.positions.forEach(row => {
        csv += row.slice(0, 14).map((val, i) => i === 5 ? (val * 100).toFixed(2) + "%" : (typeof val === "number" ? val.toFixed(2) : val)).join(",") + "\\n";
    });
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "ttc_positions_" + new Date().toISOString().split("T")[0] + ".csv";
    a.click();
    showToast("Exported to CSV", "success");
}

function formatNumber(value, column) {
    if (value === "" || value === null || value === undefined) return "";
    const num = parseFloat(value);
    if (isNaN(num)) return value;
    switch (column) {
        case "Current Price":
        case "Last Price":
        case "Open":
        case "Avg Price":
            return "$" + num.toFixed(2);
        case "Daily Change $":
        case "OGap":
            return (num >= 0 ? "+" : "") + "$" + num.toFixed(2);
        case "Daily Change %":
            return (num >= 0 ? "+" : "") + (num * 100).toFixed(2) + "%";
        default:
            return num.toLocaleString();
    }
}

function createSymbolLink(symbol) {
    const a = document.createElement("a");
    a.href = "https://www.tradingview.com/symbols/" + symbol + "/";
    a.target = "_blank";
    a.className = "symbol-link";
    a.innerHTML = symbol + ' <i class="fas fa-external-link-alt external-icon"></i>';
    return a;
}

function getSourceInfo(row, section) {
    // Extract source and data_age from the row based on section type
    // positions: source at [13], data_age at [14]
    // incomplete: source at [9], data_age at [10]
    // watchlist: source at [7], data_age at [8]
    let source = "ibkr", dataAge = "";
    if (section === "positions") {
        source = row[13] || "ibkr";
        dataAge = row[14] || "";
    } else if (section === "incomplete") {
        source = row[9] || "ibkr";
        dataAge = row[10] || "";
    } else if (section === "watchlist") {
        source = row[7] || "ibkr";
        dataAge = row[8] || "";
    }
    return { source, dataAge };
}

function createSourceDot(source, dataAge) {
    const dot = document.createElement("span");
    dot.className = "source-dot " + source;
    
    const sourceLabels = {
        ibkr: "IBKR Live",
        yahoo: "Yahoo Finance",
        cached: "Cached Data",
        unavailable: "Unavailable"
    };
    
    const tooltip = document.createElement("span");
    tooltip.className = "price-tooltip";
    let tooltipHtml = '<div class="tooltip-source">' + (sourceLabels[source] || source) + '</div>';
    if (dataAge) {
        tooltipHtml += '<div class="tooltip-age">' + dataAge + '</div>';
    }
    tooltip.innerHTML = tooltipHtml;
    dot.appendChild(tooltip);
    
    return dot;
}

function createTable(data, headers, section) {
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    
    // Display headers exclude source metadata columns
    let sectionHeaders = headers;
    if (section === "incomplete") {
        sectionHeaders = headers.filter(h => !["NP", "CC", "UC", "Shares Available"].includes(h));
    } else if (section === "watchlist") {
        sectionHeaders = ["Underlying", "Current Price", "Daily Change $", "Daily Change %", "Last Price", "Open", "OGap"];
    }
    
    sectionHeaders.forEach((header, idx) => {
        const th = document.createElement("th");
        th.textContent = header;
        th.classList.add("sortable");
        th.addEventListener("click", () => sortTable(table, idx, header));
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    
    const tbody = document.createElement("tbody");
    data.sort((a, b) => a[0].toString().toLowerCase().localeCompare(b[0].toString().toLowerCase()));
    
    data.forEach(row => {
        const tr = document.createElement("tr");
        const { source, dataAge } = getSourceInfo(row, section);
        
        let rowData = [...row];
        if (section === "incomplete") {
            rowData = rowData.filter((_, i) => !["NP", "CC", "UC", "Shares Available"].includes(headers[i]));
        } else if (section === "watchlist") {
            rowData = rowData.slice(0, 7);
        }
        
        rowData.forEach((cell, idx) => {
            const td = document.createElement("td");
            const header = sectionHeaders[idx];
            
            if (header === "Underlying") {
                td.appendChild(createSymbolLink(cell));
            } else if (typeof cell === "number") {
                if (header === "Current Price") {
                    // Add source indicator dot for Current Price column
                    const wrapper = document.createElement("span");
                    wrapper.className = "price-cell";
                    if (source === "cached") wrapper.classList.add("price-stale");
                    
                    const priceText = document.createElement("span");
                    priceText.textContent = formatNumber(cell, header);
                    wrapper.appendChild(priceText);
                    
                    // Only show dot when source is not IBKR (non-live data)
                    if (source !== "ibkr") {
                        wrapper.appendChild(createSourceDot(source, dataAge));
                    }
                    
                    td.appendChild(wrapper);
                    
                    const changeIdx = headers.indexOf("Daily Change $");
                    if (row[changeIdx] > 0) td.classList.add("positive");
                    if (row[changeIdx] < 0) td.classList.add("negative");
                } else {
                    td.textContent = formatNumber(cell, header);
                    if (["Daily Change $", "Daily Change %", "OGap"].includes(header)) {
                        if (cell > 0) td.classList.add("positive");
                        if (cell < 0) td.classList.add("negative");
                    } else if (header === "Shares" && cell === 0) {
                        td.classList.add("zero-shares");
                    } else if (header === "NP" && cell > 0) {
                        td.classList.add("naked-puts");
                    } else if (header === "CC" && cell > 0) {
                        td.classList.add("covered-calls");
                    } else if (header === "UC" && cell > 0) {
                        td.classList.add("uncovered-calls");
                    } else if (header === "Shares Available") {
                        if (cell > 0) td.classList.add("shares-available");
                        if (cell < 0) td.classList.add("shares-negative");
                    }
                }
            } else {
                td.textContent = cell;
            }
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
}

function sortTable(table, colIndex) {
    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const th = table.querySelector("th:nth-child(" + (colIndex + 1) + ")");
    
    table.querySelectorAll("th").forEach(h => h.classList.remove("asc", "desc"));
    
    let dir = "asc";
    if (currentSort.column === colIndex) {
        dir = currentSort.direction === "asc" ? "desc" : "asc";
    }
    currentSort = { column: colIndex, direction: dir };
    th.classList.add(dir);
    
    rows.sort((a, b) => {
        const aVal = getCellValue(a, colIndex);
        const bVal = getCellValue(b, colIndex);
        if (isNaN(parseFloat(aVal)) || isNaN(parseFloat(bVal))) {
            return dir === "asc" ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
        }
        return dir === "asc" ? parseFloat(aVal) - parseFloat(bVal) : parseFloat(bVal) - parseFloat(aVal);
    });
    
    rows.forEach(row => tbody.appendChild(row));
}

function getCellValue(row, index) {
    const cell = row.querySelector("td:nth-child(" + (index + 1) + ")");
    return cell ? cell.textContent.trim().replace(/[$%+,]/g, "") : "";
}

function filterTables(searchText) {
    document.querySelectorAll("table").forEach(table => {
        const rows = table.querySelectorAll("tbody tr");
        let hasVisible = false;
        
        rows.forEach(row => {
            const symbol = row.querySelector("td:first-child")?.textContent || "";
            if (symbol.match(new RegExp(searchText, "i"))) {
                row.classList.remove("hidden");
                hasVisible = true;
            } else {
                row.classList.add("hidden");
            }
        });
        
        let noResults = table.parentElement.querySelector(".no-results");
        if (hasVisible || rows.length === 0) {
            if (noResults) noResults.style.display = "none";
        } else {
            if (!noResults) {
                noResults = document.createElement("div");
                noResults.className = "no-results";
                noResults.textContent = "No matching symbols found";
                table.parentElement.appendChild(noResults);
            }
            noResults.style.display = "block";
        }
    });
}

function clearSearch() {
    const input = document.getElementById("searchInput");
    input.value = "";
    filterTables("");
    input.focus();
}

function updateSummaryStats(data) {
    document.getElementById("statPositions").textContent = data.positions.length;
    document.getElementById("statWatchlist").textContent = data.watchlist.length;
    
    let gainers = 0, losers = 0, dailyPL = 0;
    data.positions.forEach(pos => {
        const change = pos[4];
        if (change > 0) gainers++;
        if (change < 0) losers++;
        dailyPL += change * pos[1];
    });
    
    document.getElementById("statGainers").textContent = gainers;
    document.getElementById("statLosers").textContent = losers;
    
    const plEl = document.getElementById("statDailyPL");
    plEl.textContent = (dailyPL >= 0 ? "+" : "") + "$" + dailyPL.toFixed(2);
    plEl.className = "stat-value " + (dailyPL >= 0 ? "positive" : "negative");
}

function updateSectionCounts(data) {
    document.getElementById("positions-count").textContent = data.positions.length;
    document.getElementById("incomplete-count").textContent = data.incomplete_lots.length;
    document.getElementById("watchlist-count").textContent = data.watchlist.length;
}

function updateLastUpdateTime() {
    document.getElementById("lastUpdate").innerHTML = '<i class="far fa-clock"></i> <span>Updated at ' + new Date().toLocaleTimeString() + '</span>';
}

function setLoadingState(loading) {
    const icon = document.querySelector(".refresh-icon");
    if (loading) {
        icon.classList.add("refreshing");
    } else {
        icon.classList.remove("refreshing");
    }
}

function updateConnectionStatus(data) {
    const statusEl = document.getElementById("connectionStatus");
    const source = data.connection_source || "ibkr";
    
    // Remove any existing fallback banner
    const existingBanner = document.querySelector(".fallback-banner");
    if (existingBanner) existingBanner.remove();
    
    if (data.fallback) {
        // Show fallback banner
        const banner = document.createElement("div");
        banner.className = "fallback-banner";
        banner.innerHTML = '<i class="fas fa-exclamation-triangle"></i> ' + (data.fallback_message || "Using fallback data");
        const header = document.querySelector(".header");
        header.parentElement.insertBefore(banner, header.nextSibling);
        
        statusEl.className = "fallback";
        statusEl.innerHTML = '<i class="fas fa-plug"></i> ' + (source === "yahoo" ? "Yahoo Finance" : "Cached Data");
    } else if (source === "ibkr") {
        statusEl.className = "connected";
        statusEl.innerHTML = '<i class="fas fa-plug"></i> IBKR Connected';
    } else if (source === "yahoo") {
        statusEl.className = "fallback";
        statusEl.innerHTML = '<i class="fas fa-plug"></i> Yahoo Finance';
    } else if (source === "cached") {
        statusEl.className = "fallback";
        statusEl.innerHTML = '<i class="fas fa-plug"></i> Cached Data';
    }
}

async function updateTables() {
    log("updateTables called");
    if (isRefreshing) {
        log("Already refreshing, skipping");
        return;
    }
    
    isRefreshing = true;
    setLoadingState(true);
    log("Starting data fetch...");
    
    try {
        log("Fetching /api/data...");
        const response = await fetch("/api/data");
        log("Response status: " + response.status);
        
        const data = await response.json();
        
        // Handle error responses that may still contain fallback data
        if (!response.ok && !data.fallback) {
            throw new Error(data.error || "Server error");
        }
        
        log("Data received: " + data.positions.length + " positions, " + data.watchlist.length + " watchlist");
        cachedData = data;
        
        const headers = ["Underlying", "Shares", "Current Price", "Avg Price", "Daily Change $", "Daily Change %", "Last Price", "Open", "OGap", "NP", "CC", "UC", "Shares Available"];
        
        const positionsTable = document.getElementById("positions-table");
        const incompleteTable = document.getElementById("incomplete-table");
        const watchlistTable = document.getElementById("watchlist-table");
        
        positionsTable.innerHTML = data.positions.length > 0 ? "" : '<div class="no-results">No positions found</div>';
        incompleteTable.innerHTML = data.incomplete_lots.length > 0 ? "" : '<div class="no-results">No incomplete lots</div>';
        watchlistTable.innerHTML = data.watchlist.length > 0 ? "" : '<div class="no-results">No watchlist items</div>';
        
        if (data.positions.length > 0) positionsTable.appendChild(createTable(data.positions, headers, "positions"));
        if (data.incomplete_lots.length > 0) incompleteTable.appendChild(createTable(data.incomplete_lots, headers, "incomplete"));
        if (data.watchlist.length > 0) watchlistTable.appendChild(createTable(data.watchlist, headers, "watchlist"));
        
        updateLastUpdateTime();
        updateSummaryStats(data);
        updateSectionCounts(data);
        updateConnectionStatus(data);
        
        const searchVal = document.getElementById("searchInput").value;
        if (searchVal) filterTables(searchVal);
        
        if (data.fallback) {
            showToast(data.fallback_message || "Using fallback data", "info", 3000);
        } else {
            showToast("Data refreshed", "success", 1500);
        }
        
    } catch (error) {
        log("Error: " + error.message);
        console.error("Fetch error:", error);
        showToast(error.message, "error");
        document.getElementById("connectionStatus").className = "disconnected";
        document.getElementById("connectionStatus").innerHTML = '<i class="fas fa-plug"></i> Connection Error';
    } finally {
        isRefreshing = false;
        setLoadingState(false);
    }
}

function setRefreshRate(seconds) {
    if (refreshInterval) clearInterval(refreshInterval);
    if (seconds > 0) {
        refreshInterval = setInterval(updateTables, seconds * 1000);
    }
    savePreferences({ refreshRate: seconds });
}

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
        if (e.key === "Escape") {
            clearSearch();
            e.target.blur();
        }
        return;
    }
    
    switch (e.key.toLowerCase()) {
        case "r":
            e.preventDefault();
            updateTables();
            break;
        case "/":
            e.preventDefault();
            document.getElementById("searchInput").focus();
            break;
        case "d":
            e.preventDefault();
            toggleDarkMode();
            break;
        case "c":
            e.preventDefault();
            toggleCompactView();
            break;
        case "e":
            e.preventDefault();
            exportToCSV();
            break;
        case "?":
            e.preventDefault();
            openShortcutsModal();
            break;
        case "escape":
            closeShortcutsModal();
            break;
    }
});

// Initialize on DOM ready
document.addEventListener("DOMContentLoaded", () => {
    log("DOM loaded, initializing...");
    
    applyPreferences();
    
    document.getElementById("darkModeToggle").addEventListener("click", toggleDarkMode);
    document.getElementById("compactToggle").addEventListener("click", toggleCompactView);
    document.getElementById("exportBtn").addEventListener("click", exportToCSV);
    document.getElementById("shortcutsBtn").addEventListener("click", openShortcutsModal);
    document.getElementById("refreshButton").addEventListener("click", () => {
        log("Refresh button clicked");
        updateTables();
    });
    document.getElementById("refreshRate").addEventListener("change", (e) => setRefreshRate(parseInt(e.target.value)));
    document.getElementById("searchInput").addEventListener("input", (e) => filterTables(e.target.value));
    document.getElementById("clearSearch").addEventListener("click", clearSearch);
    document.getElementById("shortcuts-modal").addEventListener("click", (e) => {
        if (e.target === document.getElementById("shortcuts-modal")) closeShortcutsModal();
    });
    
    updateMarketStatus();
    marketStatusInterval = setInterval(updateMarketStatus, 60000);
    
    log("Calling initial updateTables...");
    updateTables();
    
    setRefreshRate(parseInt(document.getElementById("refreshRate").value));
    
    // Check for updates after a delay
    setTimeout(checkForUpdates, 2000);
});

// Update notification functions
function showUpdateNotification(version, notes) {
    const banner = document.createElement("div");
    banner.id = "update-banner";
    banner.style.cssText = "position:fixed;top:0;left:0;right:0;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;padding:12px 20px;display:flex;align-items:center;justify-content:center;gap:16px;z-index:10001;font-family:var(--font-sans);box-shadow:0 4px 12px rgba(0,0,0,0.15);";
    banner.innerHTML = '<i class="fas fa-gift" style="font-size:20px"></i><span><strong>Update Available!</strong> Version ' + version + ' is ready.</span><button onclick="installUpdate()" style="background:white;color:#3b82f6;border:none;padding:8px 16px;border-radius:6px;font-weight:600;cursor:pointer">Update Now</button><button onclick="dismissUpdate()" style="background:transparent;color:white;border:1px solid rgba(255,255,255,0.5);padding:8px 12px;border-radius:6px;cursor:pointer">Later</button>';
    document.body.prepend(banner);
    document.querySelector(".container").style.marginTop = "60px";
}

function dismissUpdate() {
    const banner = document.getElementById("update-banner");
    if (banner) banner.remove();
    document.querySelector(".container").style.marginTop = "";
}

async function installUpdate() {
    showToast("Downloading update...", "info", 5000);
    try {
        const response = await fetch("/api/update/download");
        const result = await response.json();
        if (result.success) {
            showToast("Installing update... The app will restart.", "success", 10000);
        } else {
            showToast("Update failed: " + result.error, "error");
        }
    } catch (error) {
        showToast("Update failed: " + error.message, "error");
    }
}

async function checkForUpdates() {
    try {
        log("Checking for updates...");
        const response = await fetch("/api/update/check");
        const result = await response.json();
        if (result.available) {
            log("Update available: " + result.latest_version);
            showUpdateNotification(result.latest_version, result.release_notes || "");
        } else {
            log("No updates available");
        }
    } catch (error) {
        log("Could not check for updates: " + error);
    }
}
'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(js)
    logger.info(f'Created: {path}')

# ============================================
# Main Entry Point
# ============================================
def main():
    """Main entry point for the application"""
    logger.info(f'Starting {APP_NAME} v{APP_VERSION}')
    logger.info(f'App directory: {APP_DIR}')
    logger.info(f'Resources directory: {RESOURCES_DIR}')
    logger.info(f'Platform: {platform.system()} {platform.release()}')
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Register cleanup
    atexit.register(cleanup)
    
    # Ensure resources exist
    ensure_resources()
    
    # Find available port
    try:
        port = find_available_port()
        logger.info(f'Using port {port}')
    except OSError as e:
        logger.error(f'Could not find available port: {e}')
        sys.exit(1)
    
    # Start server
    start_server(port)
    
    # Start background update check (non-blocking)
    update_thread = threading.Thread(target=check_updates_background, daemon=True)
    update_thread.start()
    
    # Start UI
    if HAS_WEBVIEW:
        logger.info('Starting native window...')
        create_native_window(port)
    else:
        logger.info('Opening in browser...')
        open_in_browser(port)
    
    logger.info('Application shutting down')
    cleanup()

if __name__ == '__main__':
    main()
