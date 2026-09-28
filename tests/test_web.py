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


def enhance_options(options, market_data=None, threshold=None):
    web.state.db = FakeDB()
    web.state.db.get_setting = lambda key, default=None: (
        threshold if threshold is not None else default)
    return web.enhance_with_market_data({
        'positions': [], 'incomplete_lots': [], 'watchlist': [],
        'market_data': market_data or {}, 'options': options,
    })['options_by_symbol']


def short_put(symbol, strike, dte, entry, mark, mark_source='live'):
    return {'symbol': symbol, 'right': 'P', 'strike': strike, 'dte': dte,
            'expiry': '2026-09-23', 'position': -5, 'entry_price': entry,
            'mark': mark, 'mark_source': mark_source}


class TestOptionAlerts:
    def test_expiring_puts_flagged_consistently(self):
        # The reported bug: four short puts all expiring today; AAPL (no
        # quote -> 0%) showed BUYBACK, AMZN/META/NVDA (stale $0.01) didn't.
        opts = enhance_options(
            [short_put('AAPL', 310, 0, 0.04, None, None),
             short_put('AMZN', 237.5, 0, 0.04, 0.01, 'prev_close'),
             short_put('META', 700, 0, 0.30, 0.10, 'ask')],
            market_data={'AAPL': {'last': 340.8}, 'AMZN': {'last': 254.4},
                         'META': {'last': 690.0}})
        rows = {s: r[0] for s, r in opts.items()}
        assert not any(r['buyback_target_hit'] for r in rows.values())
        assert all(r['expiring'] for r in rows.values())
        assert rows['AAPL']['itm'] is False and rows['AMZN']['itm'] is False
        assert rows['META']['itm'] is True
        assert rows['META']['cushion_pct'] < 0

    def test_missing_mark_is_none_and_never_triggers_buyback(self):
        row = enhance_options([short_put('AAPL', 310, 10, 1.00, None, None)])['AAPL'][0]
        assert row['mark'] is None
        assert row['premium_remaining_pct'] is None
        assert row['buyback_target_hit'] is False

    def test_buyback_still_fires_before_expiry(self):
        row = enhance_options([short_put('AAPL', 310, 10, 1.00, 0.10)],
                              market_data={'AAPL': {'last': 340}})['AAPL'][0]
        assert row['premium_remaining_pct'] == 10.0
        assert row['buyback_target_hit'] is True
        assert row['expiring'] is False
        assert row['itm'] is False

    def test_real_zero_mark_still_counts(self):
        row = enhance_options([short_put('AAPL', 310, 10, 1.00, 0.0)])['AAPL'][0]
        assert row['premium_remaining_pct'] == 0.0
        assert row['buyback_target_hit'] is True

    def test_call_moneyness(self):
        call = dict(short_put('AAPL', 350, 0, 5.0, 1.0), right='C')
        row = enhance_options([call], market_data={'AAPL': {'last': 355}})['AAPL'][0]
        assert row['itm'] is True and row['expiring'] is True

    def test_moneyness_unknown_without_price(self):
        row = enhance_options([short_put('XYZ', 50, 0, 1.0, 0.5)])['XYZ'][0]
        assert row['itm'] is None
