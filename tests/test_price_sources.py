import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app import price_sources
from ttc_app.price_sources import is_cusip, parse_cboe_quote


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
