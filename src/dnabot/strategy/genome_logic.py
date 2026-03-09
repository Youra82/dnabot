# src/dnabot/strategy/genome_logic.py
# Signal-Generator: Prüft aktuelle Marktstruktur gegen Genome-DB
#
# Ablauf:
#   1. Letzte 6 Kerzen codieren
#   2. Sequenzen der Länge 4, 5, 6 gegen DB prüfen
#   3. Bestes aktives Genome (höchster Score) als Signal zurückgeben
#
# Signal-Format:
#   {
#     "side": "long" | "short" | None,
#     "entry_price": float,
#     "sl_price": float,       — Low/High der Sequenz-Kerzen
#     "tp_price": float,       — 2:1 R:R
#     "genome_id": str,
#     "sequence": str,
#     "score": float,
#     "winrate": float,
#     "total_occurrences": int,
#     "seq_length": int,
#   }

import json
import logging
import pandas as pd
from typing import Optional

from dnabot.genome.encoder import encode_dataframe, genes_to_sequence_string
from dnabot.genome.database import GenomeDB
from dnabot.genome.regime import detect_regime, is_regime_allowed, REGIME_HIGH_VOL

logger = logging.getLogger(__name__)

# Wie viele Kerzen wir für ATR + Volume MA mindestens brauchen
MIN_CANDLES_REQUIRED = 35


def _build_signal(
    side: str,
    df: pd.DataFrame,
    genome: dict,
    rr_ratio: float = 2.0,
) -> dict:
    """
    Baut das Signal-Dict aus einem gefundenen Genome.

    SL = Low der gesamten Sequenz-Kerzen (für LONG)
         High der gesamten Sequenz-Kerzen (für SHORT)
    TP = Entry + rr_ratio × (Entry - SL)
    """
    seq_len = genome['seq_length']
    seq_candles = df.iloc[-seq_len:]

    last_close = float(df['close'].iloc[-1])
    winrate = genome['wins'] / max(genome['total_occurrences'], 1)

    if side == 'long':
        sl_price = float(seq_candles['low'].min())
        sl_distance = last_close - sl_price
        if sl_distance <= 0:
            sl_price = last_close * 0.98   # Fallback: 2%
            sl_distance = last_close - sl_price
        tp_price = last_close + (rr_ratio * sl_distance)

    else:  # short
        sl_price = float(seq_candles['high'].max())
        sl_distance = sl_price - last_close
        if sl_distance <= 0:
            sl_price = last_close * 1.02   # Fallback: 2%
            sl_distance = sl_price - last_close
        tp_price = last_close - (rr_ratio * sl_distance)

    sl_pct = (sl_distance / last_close) * 100.0

    logger.info(
        f"[Genome Signal] {side.upper()} | "
        f"Entry: {last_close:.4f} | SL: {sl_price:.4f} ({sl_pct:.2f}%) | "
        f"TP: {tp_price:.4f} | "
        f"Score: {genome['score']:.3f} | WR: {winrate:.1%} | "
        f"n={genome['total_occurrences']} | Seq: {genome['sequence']}"
    )

    return {
        "side": side,
        "entry_price": last_close,
        "sl_price": sl_price,
        "sl_pct": sl_pct,
        "tp_price": tp_price,
        "genome_id": genome['genome_id'],
        "sequence": genome['sequence'],
        "score": genome['score'],
        "winrate": winrate,
        "total_occurrences": genome['total_occurrences'],
        "seq_length": seq_len,
        "avg_move_pct": genome['avg_move_pct'],
    }


def get_genome_signal(
    df: pd.DataFrame,
    params: dict,
    db: GenomeDB,
) -> Optional[dict]:
    """
    Analysiert die letzten Kerzen und gibt das beste Genome-Signal zurück.

    Args:
        df: Aktueller OHLCV-DataFrame (ausreichend viele Kerzen)
        params: Config-Parameter (market, timeframe, genome, risk)
        db: GenomeDB-Instanz

    Returns:
        Signal-Dict oder None (kein Match gefunden)
    """
    if len(df) < MIN_CANDLES_REQUIRED:
        logger.warning(f"Zu wenig Kerzen ({len(df)}) für Genome-Matching. Minimum: {MIN_CANDLES_REQUIRED}.")
        return None

    market = params['market']['symbol']
    timeframe = params['market']['timeframe']
    min_score = params.get('genome', {}).get('min_score', 0.05)
    rr_ratio = params.get('risk', {}).get('rr_ratio', 2.0)
    sequence_lengths = params.get('genome', {}).get('sequence_lengths', [4, 5, 6])
    allowed_regimes = params.get('genome', {}).get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL'])

    # ── Regime-Filter ──────────────────────────────────────────────────────────
    current_regime = detect_regime(df)
    logger.info(f"[Regime] Aktuell: {current_regime}")

    if not is_regime_allowed(current_regime, allowed_regimes):
        logger.info(
            f"[Regime] {current_regime} nicht erlaubt "
            f"(erlaubt: {allowed_regimes}). Kein Signal."
        )
        return None
    # ──────────────────────────────────────────────────────────────────────────

    genes = encode_dataframe(df)

    if len(genes) < max(sequence_lengths):
        logger.warning("Nicht genug codierte Gene für Matching.")
        return None

    best_signal = None
    best_score = -1.0

    # Längste Sequenz zuerst (spezifischer = besser)
    for seq_len in sorted(sequence_lengths, reverse=True):
        if len(genes) < seq_len:
            continue

        sequence = genes_to_sequence_string(genes[-seq_len:])

        # LONG prüfen — nur wenn das Genome im aktuellen Regime aktiv ist
        long_genome = db.get_genome(sequence, market, timeframe, "LONG")
        if (long_genome and long_genome['active'] and long_genome['score'] >= min_score
                and _regime_active(long_genome, current_regime)):
            if long_genome['score'] > best_score:
                best_score = long_genome['score']
                best_signal = _build_signal("long", df, long_genome, rr_ratio)

        # SHORT prüfen
        short_genome = db.get_genome(sequence, market, timeframe, "SHORT")
        if (short_genome and short_genome['active'] and short_genome['score'] >= min_score
                and _regime_active(short_genome, current_regime)):
            if short_genome['score'] > best_score:
                best_score = short_genome['score']
                best_signal = _build_signal("short", df, short_genome, rr_ratio)

    if best_signal:
        best_signal['regime'] = current_regime
    else:
        logger.info(f"[Genome Signal] Kein Match für {market} ({timeframe}) im Regime {current_regime}.")

    return best_signal


def _regime_active(genome: dict, current_regime: str) -> bool:
    """
    Prüft ob ein Genome im aktuellen Regime gehandelt werden darf.

    Logik:
      - HIGH_VOL → immer blockiert
      - active_regimes (JSON-Liste) muss das aktuelle Regime enthalten
      - Fallback: Genome ohne active_regimes-Daten darf überall gehandelt werden
        (Rückwärtskompatibilität mit alten DB-Einträgen)
    """
    if current_regime == REGIME_HIGH_VOL:
        return False

    try:
        active_regimes = json.loads(genome.get('active_regimes', '[]'))
    except (json.JSONDecodeError, TypeError):
        active_regimes = []

    # Leere Liste = alter DB-Eintrag ohne Regime-Tracking → erlauben
    if not active_regimes:
        return True

    return current_regime in active_regimes


def update_genome_with_trade_result(
    db: GenomeDB,
    genome_id: str,
    sequence: str,
    market: str,
    timeframe: str,
    direction: str,
    seq_length: int,
    outcome: str,
    actual_move_pct: float,
    regime: str = 'NEUTRAL',
):
    """
    Aktualisiert die Genome-DB nach Abschluss eines Live-Trades.
    Ermöglicht echtes Self-Learning aus Live-Ergebnissen.

    Args:
        outcome: "WIN" oder "LOSS"
        actual_move_pct: Tatsächliche Preisbewegung in %
        regime: Marktregime zum Zeitpunkt des Trades
    """
    is_win = outcome == "WIN"
    db.upsert_genome_outcome(
        sequence=sequence,
        market=market,
        timeframe=timeframe,
        direction=direction,
        seq_length=seq_length,
        is_win=is_win,
        move_pct=abs(actual_move_pct),
        regime=regime,
    )
    logger.info(
        f"[Self-Learning] Genome {genome_id[:8]}... aktualisiert: "
        f"outcome={outcome}, move={actual_move_pct:.2f}%, regime={regime}"
    )
