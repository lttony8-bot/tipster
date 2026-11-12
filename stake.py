def kelly(prob, price, bankroll, fraction=0.05, cap_pct=0.015):
    """
    fraction=0.05: Solo apuesta el 5% de lo que sugiere la fórmula de Kelly (altamente seguro).
    cap_pct=0.015: Nunca apuesta más del 1.5% de la banca total en un solo pick.
    """
    b = price - 1.0
    if b <= 0: return 0.0
    f = (prob * b - (1 - prob)) / b
    if f <= 0: return 0.0
    return round(min(bankroll * f * fraction, bankroll * cap_pct), 2)

def flat(bankroll, pct=0.01):
    return round(bankroll * pct, 2)