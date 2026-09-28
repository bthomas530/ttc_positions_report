# Black-Scholes greeks for when IBKR doesn't send its own.
#
# Why this exists: Dad mostly looks at the app pre-market (his logs show 6-8am
# sessions). Outside regular hours IBKR streams no bid/ask for options and
# usually no model greeks either, so Delta/Theta/IV were blank on every
# contract exactly when he was planning the day. IBKR's modelGreeks remain the
# first choice; this fills the gaps from inputs we do have (underlying price,
# strike, time to expiry, and a volatility -- either IBKR's last-known IV for
# the contract or one implied from its mark).
#
# Deliberate simplifications, fine for "roughly how likely is this to finish
# ITM" but not for pricing: European exercise, no dividends, flat risk-free
# rate. Pure stdlib (math.erf) so the PyInstaller build needs nothing new.

import math

from datetime import datetime

import pytz

RISK_FREE_RATE = 0.04        # ~T-bill yield; delta barely moves with it
SECONDS_PER_YEAR = 365 * 24 * 3600
MIN_VOL, MAX_VOL = 0.01, 5.0

_EASTERN = pytz.timezone('America/New_York')


def years_to_expiry(expiry_iso, now=None):
    """Years from `now` until 4:00pm New York time on the expiry date.

    Equity options stop trading at the 4pm ET close on expiration day, so a
    contract expiring today still has hours of time value at 9am -- using
    whole calendar days (DTE 0) would call it already expired."""
    if not expiry_iso:
        return None
    try:
        day = datetime.strptime(expiry_iso[:10], '%Y-%m-%d')
    except ValueError:
        return None
    close = _EASTERN.localize(day.replace(hour=16, minute=0))
    now = now or datetime.now(pytz.utc)
    if now.tzinfo is None:
        now = pytz.utc.localize(now)
    return max(0.0, (close - now).total_seconds() / SECONDS_PER_YEAR)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S, K, T, sigma, r):
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def bs_price(S, K, T, sigma, right, r=RISK_FREE_RATE):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if right == 'C' else max(0.0, K - S)
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    if right == 'C':
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(price, S, K, T, right, r=RISK_FREE_RATE):
    """Volatility that reproduces `price`, by bisection (price is monotonic
    in vol). None when the price sits outside what any sane vol produces --
    e.g. a stale prev-close below intrinsic after the stock moved."""
    if not price or price <= 0 or S <= 0 or K <= 0 or not T or T <= 0:
        return None
    lo, hi = MIN_VOL, MAX_VOL
    if price > bs_price(S, K, T, hi, right, r):
        return None
    if price < bs_price(S, K, T, lo, right, r):
        # A deep-ITM option trading at (or a hair above) plain intrinsic sits
        # below the European no-arbitrage floor once interest is counted --
        # American exercise and dividends allow it. It's "all intrinsic, no
        # time value", i.e. the lowest vol; only a price under intrinsic
        # itself (a stale quote after a move) is truly unusable.
        intrinsic = max(0.0, S - K) if right == 'C' else max(0.0, K - S)
        return MIN_VOL if intrinsic > 0 and price >= intrinsic - 0.005 else None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, mid, right, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-5:
            break
    return 0.5 * (lo + hi)


def bs_greeks(S, K, T, sigma, right, r=RISK_FREE_RATE):
    """Per-share greeks for ONE LONG contract, in IBKR's conventions: theta is
    per calendar day (negative for a long option), vega per 1 vol point.
    At/after expiry: delta is 0 or +/-1 by moneyness, the rest 0."""
    if S <= 0 or K <= 0:
        return None
    if T is None or T <= 0 or not sigma or sigma <= 0:
        itm = S > K if right == 'C' else S < K
        delta = (1.0 if right == 'C' else -1.0) if itm else 0.0
        return {'delta': delta, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    sqrt_t = math.sqrt(T)
    gamma = _norm_pdf(d1) / (S * sigma * sqrt_t)
    vega = S * _norm_pdf(d1) * sqrt_t / 100.0
    decay = -S * _norm_pdf(d1) * sigma / (2 * sqrt_t)
    if right == 'C':
        delta = _norm_cdf(d1)
        theta = decay - r * K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = decay + r * K * math.exp(-r * T) * _norm_cdf(-d2)
    return {'delta': delta, 'gamma': gamma, 'theta': theta / 365.0, 'vega': vega}
