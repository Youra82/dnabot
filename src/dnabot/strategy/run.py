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
from dnabot.genome.scoring import breakeven_winrate
from dnabot.genome.alphabet_store import resolve_alphabet, resolve_rr_ratio
from dnabot.utils.strategy_overrides import find_strategy_overrides


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


def load_config(symbol: str, timeframe: str, settings: dict) -> dict:
    """
    Baut die Runtime-Config aus settings.json und Symbol/Timeframe.

    Globale Werte aus risk_settings / genome_settings werden durch
    per-Strategy-Overrides (risk_overrides / genome_overrides in
    active_strategies) überschrieben.
    """
    global_risk = settings.get('risk_settings', {})
    global_genome = settings.get('genome_settings', {})
    overrides = find_strategy_overrides(symbol, timeframe, settings)
    risk_ov = overrides['risk']
    genome_ov = overrides['genome']

    # Prioritaet: manueller Strategie-Override (risk_overrides.rr_ratio in
    # active_strategies) > vom Alphabet-Optimizer bestaetigte, pro Pair
    # automatisch gefundene RR-Ratio > globaler Default. Explizite Config
    # schlaegt automatisch Optimiertes, wie ueberall sonst in diesem Projekt.
    _rr_ratio = risk_ov.get('rr_ratio', resolve_rr_ratio(symbol, timeframe, settings))
    # min_winrate: explizit gesetzt (pro Strategie oder global) hat Vorrang,
    # sonst aus der fuer DIESE Strategie geltenden rr_ratio abgeleitet
    # (Breakeven-Winrate + Sicherheitspuffer statt pauschaler fester Zahl).
    _min_winrate = (genome_ov.get('min_winrate')
                    or global_genome.get('min_winrate')
                    or breakeven_winrate(_rr_ratio))

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
            "rr_ratio":           _rr_ratio,
            # Kelly-Positionsgroesse: standardmaessig AUS, aendert live nichts
            # am bisherigen Verhalten, bis explizit aktiviert.
            "use_kelly_sizing":   risk_ov.get('use_kelly_sizing',
                                   global_risk.get('use_kelly_sizing', False)),
            # Multiplikator auf risk_per_entry_pct, normiert auf 1.0x an der
            # Aktivierungsschwelle (min_winrate) -- staerkere Genome bekommen
            # proportional mehr, gedeckelt zwischen kelly_min_mult/kelly_max_mult.
            "kelly_min_mult":     risk_ov.get('kelly_min_mult',
                                   global_risk.get('kelly_min_mult', 0.5)),
            "kelly_max_mult":     risk_ov.get('kelly_max_mult',
                                   global_risk.get('kelly_max_mult', 3.0)),
            # Daempft wie steil der Multiplikator oberhalb 1.0x mitwaechst
            # (0=immer 1.0x, 1=voller ungedaempfter Kelly-Multiplikator).
            "kelly_dampening":    risk_ov.get('kelly_dampening',
                                   global_risk.get('kelly_dampening', 0.3)),
        },
        "genome": {
            "min_score":       genome_ov.get('min_score',
                                global_genome.get('min_score', 0.08)),
            "min_winrate":     _min_winrate,
            "sequence_lengths": genome_ov.get('sequence_lengths',
                                 global_genome.get('sequence_lengths', [4, 5, 6])),
            "allowed_regimes": genome_ov.get('allowed_regimes',
                                global_genome.get('allowed_regimes',
                                                  ['TREND', 'RANGE', 'NEUTRAL'])),
            "use_daily_trend_filter": genome_ov.get('use_daily_trend_filter',
                                       global_genome.get('use_daily_trend_filter', False)),
            # Muss zum Alphabet passen, mit dem die Genome-DB fuer dieses Pair
            # befuellt wurde (analysis/alphabet_optimizer.py) -- sonst matchen
            # die hier live gebauten Sequenzen nichts in der DB.
            "alphabet": resolve_alphabet(symbol, timeframe, settings),
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
