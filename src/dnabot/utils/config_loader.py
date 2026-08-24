# src/dnabot/utils/config_loader.py
# Geteiltes Laden von settings.json/secret.json und der HISTORY_DAYS_MAP --
# zentralisiert, damit risk_genome_discover.py und backtest_momentum_exit.py
# (frueher beide ueber scan_and_learn.py importiert, siehe dessen Entfernung
# beim Genome-System-Cleanup 2026-08-24) dieselben Defaults nutzen.

import os
import sys
import json
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

logger = logging.getLogger(__name__)

# Wie viele Tage History geladen werden (genug Kerzen fuer eine statistisch
# belastbare Risiko-Gen-Discovery). Kann per --history-days ueberschrieben werden.
HISTORY_DAYS_MAP = {
    '5m':  120,
    '15m': 180,
    '30m': 365,
    '1h':  365,
    '2h':  730,
    '4h':  730,
    '6h':  1095,
    '8h':  1095,
    '12h': 1095,
    '1d':  1095,
    '1w':  1095,
}


def load_settings() -> dict:
    with open(os.path.join(PROJECT_ROOT, 'settings.json'), 'r', encoding='utf-8') as f:
        return json.load(f)


def load_secrets() -> dict:
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        logger.critical("secret.json nicht gefunden!")
        sys.exit(1)
    with open(secret_path, 'r', encoding='utf-8') as f:
        return json.load(f)
