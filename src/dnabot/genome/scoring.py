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


def wilson_lower_bound(wins: int, occurrences: int, z: float = 1.96) -> float:
    """
    Untere Grenze des Wilson-Score-Konfidenzintervalls fuer eine Binomial-
    Trefferquote (Standard: 95%-Konfidenz, z=1.96).

    Ersetzt die reine Punktschaetzung (wins/occurrences) als Aktivierungs-
    Kriterium: bei kleinem n liegt die untere Grenze deutlich unter der
    Punktschaetzung (z.B. wins=2/occ=2 -> Punktschaetzung 100%, Wilson-
    Untergrenze nur ~34%), bei grossem n naehern sich beide an. Direkt
    begruendet durch research_dnabot_direction_calibration.md Fund C/O:
    kleine Stichproben (n=2) zeigten die schlechteste tatsaechliche OOS-
    Trefferquote trotz oft perfekter Punktschaetzung -- die reine Punkt-
    schaetzung belohnt genau die unzuverlaessigsten Genome am staerksten
    (winner's-curse-Effekt bei der Aktivierungsschwelle).

    Frueher (bis 2026-08-13) kurzzeitig im Einsatz, dann zugunsten von mehr
    Genome-Menge wieder auf die reine Punktschaetzung zurueckgesetzt (siehe
    evolver.py-Docstring) -- diese Session (2026-08-23, Fund C/O/Y) zeigte
    ueber 25 unabhaengige Tests konsistent, dass die Genome-Menge selbst kein
    nachweisbares Signal enthaelt und die Punktschaetzung genau deswegen
    irrefuehrend ist; Wiedereinfuehrung als evidenzbasierte Korrektur.
    """
    if occurrences <= 0:
        return 0.0
    n = float(occurrences)
    p = wins / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return max(0.0, (center - margin) / denom)


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


def kelly_multiplier(winrate: float, rr_ratio: float, threshold_winrate: float,
                      min_mult: float, max_mult: float, dampening: float = 0.3) -> float:
    """
    Kelly-Positionsgroesse als MULTIPLIKATOR (Edge-Realization: Kapitaleinsatz
    proportional zur gemessenen Kantenstaerke, statt pauschal gleich fuer
    jedes Signal). Normiert auf die Kelly-Fraction GENAU an der Aktivierungs-
    schwelle (threshold_winrate): ein Genome das gerade so aktiviert bekommt
    Multiplikator 1.0.

    Rohes Kelly waechst bei typischen rr_ratio-Werten (z.B. 2.0) so steil mit
    der Winrate, dass der reine Verhaeltnis-Multiplikator (kelly/kelly_at_
    threshold) schon knapp oberhalb der Schwelle jeden vernuenftigen Deckel
    saettigt (z.B. WR=50% bei Schwelle=38.3% ergibt bereits ~3.4x) -- das gilt
    unabhaengig davon, ob man das Ergebnis als absolute Risk% oder als
    Multiplikator ausdrueckt, es liegt an der Steilheit der Kelly-Formel
    selbst. `dampening` (0-1) daempft deshalb, wie stark der Multiplikator
    oberhalb von 1.0 mitwaechst: multiplier = 1 + (roh_multiplier - 1) *
    dampening. Bei dampening=0.3 verteilt sich der typische 40-85%-Winrate-
    Bereich sanft zwischen 1x und max_mult, statt fast ueberall am Deckel zu
    haengen.

    Zentralisiert hier (statt getrennt in trade_manager.py/backtester.py
    implementiert), damit Live-Sizing und Backtest-Simulation exakt dieselbe
    Formel verwenden -- sonst validiert der Backtest eine Positionsgroesse,
    die live gar nicht zustande kommt.
    """
    if rr_ratio <= 0:
        return 1.0
    kelly = winrate - (1.0 - winrate) / rr_ratio
    kelly_at_threshold = threshold_winrate - (1.0 - threshold_winrate) / rr_ratio
    if kelly_at_threshold <= 0:
        return 1.0
    raw_multiplier = kelly / kelly_at_threshold
    multiplier = 1.0 + (raw_multiplier - 1.0) * dampening
    return max(min_mult, min(multiplier, max_mult))


def kelly_risk_pct(winrate: float, rr_ratio: float, threshold_winrate: float,
                    min_mult: float, max_mult: float, fallback_risk_pct: float,
                    dampening: float = 0.3) -> float:
    """kelly_multiplier() als absolutes Risk% ausgedrueckt (fallback_risk_pct x Multiplikator)."""
    if rr_ratio <= 0:
        return fallback_risk_pct
    mult = kelly_multiplier(winrate, rr_ratio, threshold_winrate, min_mult, max_mult, dampening)
    return fallback_risk_pct * mult
