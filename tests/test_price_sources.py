import os
import sys

from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import price_sources
from price_sources import QualFailureCache, is_cusip, parse_cboe_quote


class TestIsCusip:
    def test_ticker(self):
        assert is_cusip('AAPL') is False
        assert is_cusip('NVDA') is False

    def test_cusip(self):
        assert is_cusip('912828YK0') is True

    def test_exchange_suffix_ticker(self):
        assert is_cusip('WBD.TEN') is False


class TestCboeParsing:
    def test_valid_quote(self):
        payload = {'data': {
            'symbol': 'AAPL', 'current_price': 294.45, 'price_change': 5.02,
            'open': 293.42, 'high': 296.59, 'low': 289.195,
            'prev_day_close': 294.38,
        }}
        result = parse_cboe_quote(payload)
        assert result['last'] == 294.45
        assert result['open'] == 293.42
        assert result['high'] == 296.59
        assert result['low'] == 289.195
        assert result['close'] == 294.38
        assert result['change'] == 5.02
        assert result['source'] == 'cboe'

    def test_change_derived_when_missing(self):
        payload = {'data': {'current_price': 100.0, 'prev_day_close': 98.0}}
        result = parse_cboe_quote(payload)
        assert abs(result['change'] - 2.0) < 1e-9

    def test_missing_price_rejected(self):
        assert parse_cboe_quote({'data': {'current_price': 0}}) is None
        assert parse_cboe_quote({'data': {}}) is None
        assert parse_cboe_quote({}) is None

    def test_fetch_skips_cusips(self, monkeypatch):
        calls = []
        monkeypatch.setattr(price_sources, '_urlopen',
                            lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError))
        assert price_sources.fetch_cboe_prices(['912828YK0']) == {}
        assert calls == []


class TestQualFailureCache:
    def test_records_and_expires(self, tmp_path):
        cache = QualFailureCache(str(tmp_path / 'qf.json'), ttl_hours=24)
        now = datetime(2026, 7, 1, 9, 0)
        cache.record_failure('PANR', 'qualification failed', now=now)

        assert cache.is_failed('PANR', now=now + timedelta(hours=1)) is True
        assert cache.is_failed('PANR', now=now + timedelta(hours=25)) is False
        assert cache.is_failed('AAPL', now=now) is False

    def test_success_clears(self, tmp_path):
        cache = QualFailureCache(str(tmp_path / 'qf.json'))
        cache.record_failure('PSTG')
        assert cache.is_failed('PSTG') is True
        cache.record_success('PSTG')
        assert cache.is_failed('PSTG') is False

    def test_persists_across_instances(self, tmp_path):
        path = str(tmp_path / 'qf.json')
        QualFailureCache(path).record_failure('AHH')
        assert QualFailureCache(path).is_failed('AHH') is True

    def test_fail_count_accumulates(self, tmp_path):
        cache = QualFailureCache(str(tmp_path / 'qf.json'))
        cache.record_failure('WBD.TEN')
        cache.record_failure('WBD.TEN')
        assert cache._entries['WBD.TEN']['fail_count'] == 2

    def test_corrupt_file_tolerated(self, tmp_path):
        path = tmp_path / 'qf.json'
        path.write_text('{not json')
        cache = QualFailureCache(str(path))
        assert cache.is_failed('AAPL') is False
