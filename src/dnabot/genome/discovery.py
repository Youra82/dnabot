# src/dnabot/genome/discovery.py
# Pattern Discovery Engine — das Herzstück des dnabot
#
# Ablauf:
#   1. OHLCV-Daten laden und Kerzen codieren
#   2. Sliding-Window über alle Kerzen (Längen 4, 5, 6)
#   3. Für jedes Fenster: strukturelles SL/TP wie live/backtester (seq_low/high + rr_ratio)
#   4. Pfadabhängige Simulation über die nächsten Horizon-Kerzen: SL oder TP zuerst?
#   5. Regime zum Zeitpunkt der Sequenz erfassen
#   6. Genome-DB aktualisieren
#
# Win/Loss-Definition bewusst identisch zu backtester.py::simulate_trade und
# genome_logic.py::_build_signal gehalten (siehe dortige SL/TP-Formel) — vorher
# nutzte Discovery einen pfadunabhängigen "max Exkursion > Threshold"-Check, der
# nichts mit der echten Backtest-/Live-Trade-Mechanik zu tun hatte und deshalb
# eine Genome-Winrate produzierte, die nicht mit der Backtest-Winrate übereinstimmte.
#
# Vereinfachung ggü. backtester.py: Pfadauflösung laeuft auf den Grob-Kerzen des
# gescannten Timeframes (kein Fein-Timeframe-Fetch pro Fenster wie im Backtester,
# das waere bei zehntausenden Fenstern pro Symbol/Timeframe nicht praktikabel) —
# das entspricht exakt backtester.py's eigenem Fallback-Pfad, wenn keine
# Fein-Daten verfuegbar sind (bar-fuer-bar SL-zuerst-Check), hier vektorisiert
# per np.argmax auf Bool-Masken statt Python-Schleife mit break.

import logging
import numpy as np
import pandas as pd

from dnabot.genome.encoder import (
    encode_dataframe, genes_to_sequence_string, build_pattern_sequence,
    build_momentum_pattern_sequence,
)
from dnabot.genome.database import GenomeDB
from dnabot.genome.regime import detect_regime

logger = logging.getLogger(__name__)

# Regime wird alle N Kerzen neu berechnet (Performance-Optimierung)
REGIME_RECALC_INTERVAL = 20


def _simulate_sl_tp_path(sl_price: float, tp_price: float, future_highs: np.ndarray,
                          future_lows: np.ndarray, direction: str, timeout_price: float) -> tuple[str, float]:
    """
    Prueft pfadabhaengig, ob SL oder TP innerhalb von future_highs/future_lows
    zuerst getroffen wird (Grob-Kerzen-Naeherung, siehe Modul-Docstring).

    Bei einem Treffer beider Bedingungen in derselben Kerze gewinnt SL (gleiche
    konservative Tie-Break-Konvention wie backtester.py::simulate_trade, das SL
    pro Bar vor TP prueft).

    Returns:
        (outcome, exit_price) mit outcome in {"WIN", "LOSS", "TIMEOUT"}
    """
    if direction == "LONG":
        sl_hit_mask = future_lows <= sl_price
        tp_hit_mask = future_highs >= tp_price
    else:
        sl_hit_mask = future_highs >= sl_price
        tp_hit_mask = future_lows <= tp_price

    sl_idx = int(np.argmax(sl_hit_mask)) if sl_hit_mask.any() else None
    tp_idx = int(np.argmax(tp_hit_mask)) if tp_hit_mask.any() else None

    if sl_idx is not None and (tp_idx is None or sl_idx <= tp_idx):
        return "LOSS", sl_price
    if tp_idx is not None:
        return "WIN", tp_price
    return "TIMEOUT", timeout_price


def discover_genomes(
    df: pd.DataFrame,
    market: str,
    timeframe: str,
    db: GenomeDB,
    sequence_lengths: list[int] = None,
    discovery_horizon: int = 5,
    rr_ratio: float = 2.0,
    start_candle_index: int = 0,
    alphabet: dict = None,
    alphabet_hash: str = None,
) -> dict:
    """
    Scannt historische OHLCV-Daten und entdeckt profitable Genome-Muster.

    Args:
        df: OHLCV DataFrame (index=Timestamp, Spalten: open, high, low, close, volume)
        market: Handelspaar z.B. "BTC/USDT:USDT"
        timeframe: Zeitrahmen z.B. "4h"
        db: GenomeDB-Instanz
        sequence_lengths: Fenstergrößen zu prüfen (Standard: [4, 5, 6])
        discovery_horizon: Wie viele Kerzen nach der Sequenz simuliert werden
                            (entspricht backtester.py's max_hold_candles — nach
                            dieser Anzahl Kerzen ohne SL/TP-Treffer gilt der
                            simulierte Trade als TIMEOUT)
        rr_ratio: Risk:Reward-Verhältnis für die TP-Berechnung (aus
                  settings.json risk_settings.rr_ratio), identisch zur
                  Live-/Backtest-Formel in genome_logic.py/backtester.py
        alphabet: Encoder-Schwellwerte fuer dieses Pair (siehe encoder.py::
                  DEFAULT_ALPHABET, alphabet_store.py::resolve_alphabet()).
                  None -> Default-Alphabet.
        alphabet_hash: wird 1:1 in scan_log geschrieben (Basis fuer die
                       Aenderungserkennung in scan_and_learn.py).

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
        f"Horizon={discovery_horizon} | RR={rr_ratio}"
    )

    # Alle Kerzen codieren
    genes = encode_dataframe(df, alphabet=alphabet)
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values

    new_genomes = 0
    updated_genomes = 0

    # Regime-Cache: wird alle REGIME_RECALC_INTERVAL Kerzen neu berechnet
    regime_cache = {}

    def get_regime_at(idx: int) -> str:
        bucket = (idx // REGIME_RECALC_INTERVAL) * REGIME_RECALC_INTERVAL
        if bucket not in regime_cache:
            # detect_regime() braucht mind. atr_ma_period(=50) + 5 = 55 Kerzen,
            # sonst greift intern ihr eigener "zu wenig Daten"-Fallback auf NEUTRAL
            # (Bug bis 2026-08-13: Fenster war nur 51 Kerzen -> IMMER NEUTRAL,
            # TREND/RANGE-Occurrences wurden nie erfasst). 70 Kerzen Puffer.
            sub_df = df.iloc[max(0, bucket - 70): bucket + 1]
            regime_cache[bucket] = detect_regime(sub_df)
        return regime_cache[bucket]

    # Für jede Sequenzlänge
    for seq_len in sequence_lengths:
        max_start = len(genes) - seq_len - discovery_horizon

        if max_start <= 0:
            logger.debug(f"  seq_len={seq_len}: Nicht genug Kerzen ({len(genes)}). Überspringe.")
            continue

        effective_start = max(start_candle_index, 0)
        logger.debug(f"  Scanne seq_len={seq_len} | {max(0, max_start - effective_start)} Fenster (ab Index {effective_start})...")

        for i in range(effective_start, max_start):
            seq_genes = genes[i:i + seq_len]
            sequence = genes_to_sequence_string(seq_genes)
            # Wildcard-Pattern-Sequenzen zusaetzlich aufzeichnen (siehe
            # encoder.py::build_pattern_sequence()/build_momentum_pattern_
            # sequence()) -- machen Kompressionsphasen (Richtung wechselt von
            # Vorkommen zu Vorkommen) bzw. Momentum-Kaskaden (Docht/Volumen
            # wechseln von Vorkommen zu Vorkommen) erkennbar, deren exakte
            # Sequenz sonst nie genug Samples ansammelt.
            pattern_sequence = build_pattern_sequence(seq_genes)
            momentum_sequence = build_momentum_pattern_sequence(seq_genes)

            entry_idx = i + seq_len
            entry_price = closes[entry_idx - 1]
            occurred_at = df.index[entry_idx - 1].isoformat()

            if entry_price <= 0:
                continue

            # Regime zum Zeitpunkt dieser Sequenz
            regime = get_regime_at(i)

            # Zukunft beobachten (strikt NACH Close der Sequenz — kein Lookahead)
            future_highs = highs[entry_idx: entry_idx + discovery_horizon]
            future_lows = lows[entry_idx: entry_idx + discovery_horizon]

            if len(future_highs) == 0:
                continue

            timeout_price = float(closes[entry_idx + len(future_highs) - 1])

            # Strukturelles SL aus dem Sequenz-Fenster selbst (i:i+seq_len) --
            # identisch zu genome_logic.py::_build_signal / backtester.py::simulate_trade.
            seq_low = float(lows[i:i + seq_len].min())
            seq_high = float(highs[i:i + seq_len].max())

            # LONG
            long_sl = seq_low
            long_sl_dist = entry_price - long_sl
            if long_sl_dist <= 0:
                long_sl = entry_price * 0.98
                long_sl_dist = entry_price - long_sl
            long_tp = entry_price + rr_ratio * long_sl_dist

            # SHORT
            short_sl = seq_high
            short_sl_dist = short_sl - entry_price
            if short_sl_dist <= 0:
                short_sl = entry_price * 1.02
                short_sl_dist = short_sl - entry_price
            short_tp = entry_price - rr_ratio * short_sl_dist

            long_result = _simulate_sl_tp_path(long_sl, long_tp, future_highs, future_lows, "LONG", timeout_price)
            short_result = _simulate_sl_tp_path(short_sl, short_tp, future_highs, future_lows, "SHORT", timeout_price)

            # Immer BEIDE Richtungen aufzeichnen — gibt realistische Win/Loss-Statistiken.
            for direction, (outcome, exit_price) in [("LONG", long_result), ("SHORT", short_result)]:
                is_win = outcome == "WIN"
                move_pct = abs((exit_price - entry_price) / entry_price * 100.0)

                # Exakte Sequenz + beide Wildcard-Pattern-Varianten aufzeichnen
                # -- jede bekommt ihre eigene genome_id (siehe database.py::
                # _genome_id()) und lebt als normale Zeile neben den anderen,
                # keine Kollision (WILDCARD="X" kommt in echten Gen-Strings
                # nie vor, und die beiden Pattern-Varianten haben "X" an
                # unterschiedlichen Positionen -- siehe encoder.py::
                # classify_pattern_type()).
                for seq_variant in (sequence, pattern_sequence, momentum_sequence):
                    is_new = db.upsert_genome_outcome(
                        sequence=seq_variant,
                        market=market,
                        timeframe=timeframe,
                        direction=direction,
                        seq_length=seq_len,
                        is_win=is_win,
                        move_pct=move_pct,
                        regime=regime,
                        occurred_at=occurred_at,
                    )
                    if is_new:
                        new_genomes += 1
                    else:
                        updated_genomes += 1

    candles_processed = max(0, len(df) - start_candle_index)
    if candles_processed > 0:
        try:
            data_end_date = df.index[-1].isoformat()
        except Exception:
            data_end_date = None
        db.log_scan(market, timeframe, candles_processed, new_genomes, updated_genomes,
                    data_end_date, alphabet_hash=alphabet_hash)

    logger.info(
        f"[Discovery] {market} ({timeframe}) abgeschlossen: "
        f"{candles_processed} neue Kerzen, {new_genomes} neue Genome, {updated_genomes} aktualisiert."
    )

    return {
        "candles_processed": candles_processed,
        "new_genomes": new_genomes,
        "updated_genomes": updated_genomes,
    }
