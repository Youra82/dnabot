# src/dnabot/genome/evolver.py
# Self-Learning Evolution Engine
#
# Bewertet alle Genome nach ihrer statistischen Qualität.
# Schlechte Genome werden deaktiviert, gute aktiviert und höher gewichtet.
#
# Score-Formel:
#   score = winrate × avg_move_pct × log(1 + total_occurrences)
#
# Bewertungslogik:
#   - Zu wenig Samples (<min_samples)    → inaktiv (noch nicht bewertet)
#   - Winrate < min_winrate              → deaktiviert (statistisch schlecht)
#   - Score < score_threshold            → deaktiviert
#   - Alles andere                       → aktiviert

import math
import logging
from datetime import datetime, timezone

from dnabot.genome.database import GenomeDB

logger = logging.getLogger(__name__)


def compute_score(winrate: float, avg_move_pct: float, total_occurrences: int) -> float:
    """
    Berechnet den Genome-Score.

    Score = Winrate × Avg. Move (%) × log(1 + Samples)

    Höherer Score = Pattern mit:
      - Hoher Trefferquote
      - Großen Durchschnitts-Moves
      - Vielen bestätigenden Samples
    """
    if total_occurrences < 1:
        return 0.0
    return winrate * avg_move_pct * math.log(1.0 + total_occurrences)


def evolve(
    db: GenomeDB,
    market: str = None,
    timeframe: str = None,
    min_samples: int = 20,
    min_winrate: float = 0.45,
    score_threshold: float = 0.05,
) -> dict:
    """
    Wertet alle Genome aus und aktiviert/deaktiviert sie basierend auf ihrer Performance.

    Args:
        db: GenomeDB-Instanz
        market: Optional — nur Genome für diesen Markt bewerten
        timeframe: Optional — nur Genome für diesen Timeframe bewerten
        min_samples: Mindestanzahl an Occurrences für Aktivierung
        min_winrate: Mindest-Winrate (z.B. 0.45 = 45%)
        score_threshold: Mindest-Score für Aktivierung

    Returns:
        dict mit Evolutions-Statistiken
    """
    genomes = db.get_all_genomes(market=market, timeframe=timeframe)

    activated = 0
    deactivated_low_samples = 0
    deactivated_low_winrate = 0
    deactivated_low_score = 0
    score_updated = 0

    for genome in genomes:
        gid = genome['genome_id']
        total = genome['total_occurrences']
        wins = genome['wins']
        avg_move = genome['avg_move_pct']

        winrate = wins / total if total > 0 else 0.0
        score = compute_score(winrate, avg_move, total)

        if total < min_samples:
            # Noch nicht genug Daten → inaktiv lassen, Score trotzdem speichern
            db.update_genome_score(gid, score, active=False)
            deactivated_low_samples += 1

        elif winrate < min_winrate:
            # Statistisch schlechte Winrate
            db.update_genome_score(gid, score, active=False)
            deactivated_low_winrate += 1
            logger.debug(
                f"Deaktiviert (low winrate={winrate:.1%}): {genome['sequence']} "
                f"[{genome['direction']}] {genome['market']}"
            )

        elif score < score_threshold:
            # Score zu niedrig
            db.update_genome_score(gid, score, active=False)
            deactivated_low_score += 1

        else:
            # Gutes Genome → aktivieren
            db.update_genome_score(gid, score, active=True)
            activated += 1
            logger.debug(
                f"Aktiviert (score={score:.3f}, winrate={winrate:.1%}, "
                f"n={total}): {genome['sequence']} [{genome['direction']}]"
            )

        score_updated += 1

    total_deactivated = deactivated_low_samples + deactivated_low_winrate + deactivated_low_score

    logger.info(
        f"[Evolver] {market or 'alle'} ({timeframe or 'alle TF'}) | "
        f"Gesamt: {len(genomes)} | Aktiviert: {activated} | "
        f"Deaktiviert: {total_deactivated} "
        f"(Samples: {deactivated_low_samples}, "
        f"Winrate: {deactivated_low_winrate}, "
        f"Score: {deactivated_low_score})"
    )

    return {
        "total": len(genomes),
        "activated": activated,
        "deactivated_low_samples": deactivated_low_samples,
        "deactivated_low_winrate": deactivated_low_winrate,
        "deactivated_low_score": deactivated_low_score,
    }


def get_top_genomes(db: GenomeDB, market: str, timeframe: str, top_n: int = 20) -> list[dict]:
    """
    Gibt die besten aktiven Genome für einen Markt sortiert nach Score zurück.
    Nützlich für Reporting und Debugging.
    """
    genomes = db.get_active_genomes_for_market(market, timeframe)
    for g in genomes:
        g['winrate'] = g['wins'] / max(g['total_occurrences'], 1)
    return sorted(genomes, key=lambda x: x['score'], reverse=True)[:top_n]


def print_genome_report(db: GenomeDB, market: str = None, timeframe: str = None):
    """Gibt einen lesbaren Report der besten Genome aus."""
    summary = db.get_db_summary()

    print("\n" + "=" * 70)
    print(f"  GENOME LIBRARY REPORT — dnabot")
    print("=" * 70)
    print(f"  Gesamt-Genome:   {summary['total_genomes']}")
    print(f"  Aktive Genome:   {summary['active_genomes']}")
    print(f"  Märkte:          {', '.join(summary['markets'])}")
    print("=" * 70)
    print(f"  Top 10 Patterns (aktiv, ≥20 Samples):")
    print("-" * 70)

    for i, p in enumerate(summary['top_patterns'], 1):
        winrate = p.get('winrate', 0)
        print(
            f"  {i:2}. [{p['direction']:<5}] {p['sequence']}"
        )
        print(
            f"       Score: {p['score']:.3f} | "
            f"WR: {winrate:.1%} | "
            f"Avg Move: {p.get('avg_move_pct', 0):.2f}% | "
            f"n={p['total_occurrences']}"
        )
        print(f"       Markt: {p['market']} | TF: {p['timeframe']}")
        print()

    print("=" * 70)
