"""Black-Scholes greeks (fallback when the broker greeks API has no data)."""
import math

SQRT_2PI = math.sqrt(2 * math.pi)


def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_price(S, K, T, r, q, sigma, opt):
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "CE":
        return S * math.exp(-q * T) * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * math.exp(-q * T) * _cdf(-d1)


def implied_vol(price, S, K, T, r, q, opt):
    if price <= 0 or S <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if price < bs_price(S, K, T, r, q, lo, opt) or price > bs_price(S, K, T, r, q, hi, opt):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, q, mid, opt) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


def greeks(price, S, K, T, r, q, opt):
    """Returns dict iv(%), delta, gamma, theta (per day), vega (per 1% vol) or None."""
    sigma = implied_vol(price, S, K, T, r, q, opt)
    if sigma is None:
        return None
    sqT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    eqT, erT = math.exp(-q * T), math.exp(-r * T)
    gamma = eqT * _pdf(d1) / (S * sigma * sqT)
    vega = S * eqT * _pdf(d1) * sqT / 100
    if opt == "CE":
        delta = eqT * _cdf(d1)
        theta = (-S * eqT * _pdf(d1) * sigma / (2 * sqT) - r * K * erT * _cdf(d2) + q * S * eqT * _cdf(d1))
    else:
        delta = eqT * (_cdf(d1) - 1)
        theta = (-S * eqT * _pdf(d1) * sigma / (2 * sqT) + r * K * erT * _cdf(-d2) - q * S * eqT * _cdf(-d1))
    return {"iv": round(sigma * 100, 4), "delta": round(delta, 6), "gamma": round(gamma, 8),
            "theta": round(theta / 365, 6), "vega": round(vega, 6)}
