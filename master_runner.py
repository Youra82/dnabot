#!/usr/bin/env python3
# master_runner.py
# Orchestriert alle aktiven dnabot-Strategien
# Wird per Cronjob aufgerufen (z.B. alle 15 Minuten für 15m/1h, alle 4h für 4h-Strategien)

import json
import subprocess
import sys
import os
import time
import logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)
log_file = os.path.join(PROJECT_ROOT, 'logs', 'master_runner.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)


def main():
    settings_file = os.path.join(SCRIPT_DIR, 'settings.json')
    secret_file = os.path.join(SCRIPT_DIR, 'secret.json')
    runner_script = os.path.join(SCRIPT_DIR, 'src', 'dnabot', 'strategy', 'run.py')

    python_exe = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')
    if not os.path.exists(python_exe):
        logging.critical(f"Python-Interpreter nicht gefunden: {python_exe}")
        return

    logging.info("=" * 55)
    logging.info("  dnabot Master Runner — Genome Trading System")
    logging.info("=" * 55)

    try:
        with open(settings_file, 'r') as f:
            settings = json.load(f)
        with open(secret_file, 'r') as f:
            secrets = json.load(f)

        if not secrets.get('dnabot'):
            logging.critical("Kein 'dnabot'-Account in secret.json.")
            return

        strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
        if not strategies:
            logging.warning("Keine aktiven Strategien in settings.json.")
            return

        for strat in strategies:
            if not isinstance(strat, dict) or not strat.get('active', False):
                continue

            symbol = strat.get('symbol')
            timeframe = strat.get('timeframe')

            if not symbol or not timeframe:
                logging.warning(f"Unvollständige Strategie-Info: {strat}")
                continue

            logging.info(f"Starte: {symbol} ({timeframe})")
            cmd = [python_exe, runner_script, '--symbol', symbol, '--timeframe', timeframe]

            try:
                proc = subprocess.Popen(cmd)
                logging.info(f"PID {proc.pid}: {symbol} ({timeframe})")
                time.sleep(2)
            except Exception as e:
                logging.error(f"Fehler beim Starten für {symbol}: {e}")

    except FileNotFoundError as e:
        logging.critical(f"Datei nicht gefunden: {e}")
    except json.JSONDecodeError as e:
        logging.critical(f"JSON-Fehler: {e}")
    except Exception as e:
        logging.critical(f"Kritischer Fehler: {e}", exc_info=True)


if __name__ == "__main__":
    main()
