import math

def implied(prices):
    return [1.0 / p for p in prices]

def _mult(p):
    s = sum(p); return [x / s for x in p]

def _additive(p):
    n = len(p); ex = sum(p) - 1.0; return [x - ex / n for x in p]

def _power(p):
    lo, hi = 1e-6, 6.0
    for _ in range(120):
        k = (lo + hi) / 2
        if sum(x ** k for x in p) > 1.0: lo = k
        else: hi = k
    k = (lo + hi) / 2
    r = [x ** k for x in p]; s = sum(r); return [x / s for x in r]

def _shin(p):
    S = sum(p)
    def tot(z):
        if z >= 0.9999: return 1.0
        d = 2 * (1 - z)
        return sum((math.sqrt(z * z + 4 * (1 - z) * (x * x) / S) - z) / d for x in p)
    lo, hi = 0.0, 0.9999
    for _ in range(120):
        z = (lo + hi) / 2
        if tot(z) > 1.0: lo = z
        else: hi = z
    z = (lo + hi) / 2
    d = 2 * (1 - z) if z < 0.9999 else 2e-4
    r = [(math.sqrt(z * z + 4 * (1 - z) * (x * x) / S) - z) / d for x in p]
    s = sum(r); return [x / s for x in r]

def fair(prices):
    p = implied(prices)
    if len(p) < 2: return None
    sets = [_power(p), _additive(p), _shin(p)]
    out = [sum(c) / len(sets) for c in zip(*sets)]
    if any(x <= 0 for x in out): return _mult(p)
    return out

def overround(prices):
    return sum(implied(prices)) - 1.0