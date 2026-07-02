# Application startup: logging, database, IBKR manager, Flask server,
# native window, and shutdown.

import atexit
import json
import logging
import os
import platform
import signal
import socket
import sys
import threading
import time
import webbrowser

from datetime import datetime
from logging.handlers import RotatingFileHandler

from ttc_app import app_update
from ttc_app.config import (
    APP_DIR, APP_NAME, APP_VERSION, DEFAULT_PORT, MAX_PORT_TRIES, UI_DIR,
    VERSION_FILE,
)
from ttc_app.db import Database
from ttc_app.ibkr_manager import IBKRManager
from ttc_app import web
from ttc_app.web import app, state

logger = logging.getLogger(__name__)

try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    webview = None
    HAS_WEBVIEW = False

shutdown_event = threading.Event()
_cleaned_up = False


def setup_logging():
    log_dir = os.path.join(APP_DIR, 'log', datetime.now().strftime('%Y-%m-%d'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    handler = RotatingFileHandler(
        os.path.join(log_dir, 'ttc_positions_app.log'),
        maxBytes=1 * 1024 * 1024, backupCount=5)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(handler)


def find_available_port(start_port=DEFAULT_PORT, max_tries=MAX_PORT_TRIES):
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise OSError("Could not find an available port")


def archive_legacy_resources():
    """The pre-2.3.0 app wrote its UI into APP_DIR/resources and preferred it
    over the bundled copy. That directory would now only confuse (and it syncs
    through Dropbox), so move it aside once."""
    legacy = os.path.join(APP_DIR, 'resources')
    if os.path.isdir(legacy):
        backup = os.path.join(APP_DIR, 'resources_old_backup')
        try:
            if os.path.exists(backup):
                backup = backup + '_' + datetime.now().strftime('%Y%m%d%H%M%S')
            os.rename(legacy, backup)
            logger.info(f'Archived legacy resources dir to {backup}')
        except OSError as e:
            logger.warning(f'Could not archive legacy resources dir: {e}')


def record_version_transition():
    previous_version = None
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, 'r') as f:
                previous_version = json.load(f).get('app_version')
        except Exception:
            pass
    if previous_version and app_update.parse_version(previous_version) < app_update.parse_version(APP_VERSION):
        logger.info(f'Updated from v{previous_version} to v{APP_VERSION}')
        state.startup_messages.append((f'Updated to v{APP_VERSION}', 'success'))
    failed_update_message = app_update.check_post_update_state(APP_DIR)
    if failed_update_message:
        state.startup_messages.append(
            ('The last update could not be applied. It will be offered again.', 'error'))
    try:
        with open(VERSION_FILE, 'w') as f:
            json.dump({'app_version': APP_VERSION,
                       'updated_at': datetime.now().isoformat()}, f, indent=2)
    except Exception as e:
        logger.warning(f'Could not save version info: {e}')


def cleanup():
    global _cleaned_up
    if _cleaned_up:
        return
    _cleaned_up = True
    logger.info('Cleaning up resources...')
    shutdown_event.set()
    if state.ibkr is not None:
        try:
            state.ibkr.stop()
            logger.info('IBKR connection manager stopped')
        except Exception:
            pass
    if state.db is not None:
        try:
            state.db.close()
        except Exception:
            pass
    if state.webview_window:
        try:
            state.webview_window.destroy()
        except Exception:
            pass
    logger.info('Cleanup complete')


def signal_handler(signum, frame):
    logger.info(f'Received signal {signum}, shutting down...')
    cleanup()
    sys.exit(0)


def run_server(port):
    from waitress import serve
    logger.info(f'Starting server on port {port}...')
    serve(app, host='127.0.0.1', port=port, threads=4)


def create_native_window(port):
    url = f'http://127.0.0.1:{port}'
    logger.info(f'Creating native window for URL: {url}')
    time.sleep(1)
    window = webview.create_window(
        APP_NAME, url, width=1400, height=900, min_size=(800, 600),
        confirm_close=False, text_select=True)
    state.webview_window = window
    logger.info('Native window created successfully')

    def on_closed():
        logger.info('Window closed, initiating shutdown...')
        cleanup()

    window.events.closed += on_closed
    webview.start()


def open_in_browser(port):
    url = f'http://127.0.0.1:{port}'
    time.sleep(1)
    webbrowser.open(url)
    try:
        print(f"\n{'=' * 50}")
        print(f"TTC Positions Report is running at: {url}")
        print("Press Ctrl+C to stop the server")
        print(f"{'=' * 50}\n")
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main():
    setup_logging()
    logger.info(f'Starting {APP_NAME} v{APP_VERSION}')
    logger.info(f'App directory: {APP_DIR}')
    logger.info(f'UI directory: {UI_DIR}')
    logger.info(f'Platform: {platform.system()} {platform.release()}')

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(cleanup)
    state.cleanup = cleanup

    record_version_transition()
    archive_legacy_resources()

    # Database (imports legacy JSON files on first run)
    state.db = Database()
    logger.info(f'Database: {state.db.path}')

    # Persistent IBKR connection manager (qual-failure cache lives in the DB)
    state.ibkr = IBKRManager(
        client_id=state.db.get_setting('ibkr_client_id'),
        on_client_id_change=lambda cid: state.db.set_setting('ibkr_client_id', cid),
        qual_failure_cache=state.db,
    )
    state.ibkr.start()
    logger.info(f'IBKR connection manager started (clientId={state.ibkr.client_id})')

    try:
        port = find_available_port()
        logger.info(f'Using port {port}')
    except OSError as e:
        logger.error(f'Could not find available port: {e}')
        sys.exit(1)

    server_thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    server_thread.start()
    logger.info(f'Server thread started on port {port}')

    threading.Thread(target=web.check_updates_background, daemon=True).start()
    threading.Thread(target=web.auto_flex_import_background, daemon=True).start()

    if HAS_WEBVIEW:
        logger.info('Starting native window...')
        create_native_window(port)
    else:
        logger.info('Opening in browser...')
        open_in_browser(port)

    logger.info('Application shutting down')
    cleanup()
