import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app import web
from ttc_app.web import get_ibkr_data


class FakeDB:
    def __init__(self, watchlist=None):
        self.watchlist = list(watchlist or [])
        self.recorded_prices = None
        self.recorded_options = None

    def get_watchlist(self):
        return list(self.watchlist)

    def set_watchlist(self, symbols):
        self.watchlist = list(symbols)

    def record_prices(self, market_data):
        self.recorded_prices = market_data

    def record_option_snapshots(self, options):
        self.recorded_options = options

    def latest_prices(self):
        return {}


class FakeIBKR:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_snapshot(self, watchlist_symbols):
        return self.snapshot


def make_snapshot(positions, market_data, options=None):
    return {
        'positions_raw': positions,
        'market_data': market_data,
        'options': options or [],
        'failed_symbols': [],
    }


def setup_state(snapshot, watchlist=None):
    web.state.db = FakeDB(watchlist)
    web.state.ibkr = FakeIBKR(snapshot)


def teardown_module():
    web.state.db = None
    web.state.ibkr = None


def position_row(data, symbol):
    return next((p for p in data['positions'] if p['symbol'] == symbol), None)


class TestGetIbkrData:
    def test_stock_with_options(self):
        setup_state(make_snapshot(
            positions=[
                {'symbol': 'AAPL', 'secType': 'STK', 'position': 200,
                 'avgCost': 150.0},
                {'symbol': 'AAPL', 'secType': 'OPT', 'position': -2,
                 'right': 'C'},
            ],
            market_data={'AAPL': {'last': 190.0}},
        ))
        data = get_ibkr_data()
        row = position_row(data, 'AAPL')
        assert row['shares'] == 200
        assert row['covered_calls'] == 2
        assert row['naked_puts'] == 0
        assert 'AAPL' not in data['watchlist']

    def test_short_put_without_stock_gets_position_row(self):
        # The original bug: cash-secured short puts on a symbol with no
        # stock position vanished into the watchlist.
        setup_state(make_snapshot(
            positions=[
                {'symbol': 'NVDA', 'secType': 'OPT', 'position': -3,
                 'right': 'P'},
            ],
            market_data={'NVDA': {'last': 120.0}},
        ))
        data = get_ibkr_data()
        row = position_row(data, 'NVDA')
        assert row is not None
        assert row['shares'] == 0
        assert row['avgCost'] == 0
        assert row['marketPrice'] == 120.0
        assert row['naked_puts'] == 3
        assert row['covered_calls'] == 0
        assert row['uncovered_calls'] == 0
        assert 'NVDA' not in data['watchlist']
        assert data['incomplete_lots'] == []

    def test_short_call_without_stock_is_uncovered(self):
        setup_state(make_snapshot(
            positions=[
                {'symbol': 'TSLA', 'secType': 'OPT', 'position': -1,
                 'right': 'C'},
            ],
            market_data={'TSLA': {'last': 250.0}},
        ))
        data = get_ibkr_data()
        row = position_row(data, 'TSLA')
        assert row is not None
        assert row['shares'] == 0
        assert row['covered_calls'] == 0
        assert row['uncovered_calls'] == 1

    def test_mixed_portfolio_keeps_watchlist_only_symbols(self):
        setup_state(make_snapshot(
            positions=[
                {'symbol': 'AAPL', 'secType': 'STK', 'position': 100,
                 'avgCost': 150.0},
                {'symbol': 'NVDA', 'secType': 'OPT', 'position': -1,
                 'right': 'P'},
            ],
            market_data={
                'AAPL': {'last': 190.0},
                'NVDA': {'last': 120.0},
                'MSFT': {'last': 400.0},
            },
        ), watchlist=['MSFT'])
        data = get_ibkr_data()
        symbols = [p['symbol'] for p in data['positions']]
        assert 'AAPL' in symbols
        assert 'NVDA' in symbols
        assert data['watchlist'] == ['MSFT']

    def test_option_only_symbol_added_to_watchlist_db(self):
        # Option-only symbols still join the persisted watchlist so the
        # price-fallback chain covers them when IBKR is down.
        setup_state(make_snapshot(
            positions=[
                {'symbol': 'NVDA', 'secType': 'OPT', 'position': -1,
                 'right': 'P'},
            ],
            market_data={'NVDA': {'last': 120.0}},
        ))
        data = get_ibkr_data()
        assert 'NVDA' in web.state.db.watchlist
        # ...but not shown as a watchlist row, since it has a position row.
        assert 'NVDA' not in data['watchlist']
