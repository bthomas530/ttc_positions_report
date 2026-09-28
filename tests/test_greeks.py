import os
import sys
from datetime import datetime

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttc_app.greeks import bs_greeks, bs_price, implied_vol, years_to_expiry


class TestBlackScholes:
    # Hull's textbook case: S=K=100, T=1, sigma=20%, r=5%
    def test_prices_match_reference(self):
        assert bs_price(100, 100, 1, 0.2, 'C', 0.05) == pytest.approx(10.4506, abs=1e-4)
        assert bs_price(100, 100, 1, 0.2, 'P', 0.05) == pytest.approx(5.5735, abs=1e-4)

    def test_greeks_match_reference(self):
        g = bs_greeks(100, 100, 1, 0.2, 'C', 0.05)
        assert g['delta'] == pytest.approx(0.6368, abs=1e-4)
        assert g['gamma'] == pytest.approx(0.01876, abs=1e-4)
        assert g['theta'] == pytest.approx(-6.414 / 365, abs=1e-4)
        assert g['vega'] == pytest.approx(0.3752, abs=1e-4)
        put = bs_greeks(100, 100, 1, 0.2, 'P', 0.05)
        assert put['delta'] == pytest.approx(0.6368 - 1, abs=1e-4)

    def test_implied_vol_roundtrip(self):
        for right in ('C', 'P'):
            price = bs_price(250, 237.5, 30 / 365, 0.35, right)
            assert implied_vol(price, 250, 237.5, 30 / 365, right) == pytest.approx(0.35, abs=1e-3)

    def test_implied_vol_rejects_impossible_price(self):
        # Stale prev close below intrinsic after a big move
        assert implied_vol(1.0, 90, 100, 10 / 365, 'P') is None
        assert implied_vol(None, 90, 100, 10 / 365, 'P') is None
        assert implied_vol(1.0, 90, 100, 0, 'P') is None

    def test_at_expiry_delta_is_moneyness(self):
        assert bs_greeks(95, 100, 0, None, 'P')['delta'] == -1.0
        assert bs_greeks(105, 100, 0, None, 'P')['delta'] == 0.0
        assert bs_greeks(105, 100, 0, None, 'C')['delta'] == 1.0


class TestYearsToExpiry:
    def test_expiry_day_morning_still_has_time(self):
        # 9:00am ET (13:00 UTC, EDT) on expiry day -> 7 hours to the close
        now = pytz.utc.localize(datetime(2026, 9, 28, 13, 0))
        assert years_to_expiry('2026-09-28', now) * 365 * 24 == pytest.approx(7)

    def test_standard_time(self):
        # Dec: EST, 4pm ET = 21:00 UTC
        now = pytz.utc.localize(datetime(2026, 12, 18, 20, 0))
        assert years_to_expiry('2026-12-18', now) * 365 * 24 == pytest.approx(1)

    def test_after_close_is_zero(self):
        now = pytz.utc.localize(datetime(2026, 9, 28, 22, 0))
        assert years_to_expiry('2026-09-28', now) == 0

    def test_bad_input(self):
        assert years_to_expiry(None) is None
        assert years_to_expiry('garbage') is None
