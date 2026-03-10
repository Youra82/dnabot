#!/usr/bin/env python3
# scan_and_learn.py
# Genome Discovery Pipeline — Haupt-Lernprozess des dnabot
#
# Was passiert hier:
#   1. Für jedes konfigurierte Symbol + Timeframe:
#      a. Historische OHLCV-Daten laden (history_days aus settings.json)
#      b. Alle Kerzen zu Gene codieren
#      c. Sliding-Window-Analyse: Genome-Muster entdecken → SQLite-DB
#      d. Evolver: Genome bewerten, aktivieren / deaktivieren
#   2. Genome-Library-Report ausgeben
#
# Ausführung:
#   .venv/bin/python3 scan_and_learn.py
#   .venv/bin/python3 scan_and_learn.py --symbol BTC/USDT:USDT --timeframe 4h

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.genome.discovery import discover_genomes
from dnabot.genome.evolver import evolve, print_genome_report
from dnabot.genome.regime import get_atr_ratio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(PROJECT_ROOT, 'logs', 'scan_and_learn.log'), mode='a'),
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')


def load_settings() -> dict:
    with open(os.path.join(PROJECT_ROOT, 'settings.json'), 'r') as f:
        return json.load(f)


def load_secrets() -> dict:
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        logger.critical("secret.json nicht gefunden!")
        sys.exit(1)
    with open(secret_path, 'r') as f:
        return json.load(f)


def fetch_history(exchange: Exchange, symbol: str, timeframe: str, history_days: int):
    """Lädt historische OHLCV-Daten für die Genome-Discovery."""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=history_days)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    logger.info(f"  Lade Daten: {symbol} ({timeframe}) | {start_str} → {end_str}")
    df = exchange.fetch_historical_ohlcv(symbol, timeframe, start_str, end_str)

    if df is None or df.empty:
        logger.warning(f"  Keine Daten für {symbol} ({timeframe}).")
        return None

    logger.info(f"  Geladen: {len(df)} Kerzen für {symbol} ({timeframe})")
    return df


def main():
    os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(PROJECT_ROOT, 'artifacts', 'db'), exist_ok=True)

    parser = argparse.ArgumentParser(description="dnabot Genome Discovery")
    parser.add_argument('--symbol', type=str, help="Nur dieses Symbol scannen")
    parser.add_argument('--timeframe', type=str, help="Nur diesen Timeframe scannen")
    parser.add_argument('--no-evolve', action='store_true', help="Evolver überspringen")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  dnabot — Genome Discovery (scan_and_learn.py)")
    logger.info("=" * 60)

    settings = load_settings()
    secrets = load_secrets()

    scan_cfg = settings.get('scan_settings', {})
    genome_cfg = settings.get('genome_settings', {})
    genome_min_samples = settings.get('scan_settings', {}).get('min_samples_to_activate', 20)

    # Symbols: aus scan_settings.symbols ODER automatisch aus active_strategies ableiten
    explicit_symbols = scan_cfg.get('symbols', [])
    if explicit_symbols:
        symbols = explicit_symbols
    else:
        symbols = list(dict.fromkeys(
            s['symbol'] for s in settings.get('live_trading_settings', {}).get('active_strategies', [])
            if s.get('symbol')
        ))
        if not symbols:
            symbols = ['BTC/USDT:USDT']
        logger.info(f"  scan_settings.symbols nicht gesetzt — übernehme Coins aus active_strategies: {symbols}")

    timeframes = scan_cfg.get('timeframes', ['4h'])
    history_days = scan_cfg.get('history_days', 730)
    discovery_horizon = scan_cfg.get('discovery_horizon', 5)
    move_threshold_pct = scan_cfg.get('move_threshold_pct', 1.0)
    sequence_lengths = genome_cfg.get('sequence_lengths', [4, 5, 6])
    min_winrate = genome_cfg.get('min_winrate', 0.45)
    min_score = genome_cfg.get('min_score', 0.08)
    half_life_days = genome_cfg.get('half_life_days', 180.0)

    # CLI-Filter
    if args.symbol:
        symbols = [args.symbol]
    if args.timeframe:
        timeframes = [args.timeframe]

    # Exchange-Verbindung (nur für Download, keine API-Keys für Discovery nötig
    # → wir nehmen ersten Account aus secret.json)
    accounts = secrets.get('dnabot', [])
    if not accounts:
        logger.critical("Kein 'dnabot'-Account in secret.json gefunden.")
        sys.exit(1)

    exchange = Exchange(accounts[0])

    # Genome-Datenbank öffnen
    db = GenomeDB(DB_PATH)

    total_new = 0
    total_updated = 0

    for symbol in symbols:
        for timeframe in timeframes:
            logger.info(f"\n{'─' * 50}")
            logger.info(f"  Scanne: {symbol} ({timeframe})")
            logger.info(f"{'─' * 50}")

            df = fetch_history(exchange, symbol, timeframe, history_days)
            if df is None:
                continue

            # Discovery
            result = discover_genomes(
                df=df,
                market=symbol,
                timeframe=timeframe,
                db=db,
                sequence_lengths=sequence_lengths,
                discovery_horizon=discovery_horizon,
                move_threshold_pct=move_threshold_pct,
            )
            total_new += result.get('new_genomes', 0)
            total_updated += result.get('updated_genomes', 0)

            # Evolver (direkt nach Discovery)
            if not args.no_evolve:
                # Vol-Factor für volatilitätsadjustierten Decay
                vol_factor = get_atr_ratio(df)
                logger.info(
                    f"  Evolver läuft für {symbol} ({timeframe}) | "
                    f"vol_factor={vol_factor:.2f} (ATR/ATR-MA)"
                )
                evo_result = evolve(
                    db=db,
                    market=symbol,
                    timeframe=timeframe,
                    min_samples=genome_min_samples,
                    min_winrate=min_winrate,
                    score_threshold=min_score,
                    half_life_days=half_life_days,
                    vol_factor=vol_factor,
                )
                logger.info(
                    f"  Evolver: {evo_result['activated']} aktiviert, "
                    f"{evo_result['deactivated']} deaktiviert | "
                    f"eff. Halbwertszeit: {evo_result['effective_half_life']:.0f}d"
                )

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  Discovery abgeschlossen:")
    logger.info(f"  Neue Genome:         {total_new}")
    logger.info(f"  Aktualisierte Gene:  {total_updated}")

    # Finale Zusammenfassung
    print_genome_report(db)

    db.close()
    logger.info("  scan_and_learn.py abgeschlossen.")


if __name__ == "__main__":
    main()
