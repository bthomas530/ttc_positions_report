# TTC Positions Report - Desktop Application
# v2.0.0 - User-friendly desktop app with auto-updates
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
APP_VERSION = "2.0.0"
DEFAULT_PORT = 8082
MAX_PORT_TRIES = 10

# GitHub configuration for auto-updates
# Set these to your repository details
GITHUB_OWNER = "your-username"  # Change this to your GitHub username
GITHUB_REPO = "ttc-positions"   # Change this to your repository name
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
        js_code = f'''
        if (typeof showUpdateNotification === 'function') {{
            showUpdateNotification("{update_info['latest_version']}", "{update_info.get('release_notes', '').replace('"', '\\"').replace('\\n', ' ')}");
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
# Data Functions
# ============================================
async def get_ibkr_data():
    """Get position data from IBKR"""
    global WATCHLIST
    
    try:
        logger.info('Connecting to IBKR...')
        ib = await connect_to_ib()
        
        logger.info('Requesting positions...')
        positions = await ib.reqPositionsAsync()
        await asyncio.sleep(0.5)
        
        symbols = set()
        for position in positions:
            symbols.add(position.contract.symbol)
        symbols.update(WATCHLIST)
        
        market_data = {}
        tickers = []
        
        for symbol in symbols:
            contract = Stock(symbol, 'SMART', 'USD')
            ticker = ib.reqMktData(contract)
            tickers.append((symbol, ticker))
        
        await asyncio.sleep(1)
        
        for symbol, ticker in tickers:
            market_data[symbol] = {
                'last': ticker.last if hasattr(ticker, 'last') and ticker.last else 0,
                'open': ticker.open if hasattr(ticker, 'open') and ticker.open else 0,
                'close': ticker.close if hasattr(ticker, 'close') and ticker.close else 0,
                'high': ticker.high if hasattr(ticker, 'high') and ticker.high else 0,
                'low': ticker.low if hasattr(ticker, 'low') and ticker.low else 0,
                'change': ticker.change if hasattr(ticker, 'change') and ticker.change else 0,
            }
            ib.cancelMktData(ticker.contract)
        
        stock_positions = {}
        option_positions = {}
        watchlist_updated = False
        
        for position in positions:
            contract = position.contract
            symbol = contract.symbol
            
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
            'watchlist': [s for s in WATCHLIST if s not in stock_positions],
            'market_data': market_data
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
    """Process market data into final format"""
    try:
        market_data = basic_data['market_data']
        
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
            
            return {
                'current_price': current_price,
                'daily_change': daily_change,
                'daily_change_pct': daily_change_pct,
                'close_price': close_price,
                'open_price': open_price,
                'opening_gap': opening_gap
            }
        
        enhanced_positions = []
        for pos in basic_data['positions']:
            symbol = pos['symbol']
            mkt_data = market_data.get(symbol, {})
            data = process_market_data(symbol, mkt_data)
            
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
                    pos['shares'], 
                    pos['naked_puts'], 
                    pos['covered_calls'], 
                    pos['uncovered_calls']
                ))
            ])
        
        enhanced_incomplete = []
        for lot in basic_data['incomplete_lots']:
            symbol = lot['symbol']
            mkt_data = market_data.get(symbol, {})
            data = process_market_data(symbol, mkt_data)
            
            enhanced_incomplete.append([
                symbol,
                safe_number(lot['shares']),
                data['current_price'],
                safe_number(lot['avgCost']),
                data['daily_change'],
                data['daily_change_pct'],
                data['close_price'],
                data['open_price'],
                data['opening_gap']
            ])
        
        enhanced_watchlist = []
        for symbol in basic_data['watchlist']:
            mkt_data = market_data.get(symbol, {})
            data = process_market_data(symbol, mkt_data)
            
            enhanced_watchlist.append([
                symbol,
                data['current_price'],
                data['daily_change'],
                data['daily_change_pct'],
                data['close_price'],
                data['open_price'],
                data['opening_gap']
            ])
        
        return {
            'positions': enhanced_positions,
            'incomplete_lots': enhanced_incomplete,
            'watchlist': enhanced_watchlist
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
    try:
        ibkr_data = await get_ibkr_data()
        enhanced_data = await enhance_with_market_data(ibkr_data)
        return jsonify(enhanced_data)
    except Exception as e:
        logger.error(f'Error in get_data: {str(e)}', exc_info=True)
        # Return user-friendly error message
        friendly_error = get_friendly_error(str(e))
        return jsonify({
            'error': friendly_error,
            'technical_error': str(e),  # Keep for debugging
            'positions': [],
            'incomplete_lots': [],
            'watchlist': []
        }), 500

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
    time.sleep(1)
    
    webview_window = webview.create_window(
        APP_NAME,
        url,
        width=1400,
        height=900,
        min_size=(800, 600),
        confirm_close=False,
        text_select=True
    )
    
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
    
    # Check if resources need to be created
    index_path = os.path.join(template_dir, 'index.html')
    css_path = os.path.join(static_dir, 'css', 'styles.css')
    js_path = os.path.join(static_dir, 'js', 'script.js')
    
    if not os.path.exists(index_path):
        create_html_template(index_path)
    
    if not os.path.exists(css_path):
        create_css_file(css_path)
    
    if not os.path.exists(js_path):
        create_js_file(js_path)
    
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
                    <span class="version-badge">v2.0.0</span>
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
            <span>TTC Positions Report v2.0.0</span>
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
    css = ''':root{--bg-primary:#f8fafc;--bg-secondary:#fff;--bg-tertiary:#f1f5f9;--text-primary:#1e293b;--text-secondary:#64748b;--text-muted:#94a3b8;--border-color:#e2e8f0;--border-light:#f1f5f9;--accent-primary:#3b82f6;--accent-secondary:#8b5cf6;--positive:#10b981;--positive-bg:#d1fae5;--negative:#ef4444;--negative-bg:#fee2e2;--warning:#f59e0b;--warning-bg:#fef3c7;--shadow-sm:0 1px 2px rgba(0,0,0,.05);--shadow-md:0 4px 6px -1px rgba(0,0,0,.1);--shadow-lg:0 10px 15px -3px rgba(0,0,0,.1);--radius-sm:6px;--radius-md:10px;--radius-lg:16px;--font-sans:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,sans-serif;--font-mono:'JetBrains Mono','SF Mono',monospace}[data-theme=dark]{--bg-primary:#0f172a;--bg-secondary:#1e293b;--bg-tertiary:#334155;--text-primary:#f1f5f9;--text-secondary:#94a3b8;--text-muted:#64748b;--border-color:#334155;--border-light:#1e293b;--positive:#34d399;--positive-bg:rgba(16,185,129,.15);--negative:#f87171;--negative-bg:rgba(239,68,68,.15);--warning-bg:rgba(245,158,11,.15)}*{margin:0;padding:0;box-sizing:border-box}body{font-family:var(--font-sans);background:var(--bg-primary);color:var(--text-primary);min-height:100vh;transition:background-color .3s,color .3s}body.compact table td,body.compact table th{padding:6px 8px;font-size:13px}.container{max-width:1800px;margin:0 auto;padding:16px 20px}.header{background:var(--bg-secondary);border-radius:var(--radius-lg);padding:20px 24px;margin-bottom:20px;box-shadow:var(--shadow-md);border:1px solid var(--border-color)}.header-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}.brand{display:flex;align-items:center;gap:12px}.brand h1{font-size:1.75rem;font-weight:700;background:linear-gradient(135deg,var(--accent-primary),var(--accent-secondary));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}.version-badge{background:var(--bg-tertiary);color:var(--text-secondary);padding:4px 10px;border-radius:20px;font-size:12px;font-weight:500;font-family:var(--font-mono)}.header-actions{display:flex;align-items:center;gap:12px}.market-status{display:flex;align-items:center;gap:8px;padding:8px 14px;background:var(--bg-tertiary);border-radius:var(--radius-md);font-size:13px;font-weight:500}.market-status.open .status-dot{background:var(--positive);box-shadow:0 0 8px var(--positive)}.market-status.closed .status-dot{background:var(--negative)}.status-dot{width:8px;height:8px;border-radius:50%;background:var(--text-muted);animation:pulse 2s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}.status-countdown{font-family:var(--font-mono);color:var(--text-muted);font-size:12px}.icon-btn{width:40px;height:40px;display:flex;align-items:center;justify-content:center;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:var(--radius-md);color:var(--text-secondary);cursor:pointer;transition:all .2s}.icon-btn:hover{background:var(--accent-primary);color:#fff;border-color:var(--accent-primary);transform:translateY(-1px)}.summary-bar{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}.stat-card{background:var(--bg-tertiary);border-radius:var(--radius-md);padding:12px 16px;text-align:center;border:1px solid var(--border-color)}.stat-label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);margin-bottom:4px}.stat-value{font-size:1.25rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary)}.stat-value.positive{color:var(--positive)}.stat-value.negative{color:var(--negative)}.controls-row{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:12px}.search-container{position:relative;flex:1;max-width:400px}.search-container .search-icon{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:14px}#searchInput{width:100%;padding:10px 40px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:var(--radius-md);font-size:14px;color:var(--text-primary);transition:all .2s}#searchInput:focus{outline:0;border-color:var(--accent-primary);box-shadow:0 0 0 3px rgba(59,130,246,.15)}#searchInput::placeholder{color:var(--text-muted)}.clear-search{position:absolute;right:10px;top:50%;transform:translateY(-50%);background:0 0;border:none;color:var(--text-muted);cursor:pointer;padding:4px;display:none}#searchInput:not(:placeholder-shown)+.clear-search{display:block}.control-group{display:flex;align-items:center;gap:10px}.refresh-rate{padding:10px 14px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:var(--radius-md);font-size:13px;color:var(--text-primary);cursor:pointer}.refresh-btn{display:flex;align-items:center;gap:8px;padding:10px 18px;background:linear-gradient(135deg,var(--accent-primary),var(--accent-secondary));border:none;border-radius:var(--radius-md);color:#fff;font-weight:600;font-size:14px;cursor:pointer;transition:all .2s}.refresh-btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(59,130,246,.4)}.refresh-icon.refreshing{animation:spin 1s linear infinite}@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}.last-update{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-muted)}.section{background:var(--bg-secondary);border-radius:var(--radius-lg);margin-bottom:16px;box-shadow:var(--shadow-md);border:1px solid var(--border-color);overflow:hidden}.section-header{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;background:var(--bg-tertiary);cursor:pointer;user-select:none;transition:background .2s}.section-header:hover{background:var(--border-color)}.section-header h2{display:flex;align-items:center;gap:10px;font-size:1rem;font-weight:600;color:var(--text-primary)}.section-header h2 i{color:var(--accent-primary)}.section-controls{display:flex;align-items:center;gap:10px}.section-count{background:var(--accent-primary);color:#fff;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;font-family:var(--font-mono)}.section-toggle{color:var(--text-muted);transition:transform .3s}.section.collapsed .section-toggle{transform:rotate(-90deg)}.section.collapsed .section-content{display:none}.section-content{padding:0}.table-container{overflow-x:auto}table{width:100%;border-collapse:collapse}thead{position:sticky;top:0;z-index:10}th{padding:12px 14px;background:var(--bg-tertiary);color:var(--text-secondary);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px;text-align:right;border-bottom:2px solid var(--border-color);white-space:nowrap;position:relative}th:first-child{text-align:left;padding-left:20px}th.sortable{cursor:pointer;padding-right:28px}th.sortable:hover{color:var(--accent-primary)}th.sortable::after{content:'↕';position:absolute;right:10px;top:50%;transform:translateY(-50%);font-size:10px;color:var(--text-muted)}th.sortable.asc::after{content:'↑';color:var(--accent-primary)}th.sortable.desc::after{content:'↓';color:var(--accent-primary)}td{padding:14px;text-align:right;border-bottom:1px solid var(--border-light);font-size:14px;font-family:var(--font-mono)}td:first-child{text-align:left;padding-left:20px;font-weight:600;font-family:var(--font-sans)}tr{transition:background .15s}tr:hover{background:var(--bg-tertiary)}.symbol-link{color:var(--text-primary);text-decoration:none;display:inline-flex;align-items:center;gap:6px;transition:color .2s}.symbol-link:hover{color:var(--accent-primary)}.symbol-link .external-icon{opacity:0;font-size:10px;transition:opacity .2s}.symbol-link:hover .external-icon{opacity:1}.positive{color:var(--positive)!important}.negative{color:var(--negative)!important}.zero-shares{color:var(--negative);font-weight:700}.naked-puts{background:var(--warning-bg);color:var(--warning);font-weight:600;border-radius:4px;padding:4px 8px}.covered-calls{background:var(--positive-bg);color:var(--positive);font-weight:600;border-radius:4px;padding:4px 8px}.uncovered-calls{background:var(--negative-bg);color:var(--negative);font-weight:600;border-radius:4px;padding:4px 8px}.shares-available{background:var(--warning-bg);border-radius:4px;padding:4px 8px}.shares-negative{background:var(--negative-bg);color:var(--negative);font-weight:600;border-radius:4px;padding:4px 8px}tr.hidden{display:none}.no-results{padding:40px;text-align:center;color:var(--text-muted)}.skeleton-loader{padding:20px}.skeleton-row{height:48px;background:linear-gradient(90deg,var(--bg-tertiary) 25%,var(--border-color) 50%,var(--bg-tertiary) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:var(--radius-sm);margin-bottom:8px}@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}#toast-container{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px}.toast{display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:var(--radius-md);box-shadow:var(--shadow-lg);animation:slideIn .3s ease;min-width:280px}.toast.success{border-left:4px solid var(--positive)}.toast.error{border-left:4px solid var(--negative)}.toast.info{border-left:4px solid var(--accent-primary)}.toast-icon{font-size:18px}.toast.success .toast-icon{color:var(--positive)}.toast.error .toast-icon{color:var(--negative)}.toast.info .toast-icon{color:var(--accent-primary)}.toast-message{flex:1;font-size:14px;color:var(--text-primary)}.toast-close{background:0 0;border:none;color:var(--text-muted);cursor:pointer;padding:4px}@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}@keyframes slideOut{from{transform:translateX(0);opacity:1}to{transform:translateX(100%);opacity:0}}.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);backdrop-filter:blur(4px);z-index:10000;align-items:center;justify-content:center}.modal.active{display:flex}.modal-content{background:var(--bg-secondary);border-radius:var(--radius-lg);padding:24px;min-width:360px;box-shadow:var(--shadow-lg);border:1px solid var(--border-color)}.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.modal-header h3{font-size:1.25rem;font-weight:600}.modal-close{background:0 0;border:none;font-size:24px;color:var(--text-muted);cursor:pointer}.shortcuts-list{display:flex;flex-direction:column;gap:12px}.shortcut{display:flex;align-items:center;gap:16px}kbd{display:inline-flex;align-items:center;justify-content:center;min-width:32px;padding:4px 10px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--text-primary)}.footer{display:flex;align-items:center;justify-content:center;gap:12px;padding:16px;color:var(--text-muted);font-size:12px}.footer-sep{color:var(--border-color)}#connectionStatus{display:flex;align-items:center;gap:6px}#connectionStatus.connected{color:var(--positive)}#connectionStatus.disconnected{color:var(--negative)}@media (max-width:1024px){.summary-bar{grid-template-columns:repeat(3,1fr)}}@media (max-width:768px){.header-top{flex-direction:column;gap:16px}.summary-bar{grid-template-columns:repeat(2,1fr)}.controls-row{flex-direction:column}.search-container{max-width:100%}}'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(css)
    logger.info(f'Created: {path}')

def create_js_file(path):
    """Create JavaScript file - can be edited without rebuilding!"""
    js = '''let isRefreshing=!1,currentSort={column:null,direction:"asc"},refreshInterval,cachedData=null,marketStatusInterval;const PREFS_KEY="ttc_positions_prefs";function loadPreferences(){try{return JSON.parse(localStorage.getItem(PREFS_KEY)||"{}")}catch(e){return{}}}function savePreferences(e){try{localStorage.setItem(PREFS_KEY,JSON.stringify({...loadPreferences(),...e}))}catch(e){}}function applyPreferences(){const e=loadPreferences();e.darkMode&&(document.documentElement.setAttribute("data-theme","dark"),document.getElementById("darkModeToggle").innerHTML='<i class="fas fa-sun"></i>'),e.compactView&&document.body.classList.add("compact"),void 0!==e.refreshRate&&(document.getElementById("refreshRate").value=e.refreshRate),e.collapsedSections&&e.collapsedSections.forEach(e=>{const t=document.getElementById(`${e}-section`);t&&t.classList.add("collapsed")})}function showToast(e,t="info",s=3e3){const n=document.getElementById("toast-container"),o=document.createElement("div");o.className=`toast ${t}`;const a={success:"fa-check-circle",error:"fa-exclamation-circle",info:"fa-info-circle"};o.innerHTML=`<i class="fas ${a[t]} toast-icon"></i><span class="toast-message">${e}</span><button class="toast-close" onclick="this.parentElement.remove()"><i class="fas fa-times"></i></button>`,n.appendChild(o),setTimeout(()=>{o.style.animation="slideOut 0.3s ease forwards",setTimeout(()=>o.remove(),300)},s)}function updateMarketStatus(){const e=new Date,t=new Date(e.toLocaleString("en-US",{timeZone:"America/New_York"})),s=t.getDay(),n=t.getHours(),o=t.getMinutes(),a=60*n+o,i=570,c=960,l=document.getElementById("marketStatus"),r=document.getElementById("marketCountdown"),d=l.querySelector(".status-text"),u=0===s||6===s,m=!u&&a>=i&&a<c;if(l.classList.remove("open","closed"),l.classList.add(m?"open":"closed"),m){d.textContent="Market Open";const e=c-a;r.textContent=`Closes in ${Math.floor(e/60)}h ${e%60}m`}else{d.textContent="Market Closed";let e;u?e=(0===s?1:2)*24*60+i-a:a<i?e=i-a:e=1440-a+i;const t=Math.floor(e/60);r.textContent=t>24?`Opens in ${Math.floor(t/24)}d ${t%24}h`:`Opens in ${t}h ${e%60}m`}}function toggleDarkMode(){const e=document.documentElement,t="dark"===e.getAttribute("data-theme");e.setAttribute("data-theme",t?"light":"dark"),document.getElementById("darkModeToggle").innerHTML=t?'<i class="fas fa-moon"></i>':'<i class="fas fa-sun"></i>',savePreferences({darkMode:!t}),showToast(`${t?"Light":"Dark"} mode enabled`,"info",1500)}function toggleCompactView(){const e=document.body.classList.toggle("compact");savePreferences({compactView:e}),showToast(`${e?"Compact":"Normal"} view enabled`,"info",1500)}function toggleSection(e){const t=document.getElementById(`${e}-section`);t.classList.toggle("collapsed");const s=loadPreferences(),n=s.collapsedSections||[];t.classList.contains("collapsed")?n.includes(e)||n.push(e):n.indexOf(e)>-1&&n.splice(n.indexOf(e),1),savePreferences({collapsedSections:n})}function openShortcutsModal(){document.getElementById("shortcuts-modal").classList.add("active")}function closeShortcutsModal(){document.getElementById("shortcuts-modal").classList.remove("active")}function exportToCSV(){if(!cachedData)return void showToast("No data to export","error");let e=["Symbol","Shares","Current Price","Avg Price","Daily Change $","Daily Change %","Last Price","Open","OGap","NP","CC","UC","Shares Available"].join(",")+"\n";cachedData.positions.forEach(t=>{e+=t.map((e,t)=>5===t?(100*e).toFixed(2)+"%":"number"==typeof e?e.toFixed(2):e).join(",")+"\n"});const t=new Blob([e],{type:"text/csv"}),s=document.createElement("a");s.href=URL.createObjectURL(t),s.download=`ttc_positions_${(new Date).toISOString().split("T")[0]}.csv`,s.click(),showToast("Exported to CSV","success")}function formatNumber(e,t){if(""===e||null==e)return"";const s=parseFloat(e);if(isNaN(s))return e;switch(t){case"Current Price":case"Last Price":case"Open":case"Avg Price":return"$"+s.toFixed(2);case"Daily Change $":case"OGap":return(s>=0?"+":"")+"$"+s.toFixed(2);case"Daily Change %":return(s>=0?"+":"")+(100*s).toFixed(2)+"%";default:return s.toLocaleString()}}function createSymbolLink(e){const t=document.createElement("a");return t.href=`https://www.tradingview.com/symbols/${e}/`,t.target="_blank",t.className="symbol-link",t.innerHTML=`${e} <i class="fas fa-external-link-alt external-icon"></i>`,t}function createTable(e,t,s){const n=document.createElement("table"),o=document.createElement("thead"),a=document.createElement("tr");let i=t;"incomplete"===s?i=t.filter(e=>!["NP","CC","UC","Shares Available"].includes(e)):"watchlist"===s&&(i=["Underlying","Current Price","Daily Change $","Daily Change %","Last Price","Open","OGap"]),i.forEach((e,t)=>{const s=document.createElement("th");s.textContent=e,s.classList.add("sortable"),s.addEventListener("click",()=>sortTable(n,t,e)),a.appendChild(s)}),o.appendChild(a),n.appendChild(o);const c=document.createElement("tbody");return e.sort((e,t)=>e[0].toString().toLowerCase().localeCompare(t[0].toString().toLowerCase())),e.forEach(e=>{const n=document.createElement("tr");let o=[...e];"incomplete"===s?o=o.filter((e,s)=>!["NP","CC","UC","Shares Available"].includes(t[s])):"watchlist"===s&&(o=o.slice(0,7)),o.forEach((s,o)=>{const a=document.createElement("td"),c=i[o];"Underlying"===c?a.appendChild(createSymbolLink(s)):"number"==typeof s?(a.textContent=formatNumber(s,c),["Daily Change $","Daily Change %","OGap"].includes(c)?(s>0&&a.classList.add("positive"),s<0&&a.classList.add("negative")):"Current Price"===c?(e[t.indexOf("Daily Change $")]>0&&a.classList.add("positive"),e[t.indexOf("Daily Change $")]<0&&a.classList.add("negative")):"Shares"===c&&0===s?a.classList.add("zero-shares"):"NP"===c&&s>0?a.classList.add("naked-puts"):"CC"===c&&s>0?a.classList.add("covered-calls"):"UC"===c&&s>0?a.classList.add("uncovered-calls"):"Shares Available"===c&&(s>0&&a.classList.add("shares-available"),s<0&&a.classList.add("shares-negative"))):a.textContent=s,n.appendChild(a)}),c.appendChild(n)}),n.appendChild(c),n}function sortTable(e,t){const s=e.querySelector("tbody"),n=Array.from(s.querySelectorAll("tr")),o=e.querySelector(`th:nth-child(${t+1})`);e.querySelectorAll("th").forEach(e=>e.classList.remove("asc","desc"));let a="asc";currentSort.column===t&&(a="asc"===currentSort.direction?"desc":"asc"),currentSort={column:t,direction:a},o.classList.add(a),n.sort((e,s)=>{const n=getCellValue(e,t),o=getCellValue(s,t);return isNaN(parseFloat(n))||isNaN(parseFloat(o))?"asc"===a?n.localeCompare(o):o.localeCompare(n):"asc"===a?parseFloat(n)-parseFloat(o):parseFloat(o)-parseFloat(n)}),n.forEach(e=>s.appendChild(e))}function getCellValue(e,t){const s=e.querySelector(`td:nth-child(${t+1})`);return s?s.textContent.trim().replace(/[$%+,]/g,""):""}function filterTables(e){document.querySelectorAll("table").forEach(t=>{const s=t.querySelectorAll("tbody tr");let n=!1;s.forEach(t=>{(t.querySelector("td:first-child")?.textContent||"").match(new RegExp(e,"i"))?(t.classList.remove("hidden"),n=!0):t.classList.add("hidden")});let o=t.parentElement.querySelector(".no-results");n||0===s.length?o&&(o.style.display="none"):(o||(o=document.createElement("div"),o.className="no-results",o.textContent="No matching symbols found",t.parentElement.appendChild(o)),o.style.display="block")})}function clearSearch(){const e=document.getElementById("searchInput");e.value="",filterTables(""),e.focus()}function updateSummaryStats(e){document.getElementById("statPositions").textContent=e.positions.length,document.getElementById("statWatchlist").textContent=e.watchlist.length;let t=0,s=0,n=0;e.positions.forEach(e=>{const o=e[4];o>0&&t++,o<0&&s++,n+=o*e[1]}),document.getElementById("statGainers").textContent=t,document.getElementById("statLosers").textContent=s;const o=document.getElementById("statDailyPL");o.textContent=(n>=0?"+":"")+"$"+n.toFixed(2),o.className="stat-value "+(n>=0?"positive":"negative")}function updateSectionCounts(e){document.getElementById("positions-count").textContent=e.positions.length,document.getElementById("incomplete-count").textContent=e.incomplete_lots.length,document.getElementById("watchlist-count").textContent=e.watchlist.length}function updateLastUpdateTime(){document.getElementById("lastUpdate").innerHTML=`<i class="far fa-clock"></i> <span>Updated at ${(new Date).toLocaleTimeString()}</span>`}function setLoadingState(e){const t=document.querySelector(".refresh-icon");e?t.classList.add("refreshing"):t.classList.remove("refreshing")}async function updateTables(){if(isRefreshing)return;isRefreshing=!0,setLoadingState(!0);try{const e=await fetch("/api/data");if(!e.ok)throw new Error((await e.json()).error||"Server error");const t=await e.json();cachedData=t;const s=["Underlying","Shares","Current Price","Avg Price","Daily Change $","Daily Change %","Last Price","Open","OGap","NP","CC","UC","Shares Available"],n=document.getElementById("positions-table"),o=document.getElementById("incomplete-table"),a=document.getElementById("watchlist-table");n.innerHTML=t.positions.length>0?"":'<div class="no-results">No positions found</div>',o.innerHTML=t.incomplete_lots.length>0?"":'<div class="no-results">No incomplete lots</div>',a.innerHTML=t.watchlist.length>0?"":'<div class="no-results">No watchlist items</div>',t.positions.length>0&&n.appendChild(createTable(t.positions,s,"positions")),t.incomplete_lots.length>0&&o.appendChild(createTable(t.incomplete_lots,s,"incomplete")),t.watchlist.length>0&&a.appendChild(createTable(t.watchlist,s,"watchlist")),updateLastUpdateTime(),updateSummaryStats(t),updateSectionCounts(t);const i=document.getElementById("searchInput").value;i&&filterTables(i),document.getElementById("connectionStatus").className="connected",document.getElementById("connectionStatus").innerHTML='<i class="fas fa-plug"></i> IBKR Connected',showToast("Data refreshed","success",1500)}catch(e){console.error("Error:",e),showToast(e.message,"error"),document.getElementById("connectionStatus").className="disconnected",document.getElementById("connectionStatus").innerHTML='<i class="fas fa-plug"></i> Connection Error'}finally{isRefreshing=!1,setLoadingState(!1)}}function setRefreshRate(e){refreshInterval&&clearInterval(refreshInterval),e>0&&(refreshInterval=setInterval(updateTables,1e3*e)),savePreferences({refreshRate:e})}document.addEventListener("keydown",e=>{if("INPUT"===e.target.tagName||"TEXTAREA"===e.target.tagName)return void("Escape"===e.key&&(clearSearch(),e.target.blur()));switch(e.key.toLowerCase()){case"r":e.preventDefault(),updateTables();break;case"/":e.preventDefault(),document.getElementById("searchInput").focus();break;case"d":e.preventDefault(),toggleDarkMode();break;case"c":e.preventDefault(),toggleCompactView();break;case"e":e.preventDefault(),exportToCSV();break;case"?":e.preventDefault(),openShortcutsModal();break;case"escape":closeShortcutsModal()}}),document.addEventListener("DOMContentLoaded",()=>{applyPreferences(),document.getElementById("darkModeToggle").addEventListener("click",toggleDarkMode),document.getElementById("compactToggle").addEventListener("click",toggleCompactView),document.getElementById("exportBtn").addEventListener("click",exportToCSV),document.getElementById("shortcutsBtn").addEventListener("click",openShortcutsModal),document.getElementById("refreshButton").addEventListener("click",updateTables),document.getElementById("refreshRate").addEventListener("change",e=>setRefreshRate(parseInt(e.target.value))),document.getElementById("searchInput").addEventListener("input",e=>filterTables(e.target.value)),document.getElementById("clearSearch").addEventListener("click",clearSearch),document.getElementById("shortcuts-modal").addEventListener("click",e=>{e.target===document.getElementById("shortcuts-modal")&&closeShortcutsModal()}),updateMarketStatus(),marketStatusInterval=setInterval(updateMarketStatus,6e4),updateTables(),setRefreshRate(parseInt(document.getElementById("refreshRate").value)),checkForUpdates()});function showUpdateNotification(e,t){const n=document.createElement("div");n.id="update-banner",n.style.cssText="position:fixed;top:0;left:0;right:0;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;padding:12px 20px;display:flex;align-items:center;justify-content:center;gap:16px;z-index:10001;font-family:var(--font-sans);box-shadow:0 4px 12px rgba(0,0,0,0.15);",n.innerHTML=`<i class="fas fa-gift" style="font-size:20px"></i><span><strong>Update Available!</strong> Version ${e} is ready.</span><button onclick="installUpdate()" style="background:white;color:#3b82f6;border:none;padding:8px 16px;border-radius:6px;font-weight:600;cursor:pointer">Update Now</button><button onclick="dismissUpdate()" style="background:transparent;color:white;border:1px solid rgba(255,255,255,0.5);padding:8px 12px;border-radius:6px;cursor:pointer">Later</button>`,document.body.prepend(n),document.querySelector(".container").style.marginTop="60px"}function dismissUpdate(){const e=document.getElementById("update-banner");e&&e.remove(),document.querySelector(".container").style.marginTop=""}async function installUpdate(){showToast("Downloading update...","info",5e3);try{const e=await fetch("/api/update/download"),t=await e.json();t.success?showToast("Installing update... The app will restart.","success",1e4):showToast("Update failed: "+t.error,"error")}catch(e){showToast("Update failed: "+e.message,"error")}}async function checkForUpdates(){try{const e=await fetch("/api/update/check"),t=await e.json();t.available&&showUpdateNotification(t.latest_version,t.release_notes||"")}catch(e){console.log("Could not check for updates:",e)}}'''
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
