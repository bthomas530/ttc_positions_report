# Paths and constants shared across the app.
#
# Three distinct directories:
#   APP_DIR    - where the exe/script lives. On the user's machine this is a
#                Dropbox-synced folder, so only small, conflict-tolerant files
#                belong here (logs, watchlist mirror, version.json).
#   DATA_DIR   - per-user local data (%LOCALAPPDATA% / Application Support).
#                The SQLite database lives here: SQLite WAL files inside a
#                Dropbox-synced folder risk corruption and conflicted copies.
#   UI_DIR     - bundled templates/static assets (sys._MEIPASS when frozen).

import os
import platform
import sys

APP_NAME = "TTC Positions Report"
APP_VERSION = "2.3.0"
USER_AGENT = f'{APP_NAME}/{APP_VERSION}'

DEFAULT_PORT = 8082
MAX_PORT_TRIES = 10

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    # Running from source: the repo root (parent of this package)
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BUNDLE_DIR = APP_DIR

UI_DIR = os.path.join(BUNDLE_DIR, 'ui')
TEMPLATE_DIR = os.path.join(UI_DIR, 'templates')
STATIC_DIR = os.path.join(UI_DIR, 'static')


def _default_data_dir():
    override = os.environ.get('TTC_DATA_DIR')
    if override:
        return override
    system = platform.system()
    if system == 'Windows':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser(r'~\AppData\Local')
    elif system == 'Darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'TTC_Positions')


DATA_DIR = _default_data_dir()
DB_PATH = os.path.join(DATA_DIR, 'ttc.db')

# Legacy JSON files in APP_DIR, imported into the DB on first run
LEGACY_WATCHLIST_FILE = os.path.join(APP_DIR, 'ttc_watchlist.json')
LEGACY_PRICE_CACHE_FILE = os.path.join(APP_DIR, 'price_cache.json')
LEGACY_QUAL_FAILURES_FILE = os.path.join(APP_DIR, 'qual_failures.json')
LEGACY_SETTINGS_FILE = os.path.join(APP_DIR, 'app_settings.json')
VERSION_FILE = os.path.join(APP_DIR, 'version.json')
