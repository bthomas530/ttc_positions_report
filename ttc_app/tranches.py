# Tranche/wheel engine: pure functions, no I/O.
#
# Rebuilds the full tranche picture from the immutable trades table every time
# (derived data is never patched incrementally). A tranche always matches a
# real purchase/assignment lot exactly (whatever size that trade actually was)
# — it is never pre-chunked into 100-share pieces up front. It only ever
# splits when something forces a boundary smaller than the whole lot: a
# covered call needs exactly 100 shares, or a sell/assignment closes fewer
# shares than the lot holds. Tracked through the wheel:
#
#   short put sold -> assigned  => tranche opened at the strike, put premium
#                                  attributed to the tranche
#   covered call sold           => call premium attributed to the covered tranche
#   call assigned / shares sold => tranche closed, realized P/L =
#                                  stock cash flows + attributed premiums
#
# Cash convention throughout: proceeds are signed (sells +, buys -) and
# commissions are negative, so "amount = proceeds + commission" is the cash
# effect of a trade and premiums/P&L are simple sums.

import logging

from datetime import datetime

logger = logging.getLogger(__name__)

LOT_SIZE = 100


def _codes(trade):
    return set(c.strip() for c in (trade.get('codes') or '').split(';') if c.strip())


def _amount(trade):
    return (trade.get('proceeds') or 0) + (trade.get('commission') or 0)


def _call_key(trade):
    return ('C', trade.get('strike'), trade.get('expiry'))


def _put_key(trade):
    return ('P', trade.get('strike'), trade.get('expiry'))


class _SymbolState:
    def __init__(self, symbol):
        self.symbol = symbol
        self.open_tranches = []           # FIFO
        self.put_ledgers = {}             # key -> {'contracts': n, 'cash': $}
        self.pending_put_assignments = [] # [{'contracts', 'premium', 'strike'}]
        self.pending_call_assignments = []# [{'contracts', 'key'}]


class _Engine:
    def __init__(self):
        self.tranches = []                # all tranches, open and closed
        self.events = []
        self.next_id = 1
        self.symbols = {}

    def state(self, symbol):
        if symbol not in self.symbols:
            self.symbols[symbol] = _SymbolState(symbol)
        return self.symbols[symbol]

    def event(self, symbol, event_type, ts, amount=0, qty=0, tranche=None,
              exec_id=None, details=''):
        self.events.append({
            'tranche_id': tranche['id'] if tranche else None,
            'symbol': symbol,
            'exec_id': exec_id,
            'event_type': event_type,
            'ts': ts,
            'amount': round(amount, 6),
            'qty': qty,
            'details': details,
        })

    def new_tranche(self, symbol, qty, ts, price, source, open_cash,
                    inferred=0):
        tranche = {
            'id': self.next_id,
            'symbol': symbol,
            'qty': qty,
            'opened_ts': ts,
            'open_price': price,
            'open_source': source,
            'closed_ts': None,
            'close_price': None,
            'close_source': None,
            'status': 'OPEN',
            'premium': 0.0,
            'realized_pl': None,
            'covering_call': None,
            'inferred': inferred,
            # internals, stripped before returning
            '_open_cash': open_cash,
            '_call_key': None,
        }
        self.next_id += 1
        self.tranches.append(tranche)
        self.state(symbol).open_tranches.append(tranche)
        return tranche

    # ---------- stock trades ----------

    def stock_buy(self, trade):
        symbol = trade['symbol']
        state = self.state(symbol)
        qty = int(abs(trade['quantity']))
        if qty == 0:
            return
        ts = trade['trade_ts']
        price = trade['price'] or 0
        assigned = 'A' in _codes(trade)
        source = 'PUT_ASSIGNMENT' if assigned else 'BUY'
        total_cash = _amount(trade)

        # One trade = one tranche, matching the real purchase/assignment lot
        # exactly -- never pre-chunked into 100-share pieces here. Coverage
        # splitting (when a covered call needs exactly 100 shares) happens
        # on demand in _call_sold, not eagerly at purchase time.
        tranche = self.new_tranche(symbol, qty, ts, price, source, total_cash)
        self.event(symbol, 'OPEN', ts, amount=total_cash, qty=qty,
                   tranche=tranche, exec_id=trade['exec_id'],
                   details=f'{source} @ {price}')

        if assigned:
            self._attach_assigned_put_premium(state, trade, [tranche], ts)

    def _attach_assigned_put_premium(self, state, trade, new_tranches, ts):
        """Attribute the assigned put's net premium to the tranche(s) it opened,
        proportional to each tranche's share of the total assigned quantity
        (in practice there's exactly one tranche, sized to a whole number of
        contracts, since assignment quantity is always a multiple of 100)."""
        total_qty = sum(t['qty'] for t in new_tranches)
        contracts_needed = total_qty // LOT_SIZE
        if contracts_needed == 0:
            return
        premium = 0.0
        remaining = contracts_needed
        price = trade['price'] or 0

        # Prefer assignments the OPT leg already announced (matching strike)
        pending = state.pending_put_assignments
        for entry in sorted(pending, key=lambda e: e['strike'] != price):
            if remaining <= 0:
                break
            take = min(entry['contracts'], remaining)
            premium += entry['premium'] * (take / entry['contracts'])
            entry['premium'] -= entry['premium'] * (take / entry['contracts'])
            entry['contracts'] -= take
            remaining -= take
        state.pending_put_assignments = [e for e in pending if e['contracts'] > 0]

        # Fall back to open put ledgers (OPT assignment row not seen yet)
        if remaining > 0:
            keys = sorted(state.put_ledgers.keys(),
                          key=lambda k: (k[1] != price, k[2] or ''))
            for key in keys:
                if remaining <= 0:
                    break
                ledger = state.put_ledgers[key]
                if ledger['contracts'] <= 0:
                    continue
                take = min(ledger['contracts'], remaining)
                share = ledger['cash'] * (take / ledger['contracts'])
                premium += share
                ledger['cash'] -= share
                ledger['contracts'] -= take
                remaining -= take

        if premium == 0:
            return
        for tranche in new_tranches:
            share = premium * (tranche['qty'] / total_qty)
            tranche['premium'] += share
            self.event(tranche['symbol'], 'PUT_ASSIGNED', ts, amount=share,
                       qty=1, tranche=tranche, exec_id=trade['exec_id'],
                       details='put premium carried into tranche')

    def stock_sell(self, trade):
        symbol = trade['symbol']
        state = self.state(symbol)
        qty = int(abs(trade['quantity']))
        if qty == 0:
            return
        ts = trade['trade_ts']
        price = trade['price'] or 0
        assigned = 'A' in _codes(trade)
        source = 'CALL_ASSIGNMENT' if assigned else 'SELL'
        total_cash = _amount(trade)

        # Called-away shares close the tranches covered by that call first
        order = list(state.open_tranches)
        if assigned:
            keys = [e['key'] for e in state.pending_call_assignments]
            state.pending_call_assignments = []

            def rank(tranche):
                key = tranche['_call_key']
                if key and key in keys:
                    return 0
                if key and key[1] == price:
                    return 1
                return 2
            order.sort(key=rank)

        remaining = qty
        for tranche in order:
            if remaining <= 0:
                break
            take = min(tranche['qty'], remaining)
            cash_share = total_cash * (take / qty)
            if take < tranche['qty']:
                tranche = self._split_tranche(tranche, take)
            self._close_tranche(tranche, ts, price, source, cash_share,
                                trade['exec_id'])
            remaining -= take

        if remaining > 0:
            self.event(symbol, 'UNMATCHED_SELL', ts,
                       amount=total_cash * (remaining / qty), qty=remaining,
                       exec_id=trade['exec_id'],
                       details='sold shares with no tracked tranche (pre-history?)')

    def _split_tranche(self, tranche, take_qty):
        """Split off take_qty shares into a new tranche; shrink the original."""
        fraction = take_qty / tranche['qty']
        part = dict(tranche)
        part['id'] = self.next_id
        self.next_id += 1
        part['qty'] = take_qty
        part['premium'] = tranche['premium'] * fraction
        part['_open_cash'] = tranche['_open_cash'] * fraction

        tranche['qty'] -= take_qty
        tranche['premium'] -= part['premium']
        tranche['_open_cash'] -= part['_open_cash']

        state = self.state(tranche['symbol'])
        index = state.open_tranches.index(tranche)
        state.open_tranches.insert(index, part)
        self.tranches.append(part)
        return part

    def _close_tranche(self, tranche, ts, price, source, close_cash, exec_id):
        tranche['closed_ts'] = ts
        tranche['close_price'] = price
        tranche['close_source'] = source
        tranche['status'] = 'CLOSED'
        tranche['realized_pl'] = tranche['_open_cash'] + close_cash + tranche['premium']
        tranche['covering_call'] = None
        tranche['_call_key'] = None
        state = self.state(tranche['symbol'])
        if tranche in state.open_tranches:
            state.open_tranches.remove(tranche)
        self.event(tranche['symbol'], 'CLOSE', ts, amount=close_cash,
                   qty=tranche['qty'], tranche=tranche, exec_id=exec_id,
                   details=f'{source} @ {price}')

    # ---------- option trades ----------

    def option_trade(self, trade):
        codes = _codes(trade)
        selling = trade['quantity'] < 0 or trade['buy_sell'] == 'SELL'
        opening = 'O' in trade['open_close']
        if trade['put_call'] == 'P':
            if selling and opening:
                self._put_sold(trade)
            elif 'A' in codes:
                self._put_assigned_leg(trade)
            elif 'Ep' in codes:
                self._put_resolved(trade, 'PUT_EXPIRED')
            else:
                self._put_resolved(trade, 'PUT_CLOSED')
        elif trade['put_call'] == 'C':
            if selling and opening:
                self._call_sold(trade)
            elif 'A' in codes:
                self._call_assigned_leg(trade)
            elif 'Ep' in codes:
                self._call_resolved(trade, 'CALL_EXPIRED')
            else:
                self._call_resolved(trade, 'CALL_CLOSED')

    def _put_sold(self, trade):
        state = self.state(trade['symbol'])
        key = _put_key(trade)
        contracts = int(abs(trade['quantity']))
        ledger = state.put_ledgers.setdefault(key, {'contracts': 0, 'cash': 0.0})
        ledger['contracts'] += contracts
        ledger['cash'] += _amount(trade)
        self.event(trade['symbol'], 'PUT_SOLD', trade['trade_ts'],
                   amount=_amount(trade), qty=contracts, exec_id=trade['exec_id'],
                   details=f"{trade.get('strike')} {trade.get('expiry')}")

    def _put_resolved(self, trade, event_type):
        """Put bought back or expired: reduce the ledger; the cash effect of a
        buyback lands as a symbol-level event (income), expiry is a no-op cash-wise."""
        state = self.state(trade['symbol'])
        key = _put_key(trade)
        contracts = int(abs(trade['quantity']))
        ledger = state.put_ledgers.get(key)
        amount = _amount(trade)
        if ledger:
            take = min(contracts, ledger['contracts'])
            if ledger['contracts'] > 0:
                ledger['cash'] -= ledger['cash'] * (take / ledger['contracts'])
            ledger['contracts'] -= take
        self.event(trade['symbol'], event_type, trade['trade_ts'],
                   amount=amount, qty=contracts, exec_id=trade['exec_id'],
                   details=f"{trade.get('strike')} {trade.get('expiry')}")

    def _put_assigned_leg(self, trade):
        """OPT side of a put assignment: stage premium for the paired STK BUY."""
        state = self.state(trade['symbol'])
        key = _put_key(trade)
        contracts = int(abs(trade['quantity']))
        ledger = state.put_ledgers.get(key)
        premium = 0.0
        if ledger and ledger['contracts'] > 0:
            take = min(contracts, ledger['contracts'])
            premium = ledger['cash'] * (take / ledger['contracts'])
            ledger['cash'] -= premium
            ledger['contracts'] -= take
        state.pending_put_assignments.append({
            'contracts': contracts,
            'premium': premium,
            'strike': trade.get('strike'),
        })

    def _call_sold(self, trade):
        """Covered call: attribute premium to exactly 100 shares per contract.
        A lot larger than 100 (a real purchase of e.g. 500 shares) is split
        on demand so only the covered slice becomes its own tranche -- the
        rest of the original purchase lot stays intact as one open tranche."""
        symbol = trade['symbol']
        state = self.state(symbol)
        key = _call_key(trade)
        contracts = int(abs(trade['quantity']))
        total = _amount(trade)
        per_contract = total / contracts if contracts else 0

        covered = 0
        while covered < contracts:
            candidate = next(
                (t for t in state.open_tranches
                 if t['_call_key'] is None and t['qty'] >= LOT_SIZE), None)
            if candidate is None:
                break
            if candidate['qty'] > LOT_SIZE:
                candidate = self._split_tranche(candidate, LOT_SIZE)
            candidate['_call_key'] = key
            candidate['covering_call'] = {'strike': trade.get('strike'),
                                          'expiry': trade.get('expiry')}
            candidate['premium'] += per_contract
            self.event(symbol, 'CALL_SOLD', trade['trade_ts'],
                       amount=per_contract, qty=1, tranche=candidate,
                       exec_id=trade['exec_id'],
                       details=f"{trade.get('strike')} {trade.get('expiry')}")
            covered += 1

        uncovered = contracts - covered
        if uncovered > 0:
            self.event(symbol, 'CALL_SOLD', trade['trade_ts'],
                       amount=per_contract * uncovered, qty=uncovered,
                       exec_id=trade['exec_id'],
                       details=f"uncovered {trade.get('strike')} {trade.get('expiry')}")

    def _call_resolved(self, trade, event_type):
        """Call bought back or expired: release coverage; buyback cash goes to
        the covered tranches, expiry just frees them."""
        symbol = trade['symbol']
        state = self.state(symbol)
        key = _call_key(trade)
        contracts = int(abs(trade['quantity']))
        total = _amount(trade)
        per_contract = total / contracts if contracts else 0

        released = 0
        for tranche in state.open_tranches:
            if released >= contracts:
                break
            if tranche['_call_key'] == key:
                tranche['_call_key'] = None
                tranche['covering_call'] = None
                tranche['premium'] += per_contract
                self.event(symbol, event_type, trade['trade_ts'],
                           amount=per_contract, qty=1, tranche=tranche,
                           exec_id=trade['exec_id'],
                           details=f"{trade.get('strike')} {trade.get('expiry')}")
                released += 1

        leftover = contracts - released
        if leftover > 0:
            self.event(symbol, event_type, trade['trade_ts'],
                       amount=per_contract * leftover, qty=leftover,
                       exec_id=trade['exec_id'],
                       details=f"uncovered {trade.get('strike')} {trade.get('expiry')}")

    def _call_assigned_leg(self, trade):
        """OPT side of a call assignment: remember the key so the paired STK
        SELL closes the right tranches first. Premium already sits on them."""
        state = self.state(trade['symbol'])
        state.pending_call_assignments.append({
            'contracts': int(abs(trade['quantity'])),
            'key': _call_key(trade),
        })


def rebuild_tranches(trades, current_positions=None):
    """Rebuild all tranches and events from trades (chronological order).

    current_positions: optional [{symbol, shares, avgCost}] from IBKR; any
    shares not explained by trade history become SEEDED tranches (inferred=1).
    Returns (tranches, events) matching the db schema.
    """
    engine = _Engine()

    for trade in trades:
        try:
            if trade['sec_type'] == 'STK':
                if trade['quantity'] > 0 or trade['buy_sell'] == 'BUY':
                    engine.stock_buy(trade)
                else:
                    engine.stock_sell(trade)
            elif trade['sec_type'] == 'OPT':
                engine.option_trade(trade)
        except Exception as e:
            logger.warning(f"Tranche engine skipped trade {trade.get('exec_id')}: {e}")

    if current_positions:
        _seed_unexplained(engine, current_positions)

    tranches = []
    for t in engine.tranches:
        clean = {k: v for k, v in t.items() if not k.startswith('_')}
        clean['premium'] = round(clean['premium'], 6)
        if clean['realized_pl'] is not None:
            clean['realized_pl'] = round(clean['realized_pl'], 6)
        tranches.append(clean)
    return tranches, engine.events


def _seed_unexplained(engine, current_positions):
    """Positions opened before the earliest Flex data get SEEDED tranches at
    IBKR's average cost, badged inferred so the UI can flag the estimate."""
    now_iso = datetime.now().isoformat()
    for position in current_positions:
        symbol = position['symbol']
        shares = int(position.get('shares') or 0)
        if shares <= 0:
            continue
        tracked = sum(t['qty'] for t in engine.state(symbol).open_tranches)
        missing = shares - tracked
        if missing <= 0:
            continue
        avg_cost = position.get('avgCost') or 0
        # One seeded tranche for the whole unexplained remainder -- there's no
        # real lot boundary to preserve here (pre-history), and on-demand
        # splitting in _call_sold will still carve out 100-share coverage
        # slices from it later if a covered call is written against part of it.
        tranche = engine.new_tranche(symbol, missing, None, avg_cost, 'SEEDED',
                                     -(missing * avg_cost), inferred=1)
        engine.event(symbol, 'OPEN', now_iso, amount=-(missing * avg_cost),
                     qty=missing, tranche=tranche,
                     details='seeded from IBKR avgCost (pre-history)')


#: which event_type values represent each way a sold option can resolve --
#: used for the Income tab's outcome breakdown (expired worthless vs bought
#: back early vs assigned). Not the same set as premium_types below: this is
#: about *how* a position resolved, not how much cash it moved.
OUTCOME_EVENT_TYPES = {
    'expired': {'PUT_EXPIRED', 'CALL_EXPIRED'},
    'bought_back': {'PUT_CLOSED', 'CALL_CLOSED'},
    'assigned': {'PUT_ASSIGNED'},
}


def income_summary(events, closed_tranches, weekly_goal=0, monthly_goal=0):
    """Aggregate premium income by ISO week/month/symbol, an outcome
    breakdown (expired/bought-back/assigned), goal-streaks, and assignment
    history. Realized stock P/L comes from closed tranches (which already
    include attributed premium — reported separately so the two aren't
    double-counted in the UI)."""
    premium_types = {'PUT_SOLD', 'PUT_CLOSED', 'CALL_SOLD', 'CALL_CLOSED',
                     'PUT_ASSIGNED'}
    weekly = {}
    monthly = {}
    by_symbol = {}
    outcomes = {k: {'count': 0, 'amount': 0.0} for k in OUTCOME_EVENT_TYPES}

    for event in events:
        event_type = event['event_type']
        amount = event['amount'] or 0

        for outcome, types in OUTCOME_EVENT_TYPES.items():
            if event_type in types:
                outcomes[outcome]['count'] += 1
                outcomes[outcome]['amount'] += amount

        if event_type not in premium_types or not event.get('ts'):
            continue
        # PUT_ASSIGNED re-attributes premium already counted at PUT_SOLD
        if event_type == 'PUT_ASSIGNED':
            continue
        try:
            when = datetime.fromisoformat(event['ts'])
        except ValueError:
            continue
        iso = when.isocalendar()
        week_label = f'{iso[0]}-W{iso[1]:02d}'
        month_label = when.strftime('%Y-%m')
        weekly[week_label] = weekly.get(week_label, 0) + amount
        monthly[month_label] = monthly.get(month_label, 0) + amount
        symbol = event.get('symbol')
        if symbol:
            by_symbol[symbol] = by_symbol.get(symbol, 0) + amount

    for outcome in outcomes.values():
        outcome['amount'] = round(outcome['amount'], 2)

    assignments = [e for e in events
                   if e['event_type'] in ('PUT_ASSIGNED', 'CLOSE')
                   and 'ASSIGNMENT' in (e.get('details') or '').upper()]

    realized = sum(t['realized_pl'] or 0 for t in closed_tranches)

    def streak(periods, goal):
        """Consecutive most-recent periods (newest first) meeting goal."""
        if goal <= 0:
            return None
        count = 0
        for _, amount in periods:
            if amount >= goal:
                count += 1
            else:
                break
        return count

    weekly_sorted = sorted(weekly.items(), reverse=True)
    monthly_sorted = sorted(monthly.items(), reverse=True)

    return {
        'weekly_premium': [{'period': k, 'amount': round(v, 2)} for k, v in weekly_sorted],
        'monthly_premium': [{'period': k, 'amount': round(v, 2)} for k, v in monthly_sorted],
        'realized_pl_closed': round(realized, 2),
        'assignments': assignments,
        'by_symbol': [{'symbol': s, 'premium': round(v, 2)}
                      for s, v in sorted(by_symbol.items(), key=lambda kv: -kv[1])],
        'outcomes': outcomes,
        'weekly_streak': streak(weekly_sorted, weekly_goal),
        'monthly_streak': streak(monthly_sorted, monthly_goal),
    }
