import os
import sys

from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app import config
from ttc_app.db import Database


@pytest.fixture(autouse=True)
def isolate_legacy_files(tmp_path, monkeypatch):
    """Point legacy-import paths at empty temp locations so a fresh test DB
    doesn't slurp the real repo's ttc_watchlist.json / price_cache.json."""
    monkeypatch.setattr(config, 'LEGACY_WATCHLIST_FILE', str(tmp_path / '_wl.json'))
    monkeypatch.setattr(config, 'LEGACY_PRICE_CACHE_FILE', str(tmp_path / '_pc.json'))
    monkeypatch.setattr(config, 'LEGACY_QUAL_FAILURES_FILE', str(tmp_path / '_qf.json'))
    monkeypatch.setattr(config, 'LEGACY_SETTINGS_FILE', str(tmp_path / '_st.json'))


def make_db(tmp_path):
    return Database(path=str(tmp_path / 'test.db'))


class TestSettings:
    def test_roundtrip(self, tmp_path):
        db = make_db(tmp_path)
        db.set_setting('flex_query_id', '123456')
        assert db.get_setting('flex_query_id') == '123456'
        db.set_setting('buyback_threshold_pct', 12.5)
        assert db.get_setting('buyback_threshold_pct') == 12.5

    def test_default(self, tmp_path):
        db = make_db(tmp_path)
        assert db.get_setting('missing', 'fallback') == 'fallback'

    def test_watchlist_default(self, tmp_path):
        db = make_db(tmp_path)
        assert db.get_watchlist() == ['AAPL', 'NVDA']


class TestPriceHistory:
    def test_record_and_latest(self, tmp_path):
        db = make_db(tmp_path)
        n = db.record_prices({'AAPL': {'last': 294.5, 'open': 293.0, 'close': 294.0,
                                       'change': 0.5, 'source': 'ibkr'}})
        assert n == 1
        latest = db.latest_prices()
        assert latest['AAPL']['last'] == 294.5
        assert latest['AAPL']['source'] == 'ibkr'

    def test_throttle(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime(2026, 7, 1, 10, 0)
        assert db.record_prices({'AAPL': {'last': 100}}, now=now) == 1
        # Within the 5-minute window: skipped
        assert db.record_prices({'AAPL': {'last': 101, 'timestamp': (now + timedelta(minutes=2)).isoformat()}},
                                now=now + timedelta(minutes=2)) == 0
        # After the window: recorded
        assert db.record_prices({'AAPL': {'last': 102, 'timestamp': (now + timedelta(minutes=6)).isoformat()}},
                                now=now + timedelta(minutes=6)) == 1
        assert db.latest_prices()['AAPL']['last'] == 102

    def test_zero_prices_skipped(self, tmp_path):
        db = make_db(tmp_path)
        assert db.record_prices({'AAPL': {'last': 0}}) == 0

    def test_retention_prune(self, tmp_path):
        db = make_db(tmp_path)
        old = datetime.now() - timedelta(days=500)
        db.record_prices({'OLD': {'last': 5, 'timestamp': old.isoformat()}}, now=old)
        db.record_prices({'NEW': {'last': 7}})
        latest = db.latest_prices()
        assert 'NEW' in latest and 'OLD' not in latest


class TestQualFailures:
    def test_ttl(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime(2026, 7, 1, 9, 0)
        db.record_failure('PANR', 'nope', now=now)
        assert db.is_failed('PANR', now=now + timedelta(hours=1)) is True
        assert db.is_failed('PANR', now=now + timedelta(hours=25)) is False

    def test_success_clears(self, tmp_path):
        db = make_db(tmp_path)
        db.record_failure('PSTG')
        db.record_success('PSTG')
        assert db.is_failed('PSTG') is False


class TestTrades:
    TRADE = {
        'exec_id': 'T1', 'order_id': 'O1', 'account': 'U123', 'symbol': 'AA',
        'local_symbol': 'AA', 'sec_type': 'STK', 'put_call': '', 'strike': None,
        'expiry': None, 'multiplier': 1, 'buy_sell': 'BUY', 'open_close': 'O',
        'quantity': 100, 'price': 30.0, 'proceeds': -3000.0, 'commission': -1.0,
        'trade_ts': '2026-06-01T10:00:00', 'codes': '',
    }

    def test_insert_dedupe(self, tmp_path):
        db = make_db(tmp_path)
        assert db.insert_trades([self.TRADE]) == 1
        assert db.insert_trades([self.TRADE]) == 0  # idempotent re-import
        assert db.trade_count() == 1

    def test_get_ordered(self, tmp_path):
        db = make_db(tmp_path)
        second = {**self.TRADE, 'exec_id': 'T2', 'trade_ts': '2026-06-02T10:00:00'}
        db.insert_trades([second, self.TRADE])
        trades = db.get_trades()
        assert [t['exec_id'] for t in trades] == ['T1', 'T2']


class TestTranchesStorage:
    def test_replace_roundtrip(self, tmp_path):
        db = make_db(tmp_path)
        tranches = [{'id': 1, 'symbol': 'AA', 'qty': 100,
                     'opened_ts': '2026-06-01T10:00:00', 'open_price': 30.0,
                     'open_source': 'BUY', 'closed_ts': None, 'close_price': None,
                     'close_source': None, 'status': 'OPEN', 'premium': 99.0,
                     'realized_pl': None,
                     'covering_call': {'strike': 32.0, 'expiry': '2026-08-21'},
                     'inferred': 0}]
        events = [{'tranche_id': 1, 'symbol': 'AA', 'exec_id': 'T1',
                   'event_type': 'OPEN', 'ts': '2026-06-01T10:00:00',
                   'amount': -3001.0, 'qty': 100, 'details': ''}]
        db.replace_tranches(tranches, events)
        stored = db.get_tranches()
        assert stored[0]['covering_call'] == {'strike': 32.0, 'expiry': '2026-08-21'}
        assert db.get_events()[0]['event_type'] == 'OPEN'
        # Replace wipes and rewrites
        db.replace_tranches([], [])
        assert db.get_tranches() == []


class TestLegacyImport:
    def test_imports_price_cache_and_watchlist(self, tmp_path, monkeypatch):
        from ttc_app import config
        import json
        legacy_cache = tmp_path / 'price_cache.json'
        legacy_cache.write_text(json.dumps({
            'last_updated': '2026-06-19T05:20:00',
            'prices': {'AA': {'last': 27.5, 'timestamp': '2026-06-19T05:20:00'}},
        }))
        legacy_watchlist = tmp_path / 'ttc_watchlist.json'
        legacy_watchlist.write_text(json.dumps({'WATCHLIST': ['AA', 'NVDA']}))
        monkeypatch.setattr(config, 'LEGACY_PRICE_CACHE_FILE', str(legacy_cache))
        monkeypatch.setattr(config, 'LEGACY_WATCHLIST_FILE', str(legacy_watchlist))
        monkeypatch.setattr(config, 'LEGACY_QUAL_FAILURES_FILE', str(tmp_path / 'nope.json'))
        monkeypatch.setattr(config, 'LEGACY_SETTINGS_FILE', str(tmp_path / 'nope2.json'))

        db = Database(path=str(tmp_path / 'fresh.db'))
        assert db.latest_prices()['AA']['last'] == 27.5
        assert db.get_watchlist() == ['AA', 'NVDA']


class TestExport:
    def test_export_writes_files(self, tmp_path):
        db = make_db(tmp_path)
        db.insert_trades([TestTrades.TRADE])
        written = db.export_to(str(tmp_path / 'export'))
        assert any(p.endswith('.db') for p in written)
        assert any('trades' in p and p.endswith('.csv') for p in written)
        for p in written:
            assert os.path.exists(p)
