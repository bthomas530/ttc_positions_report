import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app import flex_client
from ttc_app.flex_client import (
    FlexError, fetch_statement, parse_trades, send_request,
)

SEND_OK = '''<FlexStatementResponse timestamp="01 July, 2026 10:00 AM EDT">
<Status>Success</Status>
<ReferenceCode>1234567890</ReferenceCode>
<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>
</FlexStatementResponse>'''

SEND_BAD_TOKEN = '''<FlexStatementResponse timestamp="01 July, 2026 10:00 AM EDT">
<Status>Fail</Status>
<ErrorCode>1015</ErrorCode>
<ErrorMessage>Token is invalid.</ErrorMessage>
</FlexStatementResponse>'''

IN_PROGRESS = '''<FlexStatementResponse timestamp="x">
<Status>Warn</Status>
<ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>'''

STATEMENT = '''<FlexQueryResponse queryName="TTC Trades" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20250701" toDate="20260701">
<Trades>
<Trade accountId="U1234567" assetCategory="STK" symbol="AA" quantity="100"
  tradePrice="30.00" proceeds="-3000" ibCommission="-1.0" buySell="BUY"
  openCloseIndicator="O" notes="A;O" tradeDate="20260601"
  dateTime="20260601;093500" tradeID="1001" ibOrderID="501" multiplier="1" />
<Trade accountId="U1234567" assetCategory="OPT" symbol="AA    260619P00030000"
  underlyingSymbol="AA" putCall="P" strike="30" expiry="20260619" multiplier="100"
  quantity="-1" tradePrice="2.00" proceeds="200" ibCommission="-1.1" buySell="SELL"
  openCloseIndicator="O" notes="" tradeDate="20260528" dateTime="20260528;101112"
  tradeID="1002" ibOrderID="502" />
<Trade accountId="U1234567" assetCategory="FUT" symbol="ESU6" quantity="1"
  tradePrice="5000" proceeds="-5000" ibCommission="-2" buySell="BUY"
  openCloseIndicator="O" notes="" tradeDate="20260528" tradeID="1003" />
<Trade accountId="U1234567" assetCategory="OPT" symbol="AA 260619C00032500"
  putCall="C" strike="32.5" expiry="20260619" multiplier="100"
  quantity="-1" tradePrice="0.63" proceeds="63" ibCommission="-1.1" buySell="SELL"
  openCloseIndicator="O" notes="" tradeDate="20260602" tradeID="1004" />
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>'''


class TestSendRequest:
    def test_success(self):
        ref, url = send_request('tok', '123', 'UA', http_get=lambda u, ua: SEND_OK)
        assert ref == '1234567890'
        assert url.endswith('GetStatement')

    def test_invalid_token(self):
        with pytest.raises(FlexError) as exc:
            send_request('bad', '123', 'UA', http_get=lambda u, ua: SEND_BAD_TOKEN)
        assert exc.value.code == '1015'
        assert 'token is invalid' in str(exc.value).lower()


class TestFetchStatement:
    def test_polls_until_ready(self):
        responses = [IN_PROGRESS, IN_PROGRESS, STATEMENT]
        calls = []

        def fake_get(url, ua):
            calls.append(url)
            return responses.pop(0)

        text = fetch_statement('https://x/GetStatement', 'tok', 'ref', 'UA',
                               http_get=fake_get, sleep=lambda s: None)
        assert '<FlexQueryResponse' in text
        assert len(calls) == 3

    def test_fatal_error_stops(self):
        with pytest.raises(FlexError) as exc:
            fetch_statement('https://x/G', 'tok', 'ref', 'UA',
                            http_get=lambda u, ua: SEND_BAD_TOKEN,
                            sleep=lambda s: None)
        assert exc.value.code == '1015'


class TestParseTrades:
    def test_parses_stk_and_opt_skips_fut(self):
        trades = parse_trades(STATEMENT)
        assert len(trades) == 3  # FUT skipped
        by_id = {t['exec_id']: t for t in trades}

        stk = by_id['1001']
        assert stk['sec_type'] == 'STK'
        assert stk['symbol'] == 'AA'
        assert stk['quantity'] == 100
        assert stk['codes'] == 'A;O'
        assert stk['trade_ts'] == '2026-06-01T09:35:00'

        put = by_id['1002']
        assert put['sec_type'] == 'OPT'
        assert put['symbol'] == 'AA'          # from underlyingSymbol
        assert put['put_call'] == 'P'
        assert put['strike'] == 30
        assert put['expiry'] == '2026-06-19'
        assert put['proceeds'] == 200

        # No underlyingSymbol attr: fall back to OCC symbol prefix
        call = by_id['1004']
        assert call['symbol'] == 'AA'

    def test_sorted_chronologically(self):
        trades = parse_trades(STATEMENT)
        timestamps = [t['trade_ts'] for t in trades]
        assert timestamps == sorted(timestamps)


class TestRunImport:
    class FakeDB:
        def __init__(self):
            self.trades = []
            self.imports = []

        def insert_trades(self, trades):
            new = [t for t in trades if t not in self.trades]
            self.trades.extend(new)
            return len(new)

        def record_flex_import(self, *args, **kwargs):
            self.imports.append(args)

    def test_full_import(self):
        db = self.FakeDB()
        responses = [SEND_OK, STATEMENT]
        result = flex_client.run_import(
            db, 'tok', '123', 'UA',
            http_get=lambda u, ua: responses.pop(0), sleep=lambda s: None)
        assert result['ok'] is True
        assert result['trade_count'] == 3
        assert result['new_count'] == 3
        assert db.imports[0][3] == 'ok'

    def test_error_recorded(self):
        db = self.FakeDB()
        result = flex_client.run_import(
            db, 'bad', '123', 'UA',
            http_get=lambda u, ua: SEND_BAD_TOKEN, sleep=lambda s: None)
        assert result['ok'] is False
        assert '1015' == result['code']
        assert db.imports[0][3] == 'error'
