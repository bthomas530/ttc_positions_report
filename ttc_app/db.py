# SQLite persistence layer.
#
# The database lives in DATA_DIR (%LOCALAPPDATA%\TTC_Positions on Windows),
# NOT in the app dir: the app dir is Dropbox-synced and SQLite WAL files in a
# synced folder are a corruption/conflicted-copy risk. On first run, legacy
# JSON files (price_cache.json, ttc_watchlist.json, qual_failures.json,
# app_settings.json) are imported. The watchlist is mirrored back to
# ttc_watchlist.json for one release so a rollback to 2.2.x loses nothing.

import csv
import json
import logging
import os
import shutil
import sqlite3
import threading

from datetime import datetime, timedelta

from ttc_app import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

PRICE_RETENTION_DAYS = 400
PRICE_THROTTLE_MINUTES = 5
QUAL_FAILURE_TTL_HOURS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
  symbol TEXT NOT NULL,
  ts     TEXT NOT NULL,
  last   REAL, open REAL, high REAL, low REAL, close REAL, change REAL,
  source TEXT,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_price_latest ON price_history(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS qual_failures (
  symbol         TEXT PRIMARY KEY,
  reason         TEXT,
  fail_count     INTEGER DEFAULT 0,
  last_failed_ts TEXT
);

CREATE TABLE IF NOT EXISTS trades (
  exec_id    TEXT PRIMARY KEY,          -- Flex tradeID: idempotent re-import
  order_id   TEXT,
  account    TEXT,
  symbol     TEXT NOT NULL,             -- underlying symbol
  local_symbol TEXT,                    -- full option symbol for OPT
  sec_type   TEXT NOT NULL,             -- STK | OPT
  put_call   TEXT,                      -- P | C | ''
  strike     REAL,
  expiry     TEXT,                      -- YYYY-MM-DD
  multiplier REAL,
  buy_sell   TEXT,                      -- BUY | SELL
  open_close TEXT,                      -- O | C | ''
  quantity   REAL,                      -- signed (BUY +, SELL -)
  price      REAL,
  proceeds   REAL,                      -- signed cash (sells +, buys -)
  commission REAL,                      -- negative
  trade_ts   TEXT,                      -- ISO datetime
  codes      TEXT                       -- semicolon-joined IBKR codes (A, Ep, Ex...)
);
CREATE INDEX IF NOT EXISTS ix_trades_symbol_ts ON trades(symbol, trade_ts);

CREATE TABLE IF NOT EXISTS tranches (
  id           INTEGER PRIMARY KEY,
  symbol       TEXT NOT NULL,
  qty          INTEGER NOT NULL,
  opened_ts    TEXT,
  open_price   REAL,
  open_source  TEXT,                    -- BUY | PUT_ASSIGNMENT | SEEDED
  closed_ts    TEXT,
  close_price  REAL,
  close_source TEXT,                    -- SELL | CALL_ASSIGNMENT
  status       TEXT DEFAULT 'OPEN',
  premium      REAL DEFAULT 0,          -- option premium attributed (cash)
  realized_pl  REAL,                    -- for CLOSED tranches, incl. premium
  covering_call TEXT,                   -- JSON {strike, expiry} or NULL
  inferred     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tranche_events (
  id         INTEGER PRIMARY KEY,
  tranche_id INTEGER,                   -- NULL for symbol-level events
  symbol     TEXT NOT NULL,
  exec_id    TEXT,
  event_type TEXT NOT NULL,
  ts         TEXT,
  amount     REAL,                      -- signed cash
  qty        REAL,
  details    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_symbol ON tranche_events(symbol, ts);

CREATE TABLE IF NOT EXISTS option_snapshots (
  ts        TEXT NOT NULL,
  conid     INTEGER NOT NULL,
  symbol    TEXT,
  put_call  TEXT,
  strike    REAL,
  expiry    TEXT,
  position  REAL,
  mark      REAL,
  entry_price REAL,
  delta REAL, gamma REAL, theta REAL, vega REAL, iv REAL,
  und_price REAL,
  dte       INTEGER,
  PRIMARY KEY (ts, conid)
);

CREATE TABLE IF NOT EXISTS flex_imports (
  id             INTEGER PRIMARY KEY,
  requested_ts   TEXT,
  reference_code TEXT,
  trade_count    INTEGER,
  new_count      INTEGER,
  status         TEXT,                  -- ok | error
  error          TEXT
);
"""


class Database:
    """Thread-safe SQLite wrapper (one connection guarded by a lock)."""

    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fresh = not os.path.exists(self.path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA synchronous=NORMAL')
        self._migrate()
        if fresh:
            self._import_legacy_files()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------- schema ----------

    def _migrate(self):
        with self._lock, self._conn:
            version = self._conn.execute('PRAGMA user_version').fetchone()[0]
            if version < 1:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                logger.info(f'Database schema initialized (v{SCHEMA_VERSION}) at {self.path}')

    def _import_legacy_files(self):
        """One-time import of the pre-2.3.0 JSON state files."""
        try:
            if os.path.exists(config.LEGACY_PRICE_CACHE_FILE):
                with open(config.LEGACY_PRICE_CACHE_FILE, 'r') as f:
                    cache = json.load(f)
                rows = []
                for symbol, entry in cache.get('prices', {}).items():
                    ts = entry.get('timestamp') or datetime.now().isoformat()
                    rows.append((symbol, ts, entry.get('last'), entry.get('open'),
                                 entry.get('high'), entry.get('low'), entry.get('close'),
                                 entry.get('change'), entry.get('source', 'cached')))
                with self._lock, self._conn:
                    self._conn.executemany(
                        'INSERT OR IGNORE INTO price_history '
                        '(symbol, ts, last, open, high, low, close, change, source) '
                        'VALUES (?,?,?,?,?,?,?,?,?)', rows)
                logger.info(f'Imported {len(rows)} prices from legacy price_cache.json')

            if os.path.exists(config.LEGACY_WATCHLIST_FILE):
                with open(config.LEGACY_WATCHLIST_FILE, 'r') as f:
                    watchlist = json.load(f).get('WATCHLIST', [])
                self.set_setting('watchlist', sorted(watchlist))
                logger.info(f'Imported {len(watchlist)} watchlist symbols from legacy file')

            if os.path.exists(config.LEGACY_QUAL_FAILURES_FILE):
                with open(config.LEGACY_QUAL_FAILURES_FILE, 'r') as f:
                    entries = json.load(f)
                with self._lock, self._conn:
                    for symbol, entry in entries.items():
                        self._conn.execute(
                            'INSERT OR REPLACE INTO qual_failures '
                            '(symbol, reason, fail_count, last_failed_ts) VALUES (?,?,?,?)',
                            (symbol, entry.get('reason', ''), entry.get('fail_count', 1),
                             entry.get('last_failed')))
                logger.info(f'Imported {len(entries)} qualification failures from legacy file')

            if os.path.exists(config.LEGACY_SETTINGS_FILE):
                with open(config.LEGACY_SETTINGS_FILE, 'r') as f:
                    legacy = json.load(f)
                if legacy.get('ibkr_client_id'):
                    self.set_setting('ibkr_client_id', legacy['ibkr_client_id'])
        except Exception as e:
            logger.warning(f'Legacy data import failed (continuing with empty DB): {e}')

    # ---------- settings ----------

    def get_setting(self, key, default=None):
        with self._lock:
            row = self._conn.execute(
                'SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row['value'])
        except (TypeError, ValueError):
            return row['value']

    def set_setting(self, key, value):
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                (key, json.dumps(value)))

    # ---------- watchlist (mirrored to legacy JSON for rollback safety) ----------

    def get_watchlist(self):
        return self.get_setting('watchlist', ['AAPL', 'NVDA'])

    def set_watchlist(self, symbols):
        symbols = sorted(set(symbols))
        self.set_setting('watchlist', symbols)
        try:
            with open(config.LEGACY_WATCHLIST_FILE, 'w') as f:
                json.dump({'WATCHLIST': symbols}, f, indent=4)
        except Exception as e:
            logger.debug(f'Could not mirror watchlist to JSON: {e}')

    # ---------- price history ----------

    def record_prices(self, market_data, now=None):
        """Append one row per symbol, throttled to one row per symbol per
        PRICE_THROTTLE_MINUTES; prunes rows older than PRICE_RETENTION_DAYS."""
        now = now or datetime.now()
        now_iso = now.isoformat()
        threshold = (now - timedelta(minutes=PRICE_THROTTLE_MINUTES)).isoformat()
        cutoff = (now - timedelta(days=PRICE_RETENTION_DAYS)).isoformat()
        inserted = 0
        with self._lock, self._conn:
            for symbol, entry in market_data.items():
                last = entry.get('last') or 0
                if last <= 0:
                    continue
                recent = self._conn.execute(
                    'SELECT 1 FROM price_history WHERE symbol = ? AND ts > ? LIMIT 1',
                    (symbol, threshold)).fetchone()
                if recent:
                    continue
                self._conn.execute(
                    'INSERT OR IGNORE INTO price_history '
                    '(symbol, ts, last, open, high, low, close, change, source) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (symbol, entry.get('timestamp') or now_iso, last,
                     entry.get('open'), entry.get('high'), entry.get('low'),
                     entry.get('close'), entry.get('change'), entry.get('source', 'ibkr')))
                inserted += 1
            self._conn.execute('DELETE FROM price_history WHERE ts < ?', (cutoff,))
        return inserted

    def latest_prices(self):
        """Most recent stored price per symbol:
        {symbol: {last, open, high, low, close, change, source, timestamp}}"""
        with self._lock:
            rows = self._conn.execute(
                'SELECT p.* FROM price_history p '
                'JOIN (SELECT symbol, MAX(ts) AS ts FROM price_history GROUP BY symbol) m '
                'ON p.symbol = m.symbol AND p.ts = m.ts').fetchall()
        result = {}
        for row in rows:
            result[row['symbol']] = {
                'last': row['last'], 'open': row['open'], 'high': row['high'],
                'low': row['low'], 'close': row['close'], 'change': row['change'],
                'source': row['source'], 'timestamp': row['ts'],
            }
        return result

    def last_price_update(self):
        with self._lock:
            row = self._conn.execute('SELECT MAX(ts) AS ts FROM price_history').fetchone()
        return row['ts'] if row else None

    def price_history(self, symbol, days=30):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                'SELECT ts, last, source FROM price_history '
                'WHERE symbol = ? AND ts >= ? ORDER BY ts', (symbol, cutoff)).fetchall()
        return [dict(r) for r in rows]

    # ---------- qualification failures (duck-typed for IBKRManager) ----------

    def is_failed(self, symbol, now=None):
        now = now or datetime.now()
        with self._lock:
            row = self._conn.execute(
                'SELECT last_failed_ts FROM qual_failures WHERE symbol = ?',
                (symbol,)).fetchone()
        if not row or not row['last_failed_ts']:
            return False
        try:
            last_failed = datetime.fromisoformat(row['last_failed_ts'])
        except ValueError:
            return False
        return now - last_failed < timedelta(hours=QUAL_FAILURE_TTL_HOURS)

    def record_failure(self, symbol, reason='', now=None):
        now = now or datetime.now()
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO qual_failures (symbol, reason, fail_count, last_failed_ts) '
                'VALUES (?, ?, 1, ?) '
                'ON CONFLICT(symbol) DO UPDATE SET '
                'fail_count = fail_count + 1, reason = excluded.reason, '
                'last_failed_ts = excluded.last_failed_ts',
                (symbol, str(reason)[:200], now.isoformat()))

    def record_success(self, symbol):
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM qual_failures WHERE symbol = ?', (symbol,))

    # ---------- trades ----------

    def insert_trades(self, trades):
        """INSERT OR IGNORE on exec_id; returns how many rows were new."""
        with self._lock, self._conn:
            before = self._conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
            self._conn.executemany(
                'INSERT OR IGNORE INTO trades '
                '(exec_id, order_id, account, symbol, local_symbol, sec_type, put_call, '
                ' strike, expiry, multiplier, buy_sell, open_close, quantity, price, '
                ' proceeds, commission, trade_ts, codes) '
                'VALUES (:exec_id, :order_id, :account, :symbol, :local_symbol, :sec_type, '
                '        :put_call, :strike, :expiry, :multiplier, :buy_sell, :open_close, '
                '        :quantity, :price, :proceeds, :commission, :trade_ts, :codes)',
                trades)
            after = self._conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
        return after - before

    def get_trades(self, symbol=None):
        query = 'SELECT * FROM trades'
        params = ()
        if symbol:
            query += ' WHERE symbol = ?'
            params = (symbol,)
        query += ' ORDER BY trade_ts, exec_id'
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def trade_count(self):
        with self._lock:
            return self._conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]

    # ---------- tranches (derived: always fully rebuilt) ----------

    def replace_tranches(self, tranches, events):
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM tranches')
            self._conn.execute('DELETE FROM tranche_events')
            self._conn.executemany(
                'INSERT INTO tranches '
                '(id, symbol, qty, opened_ts, open_price, open_source, closed_ts, '
                ' close_price, close_source, status, premium, realized_pl, '
                ' covering_call, inferred) '
                'VALUES (:id, :symbol, :qty, :opened_ts, :open_price, :open_source, '
                '        :closed_ts, :close_price, :close_source, :status, :premium, '
                '        :realized_pl, :covering_call, :inferred)',
                [{**t, 'covering_call': json.dumps(t['covering_call']) if t.get('covering_call') else None}
                 for t in tranches])
            self._conn.executemany(
                'INSERT INTO tranche_events '
                '(tranche_id, symbol, exec_id, event_type, ts, amount, qty, details) '
                'VALUES (:tranche_id, :symbol, :exec_id, :event_type, :ts, :amount, '
                '        :qty, :details)',
                events)

    def get_tranches(self, include_closed=True):
        query = 'SELECT * FROM tranches'
        if not include_closed:
            query += " WHERE status = 'OPEN'"
        query += ' ORDER BY symbol, opened_ts'
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        result = []
        for row in rows:
            t = dict(row)
            if t.get('covering_call'):
                try:
                    t['covering_call'] = json.loads(t['covering_call'])
                except ValueError:
                    t['covering_call'] = None
            result.append(t)
        return result

    def get_events(self, symbol=None):
        query = 'SELECT * FROM tranche_events'
        params = ()
        if symbol:
            query += ' WHERE symbol = ?'
            params = (symbol,)
        query += ' ORDER BY ts, id'
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ---------- option snapshots ----------

    def record_option_snapshots(self, snapshots, now=None):
        """Persist option rows, throttled per conid like prices."""
        now = now or datetime.now()
        now_iso = now.isoformat()
        threshold = (now - timedelta(minutes=PRICE_THROTTLE_MINUTES)).isoformat()
        inserted = 0
        with self._lock, self._conn:
            for snap in snapshots:
                recent = self._conn.execute(
                    'SELECT 1 FROM option_snapshots WHERE conid = ? AND ts > ? LIMIT 1',
                    (snap['conId'], threshold)).fetchone()
                if recent:
                    continue
                self._conn.execute(
                    'INSERT OR IGNORE INTO option_snapshots '
                    '(ts, conid, symbol, put_call, strike, expiry, position, mark, '
                    ' entry_price, delta, gamma, theta, vega, iv, und_price, dte) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (now_iso, snap['conId'], snap.get('symbol'), snap.get('right'),
                     snap.get('strike'), snap.get('expiry'), snap.get('position'),
                     snap.get('mark'), snap.get('entry_price'), snap.get('delta'),
                     snap.get('gamma'), snap.get('theta'), snap.get('vega'),
                     snap.get('iv'), snap.get('und_price'), snap.get('dte')))
                inserted += 1
            cutoff = (now - timedelta(days=PRICE_RETENTION_DAYS)).isoformat()
            self._conn.execute('DELETE FROM option_snapshots WHERE ts < ?', (cutoff,))
        return inserted

    # ---------- flex imports ----------

    def record_flex_import(self, reference_code, trade_count, new_count, status, error=None):
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO flex_imports '
                '(requested_ts, reference_code, trade_count, new_count, status, error) '
                'VALUES (?,?,?,?,?,?)',
                (datetime.now().isoformat(), reference_code, trade_count,
                 new_count, status, error))

    def last_flex_import(self):
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM flex_imports ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    # ---------- export (for remote support via the Dropbox folder) ----------

    def export_to(self, export_dir):
        """Copy the DB and CSV dumps of trades/tranches into export_dir."""
        os.makedirs(export_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        with self._lock:
            self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db_copy = os.path.join(export_dir, f'ttc_{stamp}.db')
        shutil.copy2(self.path, db_copy)

        written = [db_copy]
        for table in ('trades', 'tranches', 'tranche_events'):
            with self._lock:
                rows = self._conn.execute(f'SELECT * FROM {table}').fetchall()
            csv_path = os.path.join(export_dir, f'{table}_{stamp}.csv')
            with open(csv_path, 'w', newline='') as f:
                if rows:
                    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(dict(r) for r in rows)
                else:
                    f.write('')
            written.append(csv_path)
        return written
