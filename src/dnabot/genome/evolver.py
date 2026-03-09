# src/dnabot/genome/evolver.py
# Self-Learning Evolution Engine
#
# Bewertet alle Genome nach ihrer statistischen Qualität — pro Markt-Regime.
#
# Per-Regime-Score-Formel:
#   score_regime = winrate_regime × avg_move_pct × log(1 + occ_regime)
#
# Bewertungslogik pro Regime (TREND, RANGE, NEUTRAL):
#   - Zu wenig Samples (<min_samples)    → Regime inaktiv
#   - Winrate < min_winrate              → Regime inaktiv
#   - Score < score_threshold            → Regime inaktiv
#   - Alles andere                       → Regime aktiv
#
# Ein Genome wird insgesamt aktiviert (active=1), wenn mindestens ein Regime aktiv ist.
# active_regimes = JSON-Liste der Regime, in denen das Genome gehandelt wird.
# Beispiel: active_regimes = '["RANGE", "NEUTRAL"]'

import math
import json
import logging

from dnabot.genome.database import GenomeDB

logger = logging.getLogger(__name__)

# Regime, die der Evolver bewertet (HIGH_VOL wird immer blockiert)
SCORED_REGIMES = ['TREND', 'RANGE', 'NEUTRAL']

# DB-Spalten für jedes Regime
_REGIME_COLS = {
    'TREND':   ('occ_trend',   'wins_trend'),
    'RANGE':   ('occ_range',   'wins_range'),
    'NEUTRAL': ('occ_neutral', 'wins_neutral'),
}


def compute_score(winrate: float, avg_move_pct: float, occurrences: int) -> float:
    """
    Berechnet den Genome-Score für ein einzelnes Regime.

    Score = Winrate × Avg. Move (%) × log(1 + Samples)

    Höherer Score = Pattern mit:
      - Hoher Trefferquote
      - Großen Durchschnitts-Moves
      - Vielen bestätigenden Samples
    """
    if occurrences < 1:
        return 0.0
    return winrate * avg_move_pct * math.log(1.0 + occurrences)


def evolve(
    db: GenomeDB,
    market: str = None,
    timeframe: str = None,
    min_samples: int = 100,
    min_winrate: float = 0.45,
    score_threshold: float = 0.08,
) -> dict:
    """
    Wertet alle Genome aus und aktiviert/deaktiviert sie basierend auf
    ihrer per-Regime-Performance.

    Args:
        db: GenomeDB-Instanz
        market: Optional — nur Genome für diesen Markt bewerten
        timeframe: Optional — nur Genome für diesen Timeframe bewerten
        min_samples: Mindestanzahl an Regime-Occurrences für Aktivierung
        min_winrate: Mindest-Winrate (z.B. 0.45 = 45%)
        score_threshold: Mindest-Score für Aktivierung

    Returns:
        dict mit Evolutions-Statistiken
    """
    genomes = db.get_all_genomes(market=market, timeframe=timeframe)

    activated = 0
    deactivated = 0
    total_regime_activations = {r: 0 for r in SCORED_REGIMES}

    for genome in genomes:
        gid = genome['genome_id']
        avg_move = genome['avg_move_pct']

        active_regimes = []
        best_score = 0.0

        for regime in SCORED_REGIMES:
            occ_col, wins_col = _REGIME_COLS[regime]
            occ = genome.get(occ_col, 0) or 0
            wins = genome.get(wins_col, 0) or 0

            if occ < min_samples:
                continue

            winrate = wins / occ
            score = compute_score(winrate, avg_move, occ)

            if winrate >= min_winrate and score >= score_threshold:
                active_regimes.append(regime)
                best_score = max(best_score, score)
                total_regime_activations[regime] += 1
                logger.debug(
                    f"[{regime}] Aktiv: {genome['sequence']} [{genome['direction']}] "
                    f"WR={winrate:.1%} Score={score:.3f} n={occ}"
                )

        is_active = len(active_regimes) > 0

        # Fallback-Score: global, wenn kein Regime genug Samples hat
        if best_score == 0.0:
            total = genome['total_occurrences'] or 0
            global_wins = genome['wins'] or 0
            global_winrate = global_wins / total if total > 0 else 0.0
            best_score = compute_score(global_winrate, avg_move, total)

        db.update_genome_evolution(gid, best_score, is_active, active_regimes)

        if is_active:
            activated += 1
            logger.debug(
                f"Aktiviert (Regime: {active_regimes}): "
                f"{genome['sequence']} [{genome['direction']}] {genome['market']}"
            )
        else:
            deactivated += 1

    logger.info(
        f"[Evolver] {market or 'alle'} ({timeframe or 'alle TF'}) | "
        f"Gesamt: {len(genomes)} | Aktiviert: {activated} | Deaktiviert: {deactivated} | "
        f"Regime-Aktivierungen: TREND={total_regime_activations['TREND']}, "
        f"RANGE={total_regime_activations['RANGE']}, "
        f"NEUTRAL={total_regime_activations['NEUTRAL']}"
    )

    return {
        "total": len(genomes),
        "activated": activated,
        "deactivated": deactivated,
        "regime_activations": total_regime_activations,
    }


def get_top_genomes(db: GenomeDB, market: str, timeframe: str, top_n: int = 20) -> list[dict]:
    """
    Gibt die besten aktiven Genome für einen Markt sortiert nach Score zurück.
    Nützlich für Reporting und Debugging.
    """
    genomes = db.get_active_genomes_for_market(market, timeframe)
    for g in genomes:
        g['winrate'] = g['wins'] / max(g['total_occurrences'], 1)
        try:
            g['active_regimes_list'] = json.loads(g.get('active_regimes', '[]'))
        except (json.JSONDecodeError, TypeError):
            g['active_regimes_list'] = []
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
    print(f"  Top 10 Patterns (aktiv, ≥100 Samples):")
    print("-" * 70)

    for i, p in enumerate(summary['top_patterns'], 1):
        winrate = p.get('winrate', 0)
        try:
            regimes = json.loads(p.get('active_regimes', '[]'))
        except (json.JSONDecodeError, TypeError):
            regimes = []
        print(
            f"  {i:2}. [{p['direction']:<5}] {p['sequence']}"
        )
        print(
            f"       Score: {p['score']:.3f} | "
            f"WR: {winrate:.1%} | "
            f"Avg Move: {p.get('avg_move_pct', 0):.2f}% | "
            f"n={p['total_occurrences']}"
        )
        print(f"       Regime: {', '.join(regimes) if regimes else '—'} | "
              f"Markt: {p['market']} | TF: {p['timeframe']}")
        print()

    print("=" * 70)
