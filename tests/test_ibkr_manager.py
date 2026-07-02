import asyncio
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app.ibkr_manager import (
    BACKOFF_CAP,
    IBKRManager,
    classify_handshake_error,
    compute_backoff,
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


class TestProbe:
    def test_probe_closed_port(self):
        # 127.0.0.1:1 is essentially never listening
        results = probe_ib_ports([('127.0.0.1', 1, 'Nothing')], timeout=0.1)
        assert len(results) == 1
        assert results[0]['reachable'] is False
        assert results[0]['error']


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
