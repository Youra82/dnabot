# src/dnabot/strategy/run.py
# Entry Point für eine einzelne dnabot-Strategie (Genome System)
import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
import argparse
import ccxt

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.utils.telegram import send_message
from dnabot.utils.trade_manager import full_trade_cycle, get_tracker_file_path
from dnabot.utils.guardian import guardian_decorator


DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')


def setup_logging(symbol: str, timeframe: str) -> logging.Logger:
    safe_filename = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    log_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'dnabot_{safe_filename}.log')

    logger_name = f'dnabot_{safe_filename}'
    logger = logging.getLogger(logger_name)

    if not logger.handlers:
        logger.setLevel(logging.INFO)

        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            f'%(asctime)s [{safe_filename}] %(levelname)s: %(message)s', datefmt='%H:%M:%S'
        ))
        logger.addHandler(ch)
        logger.propagate = False

    return logger


def _find_strategy_overrides(symbol: str, timeframe: str, settings: dict) -> dict:
    """
    Sucht per-Strategy-Overrides in active_strategies.
    Felder 'risk_overrides' und 'genome_overrides' überschreiben globale Werte.

    Beispiel in settings.json:
        { "symbol": "ETH/USDT:USDT", "timeframe": "1h",
          "risk_overrides":   { "leverage": 3, "risk_per_entry_pct": 0.5 },
          "genome_overrides": { "min_score": 0.12 } }
    """
    for strategy in settings.get('live_trading_settings', {}).get('active_strategies', []):
        if strategy.get('symbol') == symbol and strategy.get('timeframe') == timeframe:
            return {
                'risk':   strategy.get('risk_overrides', {}),
                'genome': strategy.get('genome_overrides', {}),
            }
    return {'risk': {}, 'genome': {}}


def load_config(symbol: str, timeframe: str, settings: dict) -> dict:
    """
    Baut die Runtime-Config aus settings.json und Symbol/Timeframe.

    Globale Werte aus risk_settings / genome_settings werden durch
    per-Strategy-Overrides (risk_overrides / genome_overrides in
    active_strategies) überschrieben.
    """
    global_risk = settings.get('risk_settings', {})
    global_genome = settings.get('genome_settings', {})
    overrides = _find_strategy_overrides(symbol, timeframe, settings)
    risk_ov = overrides['risk']
    genome_ov = overrides['genome']

    return {
        "market": {
            "symbol": symbol,
            "timeframe": timeframe,
        },
        "risk": {
            "risk_per_entry_pct": risk_ov.get('risk_per_entry_pct',
                                   global_risk.get('risk_per_entry_pct', 1.0)),
            "leverage":           risk_ov.get('leverage',
                                   global_risk.get('leverage', 5)),
            "margin_mode":        risk_ov.get('margin_mode',
                                   global_risk.get('margin_mode', 'isolated')),
            "rr_ratio":           risk_ov.get('rr_ratio',
                                   global_risk.get('rr_ratio', 2.0)),
        },
        "genome": {
            "min_score":       genome_ov.get('min_score',
                                global_genome.get('min_score', 0.08)),
            "min_winrate":     genome_ov.get('min_winrate',
                                global_genome.get('min_winrate', 0.45)),
            "sequence_lengths": genome_ov.get('sequence_lengths',
                                 global_genome.get('sequence_lengths', [4, 5, 6])),
            "allowed_regimes": genome_ov.get('allowed_regimes',
                                global_genome.get('allowed_regimes',
                                                  ['TREND', 'RANGE', 'NEUTRAL'])),
        },
        "behavior": {
            "use_longs":  risk_ov.get('use_longs', True),
            "use_shorts": risk_ov.get('use_shorts', True),
        },
    }


@guardian_decorator
def run_for_account(account, telegram_config, params, db_path, logger):
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    account_name = account.get('name', 'Standard-Account')

    logger.info(f"--- Starte dnabot (Genome) für {symbol} ({timeframe}) auf Account '{account_name}' ---")

    try:
        exchange = Exchange(account)
        full_trade_cycle(exchange, params, telegram_config, db_path, logger)
    except ccxt.AuthenticationError:
        logger.critical("Authentifizierungsfehler! API-Schlüssel prüfen!")
        raise
    except Exception as e:
        logger.error(f"Unerwarteter Fehler für {symbol}: {e}", exc_info=True)
        raise


def main():
    parser = argparse.ArgumentParser(description="dnabot Genome Trading-Skript")
    parser.add_argument('--symbol', required=True, type=str, help="Handelspaar (z.B. BTC/USDT:USDT)")
    parser.add_argument('--timeframe', required=True, type=str, help="Zeitrahmen (z.B. 4h)")
    args = parser.parse_args()

    symbol = args.symbol
    timeframe = args.timeframe
    logger = setup_logging(symbol, timeframe)

    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json'), 'r') as f:
            settings = json.load(f)

        with open(os.path.join(PROJECT_ROOT, 'secret.json'), 'r') as f:
            secrets = json.load(f)

        params = load_config(symbol, timeframe, settings)
        logger.info(f"Config geladen für {symbol} ({timeframe}).")

        accounts_to_run = secrets.get('dnabot', [])
        telegram_config = secrets.get('telegram', {})

        if not accounts_to_run:
            logger.critical("Keine Account-Konfigurationen unter 'dnabot' in secret.json gefunden.")
            sys.exit(1)

    except FileNotFoundError as e:
        logger.critical(f"Kritische Datei nicht gefunden: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"JSON-Fehler: {e}")
        sys.exit(1)

    for account in accounts_to_run:
        try:
            run_for_account(account, telegram_config, params, DB_PATH, logger)
        except Exception as e:
            logger.error(f"Schwerwiegender Fehler für Account {account.get('name', '?')}: {e}", exc_info=True)
            sys.exit(1)

    logger.info(f">>> dnabot-Lauf für {symbol} ({timeframe}) abgeschlossen <<<")


if __name__ == "__main__":
    main()
