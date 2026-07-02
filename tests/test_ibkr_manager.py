import asyncio
import math
import os
import sys
from unittest.mock import MagicMock, patch

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
