# src/dnabot/genome/order_blocks.py
# Order-Block-Zonen: EINE geteilte Implementierung fuer Backtest UND Live
# (gleiche Begruendung/Konvention wie daily_bias.py: Live und Backtest muessen
# exakt dasselbe tun, sonst validiert der Backtest ein Verhalten, das live gar
# nicht existiert).
#
# Ein Order Block ist die Kerze UNMITTELBAR vor einem Momentum-Impuls (mehrere
# aufeinanderfolgende grosskoerperige Kerzen in dieselbe Richtung). Ihre
# High/Low-Range gilt als Zone: bullischer OB (Impuls nach oben) = erwartete
# Support-Zone, baerischer OB (Impuls nach unten) = erwartete Resistance-Zone.
# Kehrt der Kurs spaeter in eine noch gueltige Zone zurueck (Wick rein) und
# schliesst wieder heraus (Ablehnung), gilt das als Long-/Short-Signal.
#
# Anders als encoder.py's Kompressions-/Momentum-Muster: kein Retest ist an
# ein festes 4-6-Kerzen-Fenster gebunden (kann beliebig viele Kerzen nach dem
# Impuls passieren) -- deshalb kein Gen-Sequenz-Matching, sondern
# zustandsbehaftetes Zonen-Tracking ueber den gesamten sichtbaren Verlauf.
#
# Experimentell -- kein statistisch entdecktes/bewertetes Genome mit eigenem
# Signifikanz-Tracking, sondern eine feste, regelbasierte Preisstruktur (siehe
# settings.json::order_block_settings, per Default deaktiviert bis per
# Backtest/Walk-Forward validiert).

import hashlib
import numpy as np
import pandas as pd

from dnabot.genome.encoder import compute_atr


def find_order_blocks(df: pd.DataFrame, alphabet: dict, impulse_length: int = 3) -> list[dict]:
    """
    Scannt den GESAMTEN df einmal (kein Lookahead-Risiko hier selbst -- die
    Lookahead-Sicherheit entsteht erst bei der Nutzung ueber
    active_zones_as_of(), das nur Zonen VOR dem aktuellen Kerzenindex zulaesst).

    Ein Impuls = `impulse_length` aufeinanderfolgende grosskoerperige Kerzen
    (body_ratio >= alphabet['body_large'], wiederverwendet dieselbe Schwelle
    wie encoder.py::encode_candle() statt einen neuen, untrainierten Wert zu
    erfinden) in dieselbe Richtung. Die Zone = High/Low der Kerze UNMITTELBAR
    davor (formed_idx). Nur der ERSTE Impuls-Start einer zusammenhaengenden
    Kaskade erzeugt eine Zone (verhindert doppelte/ueberlappende Zonen bei
    einer langen Kaskade).

    Rueckgabe: Liste von Dicts {formed_idx, direction ('bull'/'bear'), high,
    low, broken_idx (int|None)} -- broken_idx = erster nachfolgender
    Kerzenindex, dessen CLOSE die Zone in die falsche Richtung durchbricht
    (Standard-OB-Konvention: eine gebrochene Zone ist tot).
    """
    n = len(df)
    if n < impulse_length + 1:
        return []

    atr = compute_atr(df).values
    opens = df['open'].values
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values

    body_large = alphabet.get('body_large', 0.80)
    atr_safe = np.where(atr > 0, atr, np.nan)
    body_ratio = np.nan_to_num(np.abs(closes - opens) / atr_safe, nan=0.0)
    is_large = body_ratio >= body_large
    is_up = closes >= opens

    zones = []
    i = 1
    while i <= n - impulse_length:
        run_dir_up = bool(is_up[i])
        run_ok = all(
            is_large[i + k] and bool(is_up[i + k]) == run_dir_up
            for k in range(impulse_length)
        )
        prev_is_same_run = is_large[i - 1] and bool(is_up[i - 1]) == run_dir_up

        if run_ok and not prev_is_same_run:
            formed_idx = i - 1
            direction = 'bull' if run_dir_up else 'bear'
            zone_high = float(highs[formed_idx])
            zone_low = float(lows[formed_idx])

            # Erster Bruch nach formed_idx: Close jenseits der Zone in die
            # falsche Richtung (vektorisiert wie discovery.py::
            # _simulate_sl_tp_path() -- np.argmax auf Bool-Maske).
            future_closes = closes[formed_idx + 1:]
            if direction == 'bull':
                break_mask = future_closes < zone_low
            else:
                break_mask = future_closes > zone_high
            broken_idx = (formed_idx + 1 + int(np.argmax(break_mask))) if break_mask.any() else None

            zones.append({
                'formed_idx': formed_idx,
                'direction': direction,
                'high': zone_high,
                'low': zone_low,
                'broken_idx': broken_idx,
            })
            i += impulse_length  # Kaskade ueberspringen, keine ueberlappenden Zonen
        else:
            i += 1

    return zones


def active_zones_as_of(zones: list[dict], candle_idx: int, max_age_candles: int) -> list[dict]:
    """
    Nur Zonen, die VOR candle_idx entstanden sind (formed_idx < candle_idx --
    kein Lookahead: eine Zone darf nie fuer die Kerze wirksam sein, die sie
    selbst mitbegruendet), noch nicht gebrochen sind (broken_idx is None oder
    >= candle_idx) und nicht aelter als max_age_candles sind.
    """
    return [
        z for z in zones
        if z['formed_idx'] < candle_idx
        and (z['broken_idx'] is None or z['broken_idx'] >= candle_idx)
        and (candle_idx - z['formed_idx']) <= max_age_candles
    ]


def _ob_genome_id(market: str, timeframe: str, formed_idx: int, direction: str) -> str:
    """Deterministische Pseudo-genome_id fuer Tracker/Self-Learning-
    Kompatibilitaet -- trade_manager.py::place_entry_orders() erwartet dieses
    Feld, auch wenn OB (anders als echte Genome) keine DB-Zeile hat."""
    raw = f"OB::{market}::{timeframe}::{formed_idx}::{direction}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def check_retest(df: pd.DataFrame, idx: int, zones: list[dict], market: str, timeframe: str,
                  rr_ratio: float = 2.0, max_age_candles: int = 100,
                  assumed_winrate: float = 0.5) -> dict | None:
    """
    Prueft NUR die Kerze bei `idx` gegen active_zones_as_of(zones, idx,
    max_age_candles). Retest+Ablehnung: Kerze wickt in die Zone hinein UND
    schliesst wieder ausserhalb (in Zonen-Gunst) -- reine Beruehrung ohne
    Ablehnung erzeugt KEIN Signal.

    Bullish Zone: low <= zone.high UND close > zone.high -> LONG.
    Bearish Zone: high >= zone.low UND close < zone.low -> SHORT.

    SL = gegenueberliegende Zonengrenze (gleiche Fallback-Logik wie
    genome_logic.py::_build_signal() falls sl_distance<=0), TP = entry +
    rr_ratio * sl_distance. Signal-Dict in DERSELBEN Form wie
    genome_logic.py::_build_signal(), damit es unveraendert durch
    trade_manager.py::place_entry_orders() laeuft (score/winrate/
    total_occurrences sind neutrale Platzhalter -- OB hat kein eigenes
    Signifikanz-Tracking wie die echten Genome, siehe Modul-Docstring).
    """
    row = df.iloc[idx]
    low, high, close = float(row['low']), float(row['high']), float(row['close'])

    active = active_zones_as_of(zones, idx, max_age_candles)
    # Juengste zuerst -- bei mehreren gleichzeitig gueltigen Zonen gewinnt die
    # zuletzt gebildete (relevanteste, naeheste Struktur).
    for zone in sorted(active, key=lambda z: z['formed_idx'], reverse=True):
        if zone['direction'] == 'bull' and low <= zone['high'] and close > zone['high']:
            side = 'long'
            sl_price = zone['low']
            sl_distance = close - sl_price
            if sl_distance <= 0:
                sl_price = close * 0.98
                sl_distance = close - sl_price
            tp_price = close + rr_ratio * sl_distance
        elif zone['direction'] == 'bear' and high >= zone['low'] and close < zone['low']:
            side = 'short'
            sl_price = zone['high']
            sl_distance = sl_price - close
            if sl_distance <= 0:
                sl_price = close * 1.02
                sl_distance = sl_price - close
            tp_price = close - rr_ratio * sl_distance
        else:
            continue

        sl_pct = (sl_distance / close) * 100.0
        return {
            "side": side,
            "entry_price": close,
            "sl_price": sl_price,
            "sl_pct": sl_pct,
            "tp_price": tp_price,
            "genome_id": _ob_genome_id(market, timeframe, zone['formed_idx'], zone['direction']),
            "sequence": f"OB:{zone['direction'].upper()}:idx{zone['formed_idx']}",
            "score": 1.0,
            "winrate": assumed_winrate,
            "total_occurrences": 0,
            "seq_length": idx - zone['formed_idx'],
            "avg_move_pct": abs(tp_price - close) / close * 100.0,
            "regime": None,
            "is_order_block": True,
        }

    return None
