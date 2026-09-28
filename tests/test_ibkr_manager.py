import asyncio
import math
import os
import sys

import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app.ibkr_manager import (
    fill_to_dict,
    order_to_dict,
    parse_liquid_hours,
    staged_close_price,
    account_summary,
    BACKOFF_CAP,
    IBKRManager,
    classify_handshake_error,
    compute_backoff,
    option_mark,
    probe_ib_ports,
    safe_price,
)


class TestClassifyHandshakeError:
    def test_client_id_in_use(self):
        assert classify_handshake_error(Exception('clientId 1 already in use')) == 'client_id_in_use'
        assert classify_handshake_error(Exception('Peer closed connection.')) == 'client_id_in_use'

    def test_timeout(self):
        assert classify_handshake_error(asyncio.TimeoutError()) == 'handshake_timeout'
        assert classify_handshake_error(Exception('API connection timed out')) == 'handshake_timeout'

    def test_unknown(self):
        assert classify_handshake_error(Exception('something else')) == 'unknown'


class TestComputeBackoff:
    def test_zero_failures(self):
        assert compute_backoff(0) == 0

    def test_grows_and_caps(self):
        assert 2 <= compute_backoff(1) <= 2.5
        assert 4 <= compute_backoff(2) <= 5
        for failures in (6, 10, 50):
            assert compute_backoff(failures) <= BACKOFF_CAP * 1.25


class TestSafePrice:
    def test_none(self):
        assert safe_price(None) == 0

    def test_nan_inf(self):
        assert safe_price(float('nan')) == 0
        assert safe_price(float('inf')) == 0

    def test_valid(self):
        assert safe_price(42.5) == 42.5
        assert safe_price('13.25') == 13.25


def FakeTicker(**fields):
    """A real ib_async Ticker with the given quote fields. Fields are set
    after construction because Ticker.__post_init__ resets them to "unset"
    (ib_async 2.x) -- and using the real class means option_mark() is
    tested against the library's actual hasBidAsk()/marketPrice() rules."""
    from ib_async import Ticker
    ticker = Ticker()
    for name, value in fields.items():
        setattr(ticker, name, value)
    return ticker


class TestOptionMark:
    def test_live_midpoint(self):
        assert option_mark(FakeTicker(bid=0.10, ask=0.20, bidSize=5, askSize=5)) == (
            pytest.approx(0.15), 'live')

    def test_no_bid_uses_ask(self):
        # Nearly worthless expiring put: no bid, penny ask, never traded today.
        assert option_mark(FakeTicker(bid=-1, ask=0.01, askSize=10)) == (0.01, 'ask')

    def test_falls_back_to_last_then_close(self):
        assert option_mark(FakeTicker(last=0.03)) == (0.03, 'last')
        assert option_mark(FakeTicker(close=0.02)) == (0.02, 'prev_close')

    def test_no_quote_is_none_not_zero(self):
        # The original bug: this came back as 0 -> "0% left" -> BUYBACK.
        assert option_mark(FakeTicker()) == (None, None)


class TestProbe:
    def test_probe_closed_port(self):
        # 127.0.0.1:1 is essentially never listening
        results = probe_ib_ports([('127.0.0.1', 1, 'Nothing')], timeout=0.1)
        assert len(results) == 1
        assert results[0]['reachable'] is False
        assert results[0]['error']

    def test_probe_resolves_pending_connect_via_select(self):
        # connect_ex() on a non-blocking socket returns EINPROGRESS/
        # WSAEWOULDBLOCK immediately for a handshake still in flight -- on
        # Windows this happens for genuinely open AND genuinely closed ports,
        # so the errno alone can't tell them apart (a prior fix that assumed
        # otherwise broke test_probe_closed_port on Windows CI). select()
        # must be used to wait for the real outcome via SO_ERROR.
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 36  # EINPROGRESS
        mock_sock.getsockopt.return_value = 0   # SO_ERROR: connected
        with patch('socket.socket', return_value=mock_sock), \
             patch('select.select', return_value=([], [mock_sock], [])):
            results = probe_ib_ports([('127.0.0.1', 7496, 'TWS Live')], timeout=0.1)
        assert results[0]['reachable'] is True
        assert results[0]['error'] is None

    def test_probe_still_rejects_connection_refused_via_select(self):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 36  # EINPROGRESS
        mock_sock.getsockopt.return_value = 61  # SO_ERROR: ECONNREFUSED (macOS)
        with patch('socket.socket', return_value=mock_sock), \
             patch('select.select', return_value=([], [], [mock_sock])):
            results = probe_ib_ports([('127.0.0.1', 7496, 'TWS Live')], timeout=0.1)
        assert results[0]['reachable'] is False
        assert results[0]['error'] == 'connection refused'

    def test_probe_true_timeout_when_select_never_resolves(self):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 36  # EINPROGRESS
        with patch('socket.socket', return_value=mock_sock), \
             patch('select.select', return_value=([], [], [])):
            results = probe_ib_ports([('127.0.0.1', 7496, 'TWS Live')], timeout=0.1)
        assert results[0]['reachable'] is False
        assert results[0]['error'] == 'timeout'


class TestManagerStatus:
    def test_initial_status_shape(self):
        manager = IBKRManager(client_id=555)
        status = manager.status()
        assert status['state'] == 'starting'
        assert status['connected'] is False
        assert status['client_id'] == 555
        assert status['subscriptions'] == 0

    def test_random_client_id_range(self):
        manager = IBKRManager()
        assert 100 <= manager.client_id <= 999

    def test_retry_in_seconds_without_schedule(self):
        assert IBKRManager().retry_in_seconds() == 0


class TestAccountSummary:
    def test_prefers_usd_and_skips_junk(self):
        from types import SimpleNamespace as AV
        values = [
            AV(tag='TotalCashValue', value='1000', currency='BASE'),
            AV(tag='TotalCashValue', value='900', currency='USD'),
            AV(tag='NetLiquidation', value='5000', currency='BASE'),
            AV(tag='AvailableFunds', value='oops', currency='USD'),
            AV(tag='TotalCashValue', value='7', currency='EUR'),
            AV(tag='SomethingElse', value='1', currency='USD'),
        ]
        assert account_summary(values) == {'cash': 900.0, 'net_liquidation': 5000.0}

    def test_empty(self):
        assert account_summary(None) == {}


class TestParseLiquidHours:
    HOURS = '20260928:0930-20260928:1600;20260929:CLOSED;20260930:0930-20260930:1300'

    def at(self, y, m, d, hh, mm):
        # given in UTC; Sep = EDT (UTC-4)
        import pytz
        return pytz.utc.localize(__import__('datetime').datetime(y, m, d, hh, mm))

    def test_open_and_closed_times(self):
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 9, 28, 14, 0)) is True
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 9, 28, 12, 0)) is False
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 9, 28, 20, 30)) is False

    def test_holiday_and_half_day(self):
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 9, 29, 15, 0)) is False
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 9, 30, 16, 30)) is True
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 9, 30, 17, 30)) is False

    def test_unknown_day_or_garbage(self):
        assert parse_liquid_hours(self.HOURS, 'US/Eastern', self.at(2026, 10, 5, 15, 0)) is None
        assert parse_liquid_hours('', None, self.at(2026, 9, 28, 15, 0)) is None


class TestStagedClosePrice:
    def test_buy_rounds_up_from_ask(self):
        assert staged_close_price('BUY', 0.03, 0.051, 0.04) == 0.06
        assert staged_close_price('BUY', 4.0, 4.12, 4.05) == 4.15
        assert staged_close_price('BUY', 0, 0.05, 0.03) == 0.05

    def test_buy_without_quotes(self):
        assert staged_close_price('BUY', 0, 0, 0.022) == 0.03
        assert staged_close_price('BUY', 0, 0, 0) == 0.01

    def test_sell_rounds_down_from_bid(self):
        assert staged_close_price('SELL', 3.12, 3.30, 3.2) == 3.10
        assert staged_close_price('SELL', 0, 0, 0) is None


class TestOrderAndFillDicts:
    def test_order(self):
        from types import SimpleNamespace as N
        trade = N(contract=N(conId=5, symbol='AAPL', secType='OPT', right='P', strike=310.0,
                             lastTradeDateOrContractMonth='20261016'),
                  order=N(orderId=7, permId=99, action='BUY', totalQuantity=5.0,
                          orderType='LMT', lmtPrice=0.05, tif='DAY', transmit=False,
                          orderRef='TTC staged close'),
                  orderStatus=N(status='Inactive', filled=0.0))
        d = order_to_dict(trade)
        assert d['expiry'] == '2026-10-16' and d['limit_price'] == 0.05
        assert d['transmitted'] is False and d['staged_by_app'] is True

    def test_fill_unset_values(self):
        from datetime import datetime
        from types import SimpleNamespace as N
        fill = N(contract=N(symbol='AAPL', secType='STK', right='', strike=0.0,
                            lastTradeDateOrContractMonth=''),
                 execution=N(execId='x1', time=datetime(2026, 9, 28, 14, 0), side='SLD',
                             shares=100.0, price=341.2, orderRef=''),
                 commissionReport=N(commission=1.0, realizedPNL=1.7976931348623157e+308))
        d = fill_to_dict(fill)
        assert d['side'] == 'SELL' and d['realized_pl'] is None and d['commission'] == 1.0
        assert d['expiry'] is None and d['strike'] is None


class TestStageCloseNeverTransmits:
    def make_mgr(self, position, log_errors=()):
        from types import SimpleNamespace as N
        placed = []
        contract = N(conId=42, secType='OPT', symbol='AAPL', localSymbol='AAPL 261016P00310000')

        class FakeIB:
            async def reqPositionsAsync(self):
                return [N(contract=contract, position=position)]

            def placeOrder(self, c, order):
                placed.append(order)
                return N(order=N(orderId=9, permId=1), orderStatus=N(status='Inactive'),
                         log=[N(errorCode=code, message=msg) for code, msg in log_errors])

        mgr = IBKRManager.__new__(IBKRManager)
        mgr._ib = FakeIB()
        mgr._opt_contracts = {42: contract}
        mgr._opt_tickers = {42: FakeTicker(bid=0.03, ask=0.05, bidSize=1, askSize=1)}
        mgr._last_snapshot = {'x': 1}
        return mgr, placed

    def test_short_position_stages_untransmitted_buy_for_full_size(self):
        mgr, placed = self.make_mgr(-5)
        result = asyncio.run(mgr._stage_close(42))
        assert result['ok'] and result['action'] == 'BUY' and result['quantity'] == 5
        order = placed[0]
        assert order.transmit is False
        assert order.action == 'BUY' and order.totalQuantity == 5 and order.lmtPrice == 0.05
        assert mgr._last_snapshot is None

    def test_long_position_sells(self):
        mgr, placed = self.make_mgr(3)
        asyncio.run(mgr._stage_close(42))
        assert placed[0].action == 'SELL' and placed[0].transmit is False

    def test_unknown_position_places_nothing(self):
        mgr, placed = self.make_mgr(-5)
        result = asyncio.run(mgr._stage_close(999))
        assert not result['ok'] and placed == []

    def test_read_only_api_error_explained(self):
        mgr, placed = self.make_mgr(-5, log_errors=[(321, 'API interface is currently in Read-Only mode.')])
        result = asyncio.run(mgr._stage_close(42))
        assert not result['ok'] and 'Read-Only API' in result['error']
