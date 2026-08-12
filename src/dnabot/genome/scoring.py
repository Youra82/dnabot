# src/dnabot/genome/scoring.py
"""
Geteilte Scoring-Bausteine fuer evolver.py (Live-Aktivierung) und
database.py::get_genome_as_of() (Point-in-Time-Aktivierung fuer Backtests).
Beide Stellen implementierten frueher dieselbe Formel getrennt -- hier
zentralisiert, um genau die Art von Divergenz zu vermeiden, die schon
zwischen discovery.py und backtester.py aufgetreten ist (siehe
bugfix_dnabot_discovery_pathdep_winrate.md).

Kein Import von evolver.py/database.py hier -- vermeidet Zirkel-Importe
(evolver.py importiert database.py).
"""
import math


def wilson_lower_bound(wins: int, n: int, z: float = 1.645) -> float:
    """
    Konfidenzintervall-Untergrenze einer Winrate (Wilson-Score-Intervall,
    einseitig). Ersetzt einen festen `min_samples`-Cutoff: bestraft kleine
    Stichproben automatisch (n=1, 1 Win -> Untergrenze ~0.27, faellt bei
    min_winrate=0.45 durch), ohne einen willkuerlichen Schwellenwert zu
    brauchen. z=1.645 entspricht einer einseitigen ~95%-Konfidenz.

    Hintergrund: ein fester min_samples-Cutoff steckt in einer Zwickmuehle --
    zu niedrig (z.B. 2) laesst Rauschen durch (2 Beobachtungen = Muenzwurf-
    Winrate), zu hoch (z.B. 30) laesst bei der typischen Vorkommen-Sparsity
    der Genome-Discovery praktisch nichts mehr durch. Wilson-Score loest das
    strukturell statt nur den Cutoff-Wert zu verschieben.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def compute_score(winrate_lower_bound: float, avg_move_pct: float, effective_occ: float) -> float:
    """
    Score = Wilson-Untergrenze x Avg. Move (%) x log(1 + effective_occ).

    Nutzt bewusst die Wilson-Untergrenze statt der rohen Punktschaetzung der
    Winrate, damit auch das Ranking (nicht nur das Aktivierungs-Gate)
    unsichere Kleinstichproben-Genome nicht ueber robustere Genome mit
    mehr Vorkommen stellt. effective_occ = raw_occ x Decay -- aeltere
    Samples zaehlen fuers Decay weniger, die Wilson-Untergrenze selbst
    bleibt aber auf rohen (nicht decay-gewichteten) Vorkommen berechnet,
    da Konfidenz eine Frage der Beobachtungsmenge ist, nicht der Aktualitaet.
    """
    if effective_occ < 0.5:
        return 0.0
    return winrate_lower_bound * avg_move_pct * math.log(1.0 + effective_occ)
