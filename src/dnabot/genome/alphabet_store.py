# src/dnabot/genome/alphabet_store.py
# Pro-Markt/Timeframe Alphabet-Overrides fuer encoder.py.
#
# Warum pro Pair statt global: encoder.py's Body-/Wick-/Volumen-Schwellwerte
# sind Hyperparameter, die festlegen wie eine Kerze zu einem Gen-Buchstaben
# wird. Ein Alphabet, das fuer BTC 4h gut trennt, muss fuer ADA 30m nicht
# optimal sein -- andere typische Body-/Wick-Groessen relativ zum ATR.
# analysis/alphabet_optimizer.py sucht diese Werte per Optuna (mit In-
# Sample/Out-of-Sample-Split), resolve_alphabet() liest sie hier zur Laufzeit
# wieder ein.
#
# Rueckwaertskompatibel: fehlt ein Override fuer ein Pair, gilt
# encoder.DEFAULT_ALPHABET (= die bisherigen fest codierten Schwellwerte)
# unveraendert -- bestehende Pairs aendern ihr Verhalten nicht, bis
# explizit ein Override in settings.json eingetragen wird.

import hashlib
import json

from dnabot.genome.encoder import DEFAULT_ALPHABET

ALPHABET_KEYS = tuple(DEFAULT_ALPHABET.keys())


def resolve_alphabet(market: str, timeframe: str, settings: dict) -> dict:
    """
    Liest den Alphabet-Override fuer (market, timeframe) aus
    settings.json::genome_settings.alphabet_by_pair, sonst DEFAULT_ALPHABET.

    settings.json-Schema:
        "genome_settings": {
          "alphabet_by_pair": {
            "ADA/USDT:USDT": { "30m": { "body_small": 0.29, ... } }
          }
        }
    """
    by_pair = settings.get('genome_settings', {}).get('alphabet_by_pair', {})
    override = by_pair.get(market, {}).get(timeframe)
    if not override:
        return dict(DEFAULT_ALPHABET)
    # Nur bekannte Keys uebernehmen -- robust gegen unvollstaendige/veraltete
    # Eintraege in settings.json (fehlende Keys fallen auf Default zurueck).
    alphabet = dict(DEFAULT_ALPHABET)
    alphabet.update({k: v for k, v in override.items() if k in ALPHABET_KEYS})
    return alphabet


def alphabet_hash(alphabet: dict) -> str:
    """
    Kurzer deterministischer Hash -- Basis fuer die Erkennung, ob sich das
    Alphabet eines Pairs seit dem letzten Scan geaendert hat (siehe
    scan_and_learn.py: erzwingt bei Mismatch einen vollstaendigen Rescan statt
    eines inkrementellen, weil sich sonst Sequenz-Strings aus altem und neuem
    Alphabet in derselben Genome-DB vermischen wuerden -- ein Genome-Eintrag
    ist nur unter EINEM Alphabet ueberhaupt wieder erreichbar).
    """
    canon = json.dumps({k: alphabet.get(k) for k in ALPHABET_KEYS}, sort_keys=True)
    return hashlib.md5(canon.encode()).hexdigest()[:12]
