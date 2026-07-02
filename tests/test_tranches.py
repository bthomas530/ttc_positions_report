import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app.tranches import income_summary, rebuild_tranches

_id_counter = [0]


def trade(**kwargs):
    """Trade-row factory with the cash conventions of the trades table."""
    _id_counter[0] += 1
    base = {
        'exec_id': f'E{_id_counter[0]}',
        'order_id': '', 'account': 'U1', 'symbol': 'AA', 'local_symbol': '',
        'sec_type': 'STK', 'put_call': '', 'strike': None, 'expiry': None,
        'multiplier': 1, 'buy_sell': 'BUY', 'open_close': 'O',
        'quantity': 0, 'price': 0.0, 'proceeds': 0.0, 'commission': 0.0,
        'trade_ts': '2026-06-01T10:00:00', 'codes': '',
    }
    base.update(kwargs)
    return base


def buy_stock(qty, price, ts, codes='', commission=-1.0):
    return trade(sec_type='STK', buy_sell='BUY', open_close='O', quantity=qty,
                 price=price, proceeds=-qty * price, commission=commission,
                 trade_ts=ts, codes=codes)


def sell_stock(qty, price, ts, codes='', commission=-1.0):
    return trade(sec_type='STK', buy_sell='SELL', open_close='C', quantity=-qty,
                 price=price, proceeds=qty * price, commission=commission,
                 trade_ts=ts, codes=codes)


def sell_option(put_call, strike, expiry, contracts, price, ts, commission=-1.0):
    return trade(sec_type='OPT', put_call=put_call, strike=strike, expiry=expiry,
                 multiplier=100, buy_sell='SELL', open_close='O',
                 quantity=-contracts, price=price,
                 proceeds=contracts * price * 100, commission=commission,
                 trade_ts=ts)


def close_option(put_call, strike, expiry, contracts, price, ts, codes='',
                 commission=-1.0):
    proceeds = -contracts * price * 100
    return trade(sec_type='OPT', put_call=put_call, strike=strike, expiry=expiry,
                 multiplier=100, buy_sell='BUY', open_close='C',
                 quantity=contracts, price=price, proceeds=proceeds,
                 trade_ts=ts, codes=codes,
                 commission=commission if price else 0.0)


class TestStockLots:
    def test_buy_splits_into_lots(self):
        tranches, events = rebuild_tranches([buy_stock(250, 40.0, '2026-06-01T10:00:00')])
        assert [t['qty'] for t in tranches] == [100, 100, 50]
        assert all(t['status'] == 'OPEN' for t in tranches)
        assert all(t['open_source'] == 'BUY' for t in tranches)
        assert len([e for e in events if e['event_type'] == 'OPEN']) == 3

    def test_fifo_close_and_realized_pl(self):
        tranches, _ = rebuild_tranches([
            buy_stock(100, 40.0, '2026-06-01T10:00:00', commission=-1.0),
            sell_stock(100, 42.0, '2026-06-08T10:00:00', commission=-1.0),
        ])
        t = tranches[0]
        assert t['status'] == 'CLOSED'
        assert t['close_source'] == 'SELL'
        # -4001 (buy) + 4199 (sell) = 198
        assert abs(t['realized_pl'] - 198.0) < 1e-6

    def test_partial_sell_splits_tranche(self):
        tranches, _ = rebuild_tranches([
            buy_stock(100, 40.0, '2026-06-01T10:00:00', commission=0.0),
            sell_stock(40, 44.0, '2026-06-08T10:00:00', commission=0.0),
        ])
        closed = [t for t in tranches if t['status'] == 'CLOSED']
        open_ = [t for t in tranches if t['status'] == 'OPEN']
        assert len(closed) == 1 and closed[0]['qty'] == 40
        assert len(open_) == 1 and open_[0]['qty'] == 60
        # closed part: open cash -1600, close cash +1760 -> +160
        assert abs(closed[0]['realized_pl'] - 160.0) < 1e-6

    def test_unmatched_sell_flagged(self):
        _, events = rebuild_tranches([sell_stock(100, 40.0, '2026-06-01T10:00:00')])
        assert any(e['event_type'] == 'UNMATCHED_SELL' for e in events)


class TestPuts:
    def test_put_expires_as_symbol_income(self):
        tranches, events = rebuild_tranches([
            sell_option('P', 30, '2026-06-19', 1, 2.00, '2026-06-01T10:00:00'),
            close_option('P', 30, '2026-06-19', 1, 0.0, '2026-06-19T16:00:00',
                         codes='Ep'),
        ])
        assert tranches == []  # never assigned, no tranche
        sold = next(e for e in events if e['event_type'] == 'PUT_SOLD')
        assert abs(sold['amount'] - 199.0) < 1e-6  # 200 - 1 commission
        assert any(e['event_type'] == 'PUT_EXPIRED' for e in events)

    def test_put_assignment_carries_premium_into_tranche(self):
        tranches, events = rebuild_tranches([
            sell_option('P', 30, '2026-06-19', 1, 2.00, '2026-06-01T10:00:00'),
            close_option('P', 30, '2026-06-19', 1, 0.0, '2026-06-19T16:00:00',
                         codes='A'),
            buy_stock(100, 30.0, '2026-06-19T16:00:01', codes='A'),
        ])
        assert len(tranches) == 1
        t = tranches[0]
        assert t['open_source'] == 'PUT_ASSIGNMENT'
        assert abs(t['premium'] - 199.0) < 1e-6
        assert any(e['event_type'] == 'PUT_ASSIGNED' for e in events)

    def test_put_buyback_reduces_income(self):
        _, events = rebuild_tranches([
            sell_option('P', 30, '2026-06-19', 1, 2.00, '2026-06-01T10:00:00'),
            close_option('P', 30, '2026-06-19', 1, 0.50, '2026-06-10T10:00:00'),
        ])
        closed = next(e for e in events if e['event_type'] == 'PUT_CLOSED')
        assert abs(closed['amount'] - (-51.0)) < 1e-6  # -50 - 1 commission


class TestCoveredCalls:
    def test_call_premium_attributed_and_coverage_marked(self):
        tranches, _ = rebuild_tranches([
            buy_stock(200, 30.0, '2026-06-01T10:00:00', commission=0.0),
            sell_option('C', 32.5, '2026-06-19', 1, 0.63, '2026-06-02T10:00:00',
                        commission=-1.0),
        ])
        covered = [t for t in tranches if t['covering_call']]
        uncovered = [t for t in tranches if not t['covering_call']]
        assert len(covered) == 1 and len(uncovered) == 1
        assert abs(covered[0]['premium'] - 62.0) < 1e-6  # 63 - 1
        assert covered[0]['covering_call'] == {'strike': 32.5, 'expiry': '2026-06-19'}

    def test_call_expiry_frees_coverage_keeps_premium(self):
        tranches, _ = rebuild_tranches([
            buy_stock(100, 30.0, '2026-06-01T10:00:00', commission=0.0),
            sell_option('C', 32.5, '2026-06-19', 1, 0.63, '2026-06-02T10:00:00',
                        commission=0.0),
            close_option('C', 32.5, '2026-06-19', 1, 0.0, '2026-06-19T16:00:00',
                         codes='Ep'),
        ])
        t = tranches[0]
        assert t['covering_call'] is None
        assert abs(t['premium'] - 63.0) < 1e-6

    def test_call_buyback_nets_against_premium(self):
        tranches, _ = rebuild_tranches([
            buy_stock(100, 30.0, '2026-06-01T10:00:00', commission=0.0),
            sell_option('C', 32.5, '2026-06-19', 1, 0.63, '2026-06-02T10:00:00',
                        commission=0.0),
            close_option('C', 32.5, '2026-06-19', 1, 0.20, '2026-06-10T10:00:00',
                         commission=0.0),
        ])
        t = tranches[0]
        assert t['covering_call'] is None
        assert abs(t['premium'] - 43.0) < 1e-6  # 63 - 20


class TestFullWheel:
    def test_put_assigned_then_called_away(self):
        """The complete wheel from the strategy notes: sell put -> assigned ->
        covered call -> called away. P/L = stock spread + both premiums."""
        tranches, _ = rebuild_tranches([
            # Sell 1 put 30 for $2.00 (net +199)
            sell_option('P', 30, '2026-06-19', 1, 2.00, '2026-06-01T10:00:00'),
            # Assigned: option leg + stock leg (net -3001)
            close_option('P', 30, '2026-06-19', 1, 0.0, '2026-06-19T16:00:00',
                         codes='A'),
            buy_stock(100, 30.0, '2026-06-19T16:00:01', codes='A'),
            # Sell covered call 32 for $1.00 (net +99)
            sell_option('C', 32, '2026-07-17', 1, 1.00, '2026-06-22T10:00:00'),
            # Called away: option leg + stock leg (net +3199)
            close_option('C', 32, '2026-07-17', 1, 0.0, '2026-07-17T16:00:00',
                         codes='A'),
            sell_stock(100, 32.0, '2026-07-17T16:00:01', codes='A'),
        ])
        assert len(tranches) == 1
        t = tranches[0]
        assert t['status'] == 'CLOSED'
        assert t['open_source'] == 'PUT_ASSIGNMENT'
        assert t['close_source'] == 'CALL_ASSIGNMENT'
        # -3001 + 3199 + 199 + 99 = 496
        assert abs(t['realized_pl'] - 496.0) < 1e-6

    def test_call_assignment_closes_covered_tranche_first(self):
        tranches, _ = rebuild_tranches([
            buy_stock(100, 28.0, '2026-06-01T10:00:00', commission=0.0),  # older, uncovered later
            buy_stock(100, 31.0, '2026-06-02T10:00:00', commission=0.0),
            # Call sold covers the FIFO-first tranche (the 28.0 one)
            sell_option('C', 32, '2026-07-17', 1, 1.00, '2026-06-03T10:00:00',
                        commission=0.0),
            close_option('C', 32, '2026-07-17', 1, 0.0, '2026-07-17T16:00:00',
                         codes='A'),
            sell_stock(100, 32.0, '2026-07-17T16:00:01', codes='A', commission=0.0),
        ])
        closed = [t for t in tranches if t['status'] == 'CLOSED']
        open_ = [t for t in tranches if t['status'] == 'OPEN']
        assert len(closed) == 1 and len(open_) == 1
        assert closed[0]['open_price'] == 28.0   # the covered tranche went
        assert open_[0]['open_price'] == 31.0
        # -2800 + 3200 + 100 = 500
        assert abs(closed[0]['realized_pl'] - 500.0) < 1e-6


class TestSeeding:
    def test_unexplained_shares_seeded(self):
        tranches, _ = rebuild_tranches(
            [buy_stock(100, 40.0, '2026-06-01T10:00:00')],
            current_positions=[{'symbol': 'AA', 'shares': 350, 'avgCost': 38.5}])
        seeded = [t for t in tranches if t['inferred']]
        tracked = [t for t in tranches if not t['inferred']]
        assert sum(t['qty'] for t in tracked) == 100
        assert sum(t['qty'] for t in seeded) == 250
        assert all(t['open_source'] == 'SEEDED' for t in seeded)
        assert all(t['open_price'] == 38.5 for t in seeded)

    def test_fully_explained_not_seeded(self):
        tranches, _ = rebuild_tranches(
            [buy_stock(100, 40.0, '2026-06-01T10:00:00')],
            current_positions=[{'symbol': 'AA', 'shares': 100, 'avgCost': 40.0}])
        assert not any(t['inferred'] for t in tranches)


class TestIncomeSummary:
    def test_weekly_aggregation(self):
        _, events = rebuild_tranches([
            sell_option('P', 30, '2026-06-19', 1, 2.00, '2026-06-01T10:00:00'),
            sell_option('C', 32, '2026-06-19', 1, 1.00, '2026-06-02T10:00:00'),
        ])
        summary = income_summary(events, [])
        # Both trades fall in ISO week 2026-W23
        weekly = {w['period']: w['amount'] for w in summary['weekly_premium']}
        assert abs(weekly['2026-W23'] - (199.0 + 99.0)) < 1e-6
        monthly = {m['period']: m['amount'] for m in summary['monthly_premium']}
        assert abs(monthly['2026-06'] - 298.0) < 1e-6

    def test_put_assigned_not_double_counted(self):
        _, events = rebuild_tranches([
            sell_option('P', 30, '2026-06-19', 1, 2.00, '2026-06-01T10:00:00'),
            close_option('P', 30, '2026-06-19', 1, 0.0, '2026-06-19T16:00:00',
                         codes='A'),
            buy_stock(100, 30.0, '2026-06-19T16:00:01', codes='A'),
        ])
        summary = income_summary(events, [])
        total = sum(w['amount'] for w in summary['weekly_premium'])
        assert abs(total - 199.0) < 1e-6  # premium counted once, at PUT_SOLD
