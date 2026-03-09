# src/dnabot/genome/evolver.py
# Self-Learning Evolution Engine
#
# Bewertet alle Genome nach ihrer statistischen Qualität — pro Markt-Regime.
#
# Score-Formel (mit Occurrence-Decay):
#   effective_occ = occ_regime × decay
#   score_regime  = winrate_regime × avg_move_pct × log(1 + effective_occ)
#
#   Semantik: Alte Samples verlieren "effektives Gewicht".
#   1000 Samples die 1 Jahr nicht mehr auftraten = ~130 effektive Samples.
#
# Decay-Formel (volatilitätsadjustiert):
#   effective_half_life = half_life_days / clamp(vol_factor, 0.5, 3.0)
#   decay = e^(−age_days / effective_half_life)
#
#   vol_factor = aktuelle ATR / ATR-MA des gescannten Symbols.
#   Hohe Volatilität → kürzere Halbwertszeit → Pattern altern schneller.
#   Niedrige Volatilität → längere Halbwertszeit → Pattern bleiben länger gültig.
#
# Zeitstempel:
#   last_seen (nur aus upsert_genome_outcome) = Basis für Decay.
#   last_updated (inkl. Evolver) = wird ignoriert für Decay.
#
# Bewertungslogik pro Regime (TREND, RANGE, NEUTRAL):
#   - raw occ_regime < min_samples  → Regime inaktiv (nicht genug echte Daten)
#   - winrate < min_winrate         → Regime inaktiv
#   - score < score_threshold       → Regime inaktiv
#   - Alles andere                  → Regime aktiv
#
# active_regimes = JSON-Liste der Regime, in denen das Genome gehandelt wird.

import math
import json
import logging
from datetime import datetime, timezone

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


def compute_score(winrate: float, avg_move_pct: float, effective_occ: float) -> float:
    """
    Berechnet den Genome-Score basierend auf effektiven (decay-gewichteten) Occurrences.

    Score = Winrate × Avg. Move (%) × log(1 + effective_occ)

    effective_occ = raw_occ × decay  — ältere Samples zählen weniger.
    """
    if effective_occ < 0.5:
        return 0.0
    return winrate * avg_move_pct * math.log(1.0 + effective_occ)


def compute_decay(last_seen_iso: str, effective_half_life: float) -> float:
    """
    Berechnet den temporalen Decay-Faktor basierend auf last_seen.

    decay = e^(−age_days / effective_half_life)

    age_days = Tage seit letzter echter Beobachtung (last_seen).
    last_seen wird NUR durch upsert_genome_outcome gesetzt (Discovery/Live-Trades).
    Evolver-Updates ändern last_seen nicht → Decay akkumuliert korrekt.

    Args:
        last_seen_iso: ISO-8601 Timestamp aus DB-Feld last_seen
        effective_half_life: Halbwertszeit in Tagen (bereits vol-adjustiert)

    Returns:
        Decay-Faktor zwischen 0.0 und 1.0
    """
    if effective_half_life <= 0:
        return 1.0
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(timezone.utc) - last_seen).days, 0)
        return math.exp(-age_days / effective_half_life)
    except Exception:
        return 1.0  # Fallback: kein Decay bei ungültigem Timestamp


def evolve(
    db: GenomeDB,
    market: str = None,
    timeframe: str = None,
    min_samples: int = 100,
    min_winrate: float = 0.45,
    score_threshold: float = 0.08,
    half_life_days: float = 180.0,
    vol_factor: float = 1.0,
) -> dict:
    """
    Wertet alle Genome aus und aktiviert/deaktiviert sie basierend auf
    ihrer per-Regime-Performance mit Decay-Weighting.

    Args:
        db: GenomeDB-Instanz
        market: Optional — nur Genome für diesen Markt bewerten
        timeframe: Optional — nur Genome für diesen Timeframe bewerten
        min_samples: Mindestanzahl an RAW Regime-Occurrences für Aktivierung
        min_winrate: Mindest-Winrate (z.B. 0.45 = 45%)
        score_threshold: Mindest-Score für Aktivierung (nach Decay)
        half_life_days: Basis-Halbwertszeit für Decay (0 = kein Decay)
        vol_factor: ATR/ATR_MA des aktuellen Symbols — passt Halbwertszeit an.
                    > 1.0 = hohe Volatilität → schnellerer Decay
                    < 1.0 = niedrige Volatilität → langsamerer Decay

    Returns:
        dict mit Evolutions-Statistiken
    """
    # Vol-adjustierte Halbwertszeit: hohe Vol → Pattern altern schneller
    vol_factor_clamped = max(0.5, min(vol_factor, 3.0))
    effective_half_life = half_life_days / vol_factor_clamped if half_life_days > 0 else 0.0

    genomes = db.get_all_genomes(market=market, timeframe=timeframe)

    activated = 0
    deactivated = 0
    total_regime_activations = {r: 0 for r in SCORED_REGIMES}

    for genome in genomes:
        gid = genome['genome_id']
        avg_move = genome['avg_move_pct']

        # Decay basierend auf last_seen (nicht last_updated!)
        last_seen = genome.get('last_seen') or genome.get('last_updated', '')
        decay = compute_decay(last_seen, effective_half_life)

        active_regimes = []
        best_score = 0.0

        for regime in SCORED_REGIMES:
            occ_col, wins_col = _REGIME_COLS[regime]
            occ = genome.get(occ_col, 0) or 0
            wins = genome.get(wins_col, 0) or 0

            # Schwellwert auf rohe Occurrences (wir wollen echte Datenbasis)
            if occ < min_samples:
                continue

            winrate = wins / occ
            effective_occ = occ * decay
            score = compute_score(winrate, avg_move, effective_occ)

            if winrate >= min_winrate and score >= score_threshold:
                active_regimes.append(regime)
                best_score = max(best_score, score)
                total_regime_activations[regime] += 1
                logger.debug(
                    f"[{regime}] Aktiv: {genome['sequence']} [{genome['direction']}] "
                    f"WR={winrate:.1%} Score={score:.3f} "
                    f"(eff_occ={effective_occ:.0f}, decay={decay:.2f}) n={occ}"
                )

        is_active = len(active_regimes) > 0

        # Fallback-Score: global, wenn kein Regime genug Samples hat
        if best_score == 0.0:
            total = genome['total_occurrences'] or 0
            global_wins = genome['wins'] or 0
            global_winrate = global_wins / total if total > 0 else 0.0
            best_score = compute_score(global_winrate, avg_move, total * decay)

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
        f"half_life={half_life_days}d vol_factor={vol_factor_clamped:.2f} "
        f"eff_half_life={effective_half_life:.0f}d | "
        f"Regime: TREND={total_regime_activations['TREND']}, "
        f"RANGE={total_regime_activations['RANGE']}, "
        f"NEUTRAL={total_regime_activations['NEUTRAL']}"
    )

    return {
        "total": len(genomes),
        "activated": activated,
        "deactivated": deactivated,
        "regime_activations": total_regime_activations,
        "effective_half_life": effective_half_life,
    }


def get_top_genomes(db: GenomeDB, market: str, timeframe: str, top_n: int = 20) -> list[dict]:
    """
    Gibt die besten aktiven Genome für einen Markt sortiert nach Score zurück.
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
        print(f"  {i:2}. [{p['direction']:<5}] {p['sequence']}")
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
