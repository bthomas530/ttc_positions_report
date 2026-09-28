import os
import sys

import pytest

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

    # Option-row extras (see web.option_context)
    trades = ()
    tranches = ()
    ivs = {}
    first_seen = {}

    def get_trades(self, symbol=None):
        return list(self.trades)

    def get_tranches(self, include_closed=True):
        return list(self.tranches)

    def latest_option_ivs(self, conids):
        return {c: v for c, v in self.ivs.items() if c in conids}

    def option_first_seen(self, conids):
        return {c: v for c, v in self.first_seen.items() if c in conids}


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


def enhance(options, market_data=None, settings=None, positions=None,
            account=None, **db_attrs):
    web.state.db = FakeDB()
    for k, v in db_attrs.items():
        setattr(web.state.db, k, v)
    settings = settings or {}
    web.state.db.get_setting = lambda key, default=None: settings.get(key, default)
    return web.enhance_with_market_data({
        'positions': positions or [], 'incomplete_lots': [], 'watchlist': [],
        'market_data': market_data or {}, 'options': options,
        'account': account or {},
    })


def enhance_options(options, market_data=None, **kw):
    return enhance(options, market_data, **kw)['options_by_symbol']


def short_put(symbol, strike, dte, entry, mark, mark_source='live'):
    from datetime import date, timedelta
    return {'symbol': symbol, 'right': 'P', 'strike': strike, 'dte': dte,
            'expiry': (date.today() + timedelta(days=dte)).isoformat(),
            'position': -5, 'entry_price': entry,
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


class TestGreeksFallback:
    def test_ibkr_greeks_pass_through(self):
        opt = dict(short_put('AAPL', 310, 10, 2.0, 1.0), delta=-0.2, theta=-0.05, iv=0.3)
        row = enhance_options([opt], {'AAPL': {'last': 320}})['AAPL'][0]
        assert row['delta'] == -0.2 and row['greeks_source'] == 'ibkr'

    def test_premarket_uses_last_known_iv(self):
        # No IBKR greeks and a stale prev-close mark: prefer IBKR's last IV
        opt = dict(short_put('AAPL', 310, 10, 2.0, 1.0, 'prev_close'), conId=42)
        row = enhance_options([opt], {'AAPL': {'last': 320}}, ivs={42: 0.3})['AAPL'][0]
        assert row['greeks_source'] == 'calc' and row['iv_source'] == 'last_known'
        assert row['iv'] == 0.3
        assert -0.5 < row['delta'] < 0
        assert row['theta'] < 0

    def test_live_mark_implies_vol(self):
        opt = dict(short_put('AAPL', 310, 10, 2.0, 1.5, 'live'), conId=42)
        row = enhance_options([opt], {'AAPL': {'last': 320}}, ivs={42: 0.9})['AAPL'][0]
        assert row['iv_source'] == 'implied'
        assert row['iv'] != 0.9

    def test_deep_itm_gets_delta_but_no_fake_iv(self):
        call = dict(short_put('MSFT', 420, 9, 4.0, 10.30, 'live'), right='C')
        row = enhance_options([call], {'MSFT': {'last': 430}})['MSFT'][0]
        assert row['delta'] == pytest.approx(1.0, abs=1e-3) and row['iv'] is None

    def test_no_vol_leaves_blank(self):
        opt = dict(short_put('AAPL', 310, 10, 2.0, None, None), conId=42)
        row = enhance_options([opt], {'AAPL': {'last': 320}})['AAPL'][0]
        assert row['delta'] is None and row['greeks_source'] is None


class TestPnlAndYield:
    def test_unrealized_pl_dollars(self):
        call = dict(short_put('AAPL', 350, 450, 5.35, 45.74), right='C', position=-30)
        row = enhance_options([call])['AAPL'][0]
        assert row['unrealized_pl'] == pytest.approx(-121170.0)
        put = enhance_options([short_put('AAPL', 310, 10, 2.0, 0.5)])['AAPL'][0]
        assert put['unrealized_pl'] == pytest.approx(750.0)

    def test_yield_from_trade_history(self):
        opt = short_put('AAPL', 100, 20, 1.0, 0.5)
        opt['expiry'] = '2026-10-16'
        trades = [{'sec_type': 'OPT', 'symbol': 'AAPL', 'put_call': 'P', 'strike': 100,
                   'expiry': '2026-10-16', 'quantity': -5, 'trade_ts': '2026-09-16T10:00:00'}]
        row = enhance_options([opt], trades=trades)['AAPL'][0]
        assert row['open_date'] == '2026-09-16' and row['open_date_source'] == 'trades'
        assert row['yield_ann'] == pytest.approx(round(100 * 1.0 / 100 * 365 / 30, 1))
        assert row['yield_left_ann'] == pytest.approx(round(100 * 0.5 / 100 * 365 / 20, 1))

    def test_yield_falls_back_to_first_seen(self):
        opt = dict(short_put('AAPL', 100, 20, 1.0, 0.5), conId=9)
        opt['expiry'] = '2026-10-16'
        row = enhance_options([opt], first_seen={9: '2026-10-06T09:00:00'})['AAPL'][0]
        assert row['open_date_source'] == 'first_seen'
        assert row['yield_ann'] == pytest.approx(36.5)

    def test_yield_left_uses_time_value_only(self):
        call = dict(short_put('MSFT', 420, 9, 4.0, 10.30), right='C')
        row = enhance_options([call], {'MSFT': {'last': 430}})['MSFT'][0]
        assert row['yield_left_ann'] == pytest.approx(round(100 * 0.30 / 420 * 365 / 9, 1))

    def test_no_yield_for_longs(self):
        opt = dict(short_put('AAPL', 100, 20, 1.0, 0.5), position=5)
        row = enhance_options([opt])['AAPL'][0]
        assert row['yield_ann'] is None and row['yield_left_ann'] is None


class TestAssignmentRisk:
    def test_near_and_itm_inside_window(self):
        rows = enhance_options(
            [short_put('AAA', 100, 5, 1.0, 0.5), short_put('BBB', 100, 5, 1.0, 0.5),
             short_put('CCC', 100, 5, 1.0, 0.5), short_put('DDD', 100, 30, 1.0, 0.5)],
            {'AAA': {'last': 101}, 'BBB': {'last': 99}, 'CCC': {'last': 120},
             'DDD': {'last': 99}})
        assert rows['AAA'][0]['assignment_risk'] == 'near'
        assert rows['BBB'][0]['assignment_risk'] == 'itm'
        assert rows['CCC'][0]['assignment_risk'] is None
        assert rows['DDD'][0]['assignment_risk'] is None   # outside 7d window

    def test_settings_respected_including_zero(self):
        rows = enhance_options([short_put('AAA', 100, 20, 1.0, 0.5)], {'AAA': {'last': 101}},
                               settings={'assignment_warn_pct': 0, 'assignment_warn_dte': 30})
        assert rows['AAA'][0]['assignment_risk'] is None
        rows = enhance_options([short_put('AAA', 100, 20, 1.0, 0.5)], {'AAA': {'last': 101}},
                               settings={'assignment_warn_pct': 2, 'assignment_warn_dte': 30})
        assert rows['AAA'][0]['assignment_risk'] == 'near'


class TestCoveredCallBasis:
    def lot(self, open_price, premium, qty=100):
        return {'open_price': open_price, 'premium': premium, 'qty': qty}

    def test_flags(self):
        f = web.covered_call_basis_flag
        assert f(95, [self.lot(100, 200)], None) == ('loss', 98.0)
        assert f(99, [self.lot(100, 200)], None) == ('stock_loss', 100.0)
        assert f(101, [self.lot(100, 200)], None) == (None, None)
        # worst lot drives it
        assert f(99, [self.lot(90, 0), self.lot(100, 0)], None) == ('loss', 100.0)
        # no tranche data -> IBKR average cost
        assert f(90, None, 95.0) == ('below_avg', 95.0)
        assert f(100, None, 95.0) == (None, None)

    def test_wired_into_call_rows(self):
        call = dict(short_put('AAPL', 95, 30, 1.0, 0.5), right='C', expiry='2026-10-16')
        lots = [{'symbol': 'AAPL', 'open_price': 100, 'premium': 200, 'qty': 100,
                 'covering_call': {'strike': 95, 'expiry': '2026-10-16'}}]
        row = enhance_options([call], tranches=lots)['AAPL'][0]
        assert row['basis_flag'] == 'loss' and row['basis_ref'] == 98.0
        row = enhance_options([call], positions=[{'symbol': 'AAPL', 'shares': 100, 'avgCost': 97, 'naked_puts': 0, 'covered_calls': 1, 'uncovered_calls': 0}])['AAPL'][0]
        assert row['basis_flag'] == 'below_avg'


class TestPortfolioSummary:
    def test_days_to_friday(self):
        from datetime import date
        assert web._days_to_friday(date(2026, 9, 28)) == 4   # Mon
        assert web._days_to_friday(date(2026, 10, 2)) == 0   # Fri
        assert web._days_to_friday(date(2026, 10, 3)) == 6   # Sat -> next Fri

    def test_exposure_and_week(self):
        from datetime import date
        data = enhance(
            [short_put('AAA', 100, 2, 1.0, 0.5), short_put('BBB', 50, 30, 1.0, 0.5),
             dict(short_put('CCC', 60, 1, 1.0, 0.5), right='C')],
            {'AAA': {'last': 99}, 'BBB': {'last': 60}, 'CCC': {'last': 50}},
            account={'cash': 100000.0})
        p = web.portfolio_summary(data['options_by_symbol'], {'cash': 100000.0},
                                  today=date(2026, 9, 28))
        assert p['put_exposure'] == 5 * 100 * 100 + 5 * 50 * 100
        assert p['put_exposure_pct_of_cash'] == 75.0
        week = p['expiring_week']
        assert week['contracts'] == 10 and week['itm'] == 5
        assert [i['symbol'] for i in week['items']] == ['CCC', 'AAA']
        assert p['expiring_week_through'] == '2026-10-02'

    def test_no_cash(self):
        p = web.portfolio_summary({}, {}, has_options_data=False)
        assert p['put_exposure_pct_of_cash'] is None and p['has_options_data'] is False


class TestTranchesBasisFlag:
    def test_api_marks_below_cost_lot(self, monkeypatch):
        lot = {'id': 1, 'symbol': 'AAPL', 'qty': 100, 'opened_ts': '2026-01-01T00:00:00',
               'open_price': 100.0, 'status': 'OPEN', 'premium': 200.0, 'realized_pl': None,
               'covering_call': {'strike': 95.0, 'expiry': '2026-10-16'}, 'inferred': 0}
        monkeypatch.setattr(web, 'rebuild_and_store_tranches', lambda: ([lot], []))
        monkeypatch.setattr(web, '_current_prices', lambda: {'AAPL': 99.0})
        web.state.db = FakeDB()
        web.state.db.trade_count = lambda: 0
        web.state.db.last_flex_import = lambda: None
        web.state.db.get_setting = lambda key, default=None: default
        with web.app.test_client() as c:
            body = c.get('/api/tranches').get_json()
        row = body['groups'][0]['open'][0]
        assert row['call_basis_flag'] == 'loss' and row['call_basis_ref'] == 98.0


class TestExDividendRisk:
    def row(self, mark, ex_days=3, und=110, expiry_days=10, amount=0.5, right='C'):
        from datetime import date, timedelta
        opt = dict(short_put('AAPL', 100, expiry_days, 2.0, mark), right=right)
        div = {'next_date': (date.today() + timedelta(days=ex_days)).isoformat(),
               'next_amount': amount}
        return enhance_options([opt], {'AAPL': {'last': und, 'dividend': div}})['AAPL'][0]

    def test_likely_when_extrinsic_below_dividend(self):
        r = self.row(mark=10.2)   # intrinsic 10, extrinsic 0.2 < 0.5
        assert r['exdiv_risk'] == 'likely' and r['extrinsic'] == pytest.approx(0.2)

    def test_possible_when_time_value_exceeds_dividend(self):
        assert self.row(mark=11.0)['exdiv_risk'] == 'possible'

    def test_none_cases(self):
        assert self.row(mark=1.0, und=95)['exdiv_risk'] is None          # OTM
        assert self.row(mark=10.2, ex_days=20)['exdiv_risk'] is None     # after expiry
        assert self.row(mark=10.2, ex_days=-1)['exdiv_risk'] is None     # already passed
        assert self.row(mark=10.2, right='P', und=90)['exdiv_risk'] is None


class TestFreshness:
    def at(self, h, m, day=28):
        import pytz
        from datetime import datetime
        return pytz.utc.localize(datetime(2026, 9, day, h, m))

    def test_stale_when_no_ticks_in_session(self):
        f = web.data_freshness('ibkr', {'open': True}, self.at(14, 0).isoformat(), now=self.at(14, 5))
        assert f['stale'] is True and f['tick_age_seconds'] == 300

    def test_fresh_and_closed(self):
        assert web.data_freshness('ibkr', {'open': True}, self.at(14, 4).isoformat(),
                                  now=self.at(14, 5))['stale'] is False
        # holiday per IBKR calendar: silence is expected
        assert web.data_freshness('ibkr', {'open': False}, None, now=self.at(14, 5))['stale'] is False

    def test_clock_fallback_and_non_ibkr(self):
        f = web.data_freshness('ibkr', {}, None, now=self.at(14, 5))   # Mon 10:05 ET
        assert f['market_open'] is True and f['market_open_source'] == 'clock' and f['stale']
        assert web.data_freshness('yahoo', {'open': True}, None, now=self.at(14, 5))['stale'] is False


class TestOrdersAttach:
    def test_orders_matched_to_contract(self):
        order = {'con_id': 42, 'action': 'BUY', 'quantity': 5, 'status': 'Submitted'}
        opt = dict(short_put('AAPL', 310, 10, 2.0, 1.0), conId=42)
        other = dict(short_put('AAPL', 300, 10, 2.0, 1.0), conId=43)
        data = enhance([opt, other])
        assert data['open_orders'] is None
        web.state.db = FakeDB()
        web.state.db.get_setting = lambda key, default=None: default
        data = web.enhance_with_market_data({
            'positions': [], 'incomplete_lots': [], 'watchlist': [], 'market_data': {},
            'options': [opt, other], 'open_orders': [order], 'fills_today': []})
        rows = {r['conId']: r for r in data['options_by_symbol']['AAPL']}
        assert rows[42]['working_orders'] == [order] and rows[43]['working_orders'] == []
        assert data['fills_today'] == []


class TestStageCloseRoute:
    def test_routes_to_manager(self):
        calls = []

        class Mgr:
            def stage_close_order(self, conid):
                calls.append(conid)
                return {'ok': True, 'description': 'BUY 5 X @ 0.05 LMT'}
        web.state.ibkr = Mgr()
        with web.app.test_client() as c:
            r = c.post('/api/orders/stage-close', json={'conId': '42'})
            assert r.status_code == 200 and calls == [42]
            assert c.post('/api/orders/stage-close', json={}).status_code == 400

    def test_manager_refusal_is_409(self):
        class Mgr:
            def stage_close_order(self, conid):
                return {'ok': False, 'error': 'read-only'}
        web.state.ibkr = Mgr()
        with web.app.test_client() as c:
            assert c.post('/api/orders/stage-close', json={'conId': 1}).status_code == 409
