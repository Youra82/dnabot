# src/dnabot/genome/discovery.py
# Pattern Discovery Engine — das Herzstück des dnabot
#
# Ablauf:
#   1. OHLCV-Daten laden und Kerzen codieren
#   2. Sliding-Window über alle Kerzen (Längen 4, 5, 6)
#   3. Für jedes Fenster: Was passierte danach? (Horizon-Kerzen)
#   4. LONG-Outcome: max Up-Move > Threshold UND > max Down-Move
#   5. SHORT-Outcome: max Down-Move > Threshold UND > max Up-Move
#   6. Regime zum Zeitpunkt der Sequenz erfassen
#   7. Genome-DB aktualisieren

import logging
import pandas as pd

from dnabot.genome.encoder import encode_dataframe, genes_to_sequence_string
from dnabot.genome.database import GenomeDB
from dnabot.genome.regime import detect_regime

logger = logging.getLogger(__name__)

# Regime wird alle N Kerzen neu berechnet (Performance-Optimierung)
REGIME_RECALC_INTERVAL = 20


def discover_genomes(
    df: pd.DataFrame,
    market: str,
    timeframe: str,
    db: GenomeDB,
    sequence_lengths: list[int] = None,
    discovery_horizon: int = 5,
    move_threshold_pct: float = 1.0,
) -> dict:
    """
    Scannt historische OHLCV-Daten und entdeckt profitable Genome-Muster.

    Args:
        df: OHLCV DataFrame (index=Timestamp, Spalten: open, high, low, close, volume)
        market: Handelspaar z.B. "BTC/USDT:USDT"
        timeframe: Zeitrahmen z.B. "4h"
        db: GenomeDB-Instanz
        sequence_lengths: Fenstergrößen zu prüfen (Standard: [4, 5, 6])
        discovery_horizon: Wie viele Kerzen nach der Sequenz beobachtet werden
        move_threshold_pct: Mindest-Preisbewegung in % für ein gültiges Outcome

    Returns:
        dict mit Statistiken über den Discovery-Lauf
    """
    if sequence_lengths is None:
        sequence_lengths = [4, 5, 6]

    if len(df) < 60:
        logger.warning(f"Zu wenig Daten für {market} ({timeframe}): {len(df)} Kerzen. Minimum: 60.")
        return {"candles_processed": 0, "new_genomes": 0, "updated_genomes": 0}

    logger.info(
        f"[Discovery] {market} ({timeframe}) | {len(df)} Kerzen | "
        f"Horizon={discovery_horizon} | Threshold={move_threshold_pct}%"
    )

    # Alle Kerzen codieren
    genes = encode_dataframe(df)
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values

    new_genomes = 0
    updated_genomes = 0
    threshold_factor = move_threshold_pct / 100.0

    # Regime-Cache: wird alle REGIME_RECALC_INTERVAL Kerzen neu berechnet
    regime_cache = {}

    def get_regime_at(idx: int) -> str:
        bucket = (idx // REGIME_RECALC_INTERVAL) * REGIME_RECALC_INTERVAL
        if bucket not in regime_cache:
            sub_df = df.iloc[max(0, bucket - 50): bucket + 1]
            regime_cache[bucket] = detect_regime(sub_df)
        return regime_cache[bucket]

    # Für jede Sequenzlänge
    for seq_len in sequence_lengths:
        max_start = len(genes) - seq_len - discovery_horizon

        if max_start <= 0:
            logger.debug(f"  seq_len={seq_len}: Nicht genug Kerzen ({len(genes)}). Überspringe.")
            continue

        logger.debug(f"  Scanne seq_len={seq_len} | {max_start} Fenster...")

        for i in range(max_start):
            seq_genes = genes[i:i + seq_len]
            sequence = genes_to_sequence_string(seq_genes)

            entry_idx = i + seq_len
            entry_price = closes[entry_idx - 1]

            if entry_price <= 0:
                continue

            # Regime zum Zeitpunkt dieser Sequenz
            regime = get_regime_at(i)

            # Zukunft beobachten (strikt NACH Close der Sequenz — kein Lookahead)
            future_highs = highs[entry_idx: entry_idx + discovery_horizon]
            future_lows = lows[entry_idx: entry_idx + discovery_horizon]

            if len(future_highs) == 0:
                continue

            max_high = float(future_highs.max())
            min_low = float(future_lows.min())

            max_up_pct = (max_high - entry_price) / entry_price
            max_down_pct = (entry_price - min_low) / entry_price

            long_outcome = (max_up_pct >= threshold_factor) and (max_up_pct > max_down_pct)
            short_outcome = (max_down_pct >= threshold_factor) and (max_down_pct > max_up_pct)

            # Immer BEIDE Richtungen aufzeichnen — gibt realistische Win/Loss-Statistiken.
            # LONG gewinnt wenn Up > Threshold und Up > Down, sonst verliert LONG.
            # SHORT gewinnt wenn Down > Threshold und Down > Up, sonst verliert SHORT.
            for direction, is_win, move in [
                ("LONG",  long_outcome,  max_up_pct   * 100.0),
                ("SHORT", short_outcome, max_down_pct * 100.0),
            ]:
                is_new = db.upsert_genome_outcome(
                    sequence=sequence,
                    market=market,
                    timeframe=timeframe,
                    direction=direction,
                    seq_length=seq_len,
                    is_win=is_win,
                    move_pct=move,
                    regime=regime,
                )
                if is_new:
                    new_genomes += 1
                else:
                    updated_genomes += 1

    candles_processed = len(df)
    db.log_scan(market, timeframe, candles_processed, new_genomes, updated_genomes)

    logger.info(
        f"[Discovery] {market} ({timeframe}) abgeschlossen: "
        f"{candles_processed} Kerzen, {new_genomes} neue Genome, {updated_genomes} aktualisiert."
    )

    return {
        "candles_processed": candles_processed,
        "new_genomes": new_genomes,
        "updated_genomes": updated_genomes,
    }
