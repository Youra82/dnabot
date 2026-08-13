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


def breakeven_winrate(rr_ratio: float, margin_pct: float = 0.05) -> float:
    """
    Mindest-Winrate, die ein Genome mit gegebenem Risk:Reward-Verhaeltnis
    (rr_ratio = Ø Gewinn / Ø Verlust bei TP/SL-Treffer) braucht, um profitabel
    zu sein -- plus Sicherheitspuffer fuer Gebuehren/Slippage.

    Herleitung (Erwartungswert = 0):
        WR * rr_ratio - (1 - WR) = 0  =>  WR = 1 / (1 + rr_ratio)

    z.B. rr_ratio=2.0 -> Breakeven bei 33.3%, plus margin_pct (Standard 5
    Prozentpunkte) ergibt eine Aktivierungsschwelle von ~38.3% statt einer
    pauschalen, vom tatsaechlich konfigurierten R:R unabhaengigen Zahl.
    """
    if rr_ratio <= 0:
        return 1.0
    return 1.0 / (1.0 + rr_ratio) + margin_pct


def compute_score(winrate: float, avg_move_pct: float, effective_occ: float) -> float:
    """
    Score = Winrate x Avg. Move (%) x log(1 + effective_occ).

    effective_occ = raw_occ x Decay -- aeltere Samples zaehlen fuers Decay
    weniger.
    """
    if effective_occ < 0.5:
        return 0.0
    return winrate * avg_move_pct * math.log(1.0 + effective_occ)
