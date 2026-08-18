# src/dnabot/genome/encoder.py
# Kodiert OHLCV-Kerzen zu DNA-Gensequenzen
#
# Gene-Format: {Richtung}{Körpergröße}{Volatilität}-{Wick}{Volumen}
#
# Beispiele:
#   B3H-UH  = Bullisch, großer Körper, hohe Volatilität, oberer Wick, hohes Volumen
#   S1L-DL  = Bärisch, kleiner Körper, niedrige Volatilität, unterer Wick, niedriges Volumen
#   B2H-BH  = Bullisch, mittlerer Körper, hohe Volatilität, beide Wicks, hohes Volumen
#   S3H-NH  = Bärisch, großer Körper, hohe Volatilität, kein dominanter Wick, hohes Volumen

import pandas as pd
import numpy as np
import ta

# Die sechs Schwellwerte, die das Gen-Alphabet definieren -- bewusst als ein
# einziges Dict statt einzelner Konstanten, damit sie pro (Markt, Timeframe)
# ueberschrieben werden koennen (siehe alphabet_store.py::resolve_alphabet()
# und analysis/alphabet_optimizer.py). DEFAULT_ALPHABET = die Werte, die vor
# der Pro-Pair-Optimierung fest im Code standen -- bleibt die Referenz fuer
# Pairs ohne eigenes optimiertes Alphabet.
DEFAULT_ALPHABET = {
    "body_small":     0.30,   # body_ratio < body_small        -> Koerpergroesse "1"
    "body_large":     0.80,   # body_ratio < body_large        -> Koerpergroesse "2", sonst "3"
    "vol_mult":       1.0,    # candle_range >= atr * vol_mult  -> Volatilitaet "H"
    "wick_body_ratio": 0.5,   # Wick prominent wenn > body * wick_body_ratio
    "wick_doji_ratio": 0.25,  # ... bzw. > range * wick_doji_ratio bei Doji (body=0)
    "vol_rel_mult":   1.0,    # volume > avg_volume * vol_rel_mult -> Volumen "H"
}

# Wird in alphabet_store.py::alphabet_hash() mitgehasht, um beim Deploy dieser
# Wildcard-Pattern-Funktionen (siehe wildcard_gene()/build_pattern_sequence()
# unten) einmalig den bestehenden sicheren Full-Rescan-Mechanismus fuer JEDES
# Pair auszuloesen (scan_and_learn.py: Alphabet-Hash-Mismatch -> delete_pair()
# + vollstaendige Neu-Discovery) -- ohne das wuerden alte DBs nie die neuen
# Pattern-Genome nachtragen, und ein manueller start_candle_index=0-Rescan
# wuerde bestehende exakte Genome doppelt zaehlen.
DISCOVERY_SCHEMA_VERSION = 1


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Berechnet den ATR (Average True Range)."""
    return ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'],
        window=period, fillna=True
    ).average_true_range()


def compute_volume_ma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Berechnet den gleitenden Durchschnitt des Volumens."""
    return df['volume'].rolling(window=period, min_periods=1).mean()


def encode_candle(
    open_: float, high: float, low: float, close: float,
    volume: float, atr: float, avg_volume: float,
    alphabet: dict = None,
) -> str:
    """
    Kodiert eine einzelne Kerze zu einem Gen-String.

    Rückgabe: z.B. "B3H-UH"
      [B/S] = Richtung (Bullish / Bearish)
      [1/2/3] = Körpergröße relativ zu ATR (klein / mittel / groß)
      [L/H] = Volatilität (Kerzenlänge relativ zu ATR)
      - Trennzeichen
      [U/D/B/N] = Wick-Struktur (oben / unten / beide / keiner)
      [L/H] = Volumen relativ zum 20-Perioden-Durchschnitt

    alphabet: Schwellwert-Overrides (siehe DEFAULT_ALPHABET). None -> Default
    (identisch zum bisherigen fest codierten Verhalten).
    """
    a = alphabet or DEFAULT_ALPHABET

    # --- Richtung ---
    direction = "B" if close >= open_ else "S"

    # --- Körpergröße relativ zu ATR ---
    body = abs(close - open_)
    if atr > 0:
        body_ratio = body / atr
        if body_ratio < a["body_small"]:
            size = "1"   # kleiner Körper / Doji
        elif body_ratio < a["body_large"]:
            size = "2"   # mittlerer Körper
        else:
            size = "3"   # großer Körper (Momentum)
    else:
        size = "2"

    # --- Volatilität: Kerzenlänge vs ATR ---
    candle_range = high - low
    if atr > 0:
        vol_code = "H" if candle_range >= atr * a["vol_mult"] else "L"
    else:
        vol_code = "L"

    # --- Wick-Struktur ---
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    # Wick ist prominent, wenn er mehr als wick_body_ratio des Körpers beträgt
    # Bei Doji (body=0) nutzen wir wick_doji_ratio der Range als Schwellwert
    wick_threshold = body * a["wick_body_ratio"] if body > 0 else candle_range * a["wick_doji_ratio"]
    upper_prom = upper_wick > wick_threshold
    lower_prom = lower_wick > wick_threshold

    if upper_prom and lower_prom:
        wick = "B"   # Beide Wicks prominent (Spinning Top)
    elif upper_prom:
        wick = "U"   # Oberer Wick dominant (Shooting Star / Bearish)
    elif lower_prom:
        wick = "D"   # Unterer Wick dominant (Hammer / Bullish)
    else:
        wick = "N"   # Kein dominanter Wick (Marubozu / Momentum)

    # --- Volumen relativ zu Durchschnitt ---
    vol_rel = "H" if (avg_volume > 0 and volume > avg_volume * a["vol_rel_mult"]) else "L"

    return f"{direction}{size}{vol_code}-{wick}{vol_rel}"


def encode_dataframe(df: pd.DataFrame, alphabet: dict = None) -> list[str]:
    """
    Kodiert alle Kerzen eines OHLCV-DataFrames zu einer Liste von Gen-Strings.

    Erwartet Spalten: open, high, low, close, volume
    alphabet: siehe encode_candle(). None -> DEFAULT_ALPHABET fuer alle Kerzen.
    Rückgabe: Liste von Gene-Strings, gleiche Länge wie df.
    """
    if len(df) < 2:
        return []

    atr_series = compute_atr(df)
    vol_ma_series = compute_volume_ma(df)

    genes = []
    for i in range(len(df)):
        row = df.iloc[i]
        atr_val = float(atr_series.iloc[i])
        avg_vol = float(vol_ma_series.iloc[i])

        gene = encode_candle(
            open_=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            volume=float(row['volume']),
            atr=atr_val,
            avg_volume=avg_vol,
            alphabet=alphabet,
        )
        genes.append(gene)

    return genes


def decode_gene(gene: str) -> dict:
    """
    Dekodiert einen Gen-String zurück zu lesbaren Merkmalen.
    Hilfreich für Debugging und Reporting.
    """
    if len(gene) < 6:
        return {}

    direction_map = {"B": "Bullish", "S": "Bearish", "X": "beliebig (Wildcard)"}
    size_map = {"1": "klein (<30% ATR)", "2": "mittel (30-80% ATR)", "3": "groß (>80% ATR)"}
    vol_map = {"L": "niedrig", "H": "hoch", "X": "beliebig (Wildcard)"}
    wick_map = {
        "U": "oberer Wick dominant",
        "D": "unterer Wick dominant",
        "B": "beide Wicks prominent",
        "N": "kein dominanter Wick",
        "X": "beliebig (Wildcard)",
    }

    parts = gene.split("-")
    if len(parts) != 2 or len(parts[0]) != 3 or len(parts[1]) != 2:
        return {"raw": gene}

    main, ext = parts[0], parts[1]

    return {
        "gene": gene,
        "direction": direction_map.get(main[0], main[0]),
        "body_size": size_map.get(main[1], main[1]),
        "volatility": vol_map.get(main[2], main[2]),
        "wick": wick_map.get(ext[0], ext[0]),
        "volume": vol_map.get(ext[1], ext[1]),
    }


def genes_to_sequence_string(genes: list[str]) -> str:
    """Verbindet Gene zu einem Sequenz-String für DB-Speicherung."""
    return "|".join(genes)


def sequence_string_to_genes(sequence: str) -> list[str]:
    """Trennt einen Sequenz-String wieder in einzelne Gene."""
    return sequence.split("|")


WILDCARD = "X"   # kommt in echten Gen-Strings nie vor -> keine Kollisionsgefahr
                 # mit exakten Sequenzen (siehe database.py::_genome_id())


def wildcard_gene(gene: str) -> str:
    """
    Reduziert ein Gen auf Koerpergroesse+Volatilitaet; Richtung/Wick/Volumen
    werden zu Wildcards (X).

    Fuer Kompressions-Positionen einer Pattern-Sequenz (siehe
    build_pattern_sequence()): waehrend einer echten Kompressionsphase ist die
    Kerzenfarbe der Einzelkerzen praktisch zufaellig (Preis pendelt, waehrend
    die Range schrumpft) -- dieselbe visuell erkennbare Formation erzeugt bei
    jedem Auftreten eine andere exakte Gen-Sequenz und keine Variante erreicht
    je min_samples. Koerpergroesse ("klein") + Volatilitaet ("niedrig") sind
    die Merkmale, die "Kompression" tatsaechlich definieren.
    """
    parts = gene.split("-")
    if len(parts) != 2 or len(parts[0]) != 3 or len(parts[1]) != 2:
        return gene   # unbekanntes Format -> unveraendert zurueckgeben
    main = parts[0]
    return f"{WILDCARD}{main[1]}{main[2]}-{WILDCARD}{WILDCARD}"


def build_pattern_sequence(genes: list[str]) -> str:
    """
    Baut die Wildcard-Pattern-Sequenz aus einer Gen-Liste: alle Positionen bis
    auf die letzte (Trigger-Kerze) werden auf Koerpergroesse+Volatilitaet
    reduziert (wildcard_gene()), die letzte bleibt exakt -- Richtung und Wick
    der Trigger-Kerze signalisieren die Umkehr und bleiben deshalb Teil des
    exakten Matches. Macht Kompressionsphasen erkennbar, deren interne
    Zick-Zack-Farbfolge von Vorkommen zu Vorkommen variiert, ohne das
    bestehende exakte Sequenz-Matching zu ersetzen (siehe
    genome/discovery.py, strategy/genome_logic.py, analysis/backtester.py --
    alle drei versuchen sowohl die exakte als auch die Pattern-Sequenz).
    """
    if len(genes) <= 1:
        return genes_to_sequence_string(genes)
    return genes_to_sequence_string([wildcard_gene(g) for g in genes[:-1]] + [genes[-1]])
