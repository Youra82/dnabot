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
import os

from dnabot.genome.encoder import DEFAULT_ALPHABET, DISCOVERY_SCHEMA_VERSION

ALPHABET_KEYS = tuple(DEFAULT_ALPHABET.keys())

# alphabet_by_pair/rr_ratio_by_pair leben in einer eigenen Repo-Root-Datei,
# nicht mehr in settings.json -- wachsen mit jedem vom Alphabet-Optimizer
# bestaetigten Pair (bis zu ~130 Pairs x 6 Werte) und wuerden die fuer
# Menschen gedachte settings.json sonst zumuellen. Genau wie settings.json/
# secret.json normal git-getrackt -- update.sh sichert/stellt sie um
# git reset --hard herum wieder her, push_configs.sh pusht sie manuell
# ins Repo (gleiches, bereits etabliertes Muster, nur zweite Datei).
_ALPHABET_OVERRIDES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'alphabet_overrides.json'
)


def _load_alphabet_overrides() -> dict:
    try:
        with open(_ALPHABET_OVERRIDES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_alphabet(market: str, timeframe: str, settings: dict) -> dict:
    """
    Liest den Alphabet-Override fuer (market, timeframe) aus
    alphabet_overrides.json (Repo-Root), sonst DEFAULT_ALPHABET.

    alphabet_overrides.json-Schema:
        {
          "alphabet_by_pair": {
            "ADA/USDT:USDT": { "30m": { "body_small": 0.29, ... } }
          }
        }
    """
    by_pair = _load_alphabet_overrides().get('alphabet_by_pair', {})
    override = by_pair.get(market, {}).get(timeframe)
    if not override:
        return dict(DEFAULT_ALPHABET)
    # Nur bekannte Keys uebernehmen -- robust gegen unvollstaendige/veraltete
    # Eintraege (fehlende Keys fallen auf Default zurueck).
    alphabet = dict(DEFAULT_ALPHABET)
    alphabet.update({k: v for k, v in override.items() if k in ALPHABET_KEYS})
    return alphabet


def resolve_rr_ratio(market: str, timeframe: str, settings: dict) -> float:
    """
    Liest den RR-Ratio-Override fuer (market, timeframe) aus
    alphabet_overrides.json (von analysis/alphabet_optimizer.py gemeinsam mit
    dem Alphabet per Optuna gesucht und bestaetigt -- siehe dortige
    Zielfunktion), sonst settings.json::risk_settings.rr_ratio (globaler
    Default -- bleibt in settings.json, ist reine Handkonfiguration).

    Prioritaet bei strategy/run.py (Live): ein manuell gesetztes
    risk_overrides.rr_ratio pro Strategie in active_strategies hat weiterhin
    Vorrang vor diesem automatisch gefundenen Wert -- explizite Config
    schlaegt automatisch Optimiertes, wie ueberall sonst in diesem Projekt.

    alphabet_overrides.json-Schema:
        {
          "rr_ratio_by_pair": {
            "ADA/USDT:USDT": { "30m": 2.7 }
          }
        }
    """
    by_pair = _load_alphabet_overrides().get('rr_ratio_by_pair', {})
    override = by_pair.get(market, {}).get(timeframe)
    if override is not None:
        return float(override)
    return float(settings.get('risk_settings', {}).get('rr_ratio', 2.0))


def alphabet_hash(alphabet: dict) -> str:
    """
    Kurzer deterministischer Hash -- Basis fuer die Erkennung, ob sich das
    Alphabet eines Pairs seit dem letzten Scan geaendert hat (siehe
    scan_and_learn.py: erzwingt bei Mismatch einen vollstaendigen Rescan statt
    eines inkrementellen, weil sich sonst Sequenz-Strings aus altem und neuem
    Alphabet in derselben Genome-DB vermischen wuerden -- ein Genome-Eintrag
    ist nur unter EINEM Alphabet ueberhaupt wieder erreichbar).

    Enthaelt zusaetzlich DISCOVERY_SCHEMA_VERSION (encoder.py) -- ein Bump
    dort loest denselben sicheren delete_pair()+Neu-Scan fuer JEDES Pair aus,
    ohne dass sich das Alphabet selbst aendert. Genutzt, um die Wildcard-
    Pattern-Genome (encoder.py::build_pattern_sequence()) einmalig in
    bestehende Datenbanken nachzutragen -- ein manueller Rescan ohne Loeschen
    wuerde bestehende exakte Genome doppelt zaehlen.
    """
    canon = json.dumps(
        {**{k: alphabet.get(k) for k in ALPHABET_KEYS}, "_discovery_version": DISCOVERY_SCHEMA_VERSION},
        sort_keys=True,
    )
    return hashlib.md5(canon.encode()).hexdigest()[:12]
