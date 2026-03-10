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

# ──────────────────────────────────────────────────────────────
# Automatische Defaults pro Timeframe
# Alle Werte können in settings.json manuell überschrieben werden.
# ──────────────────────────────────────────────────────────────

# Wie viele Tage History geladen werden (genug Kerzen für stat. belastbare Genome)
HISTORY_DAYS_MAP = {
    '5m':  120,   # ~34 500 Kerzen
    '15m': 180,   # ~17 000 Kerzen
    '30m': 365,   # ~17 500 Kerzen
    '1h':  365,   # ~8 700 Kerzen
    '2h':  730,   # ~8 700 Kerzen
    '4h':  730,   # ~4 380 Kerzen
    '6h':  1095,  # ~4 380 Kerzen
    '8h':  1095,  # ~3 285 Kerzen
    '12h': 1095,  # ~2 190 Kerzen
    '1d':  1095,  # ~1 095 Kerzen
    '1w':  1095,  # ~156 Kerzen (zu wenig — 1w nicht empfohlen)
}

# Wie viele Kerzen NACH einer Sequenz beobachtet werden (Ziel: ~1 Tag Lookahead)
DISCOVERY_HORIZON_MAP = {
    '5m':  288,   # 1 Tag
    '15m': 96,    # 1 Tag
    '30m': 48,    # 1 Tag
    '1h':  24,    # 1 Tag
    '2h':  12,    # 1 Tag
    '4h':  6,     # 1 Tag
    '6h':  4,     # 1 Tag
    '8h':  3,     # 1 Tag
    '12h': 2,     # 1 Tag
    '1d':  3,     # 3 Tage (tägliche Kerzen brauchen mehr Spielraum)
    '1w':  2,
}

# Mindest-Bewegung in % für ein gültiges Outcome (typische Volatilität je Timeframe)
MOVE_THRESHOLD_MAP = {
    '5m':  0.15,
    '15m': 0.25,
    '30m': 0.4,
    '1h':  0.5,
    '2h':  0.7,
    '4h':  1.0,
    '6h':  1.2,
    '8h':  1.5,
    '12h': 1.5,
    '1d':  2.0,
    '1w':  3.0,
}

# Mindest-Vorkommen für Aktivierung (stat. Belastbarkeit je verfügbarer Datenmenge)
MIN_SAMPLES_MAP = {
    '5m':  200,
    '15m': 150,
    '30m': 120,
    '1h':  100,
    '2h':  80,
    '4h':  80,
    '6h':  60,
    '8h':  60,
    '12h': 50,
    '1d':  50,
    '1w':  30,
}


def _resolve(tf: str, override, mapping: dict, fallback):
    """Override hat Vorrang, sonst Mapping-Wert, sonst Fallback."""
    return override if override is not None else mapping.get(tf, fallback)


def resolve_history_days(timeframe: str, override) -> int:
    return _resolve(timeframe, override, HISTORY_DAYS_MAP, 730)


def resolve_discovery_horizon(timeframe: str, override) -> int:
    return _resolve(timeframe, override, DISCOVERY_HORIZON_MAP, 6)


def resolve_move_threshold(timeframe: str, override) -> float:
    return _resolve(timeframe, override, MOVE_THRESHOLD_MAP, 1.0)


def resolve_min_samples(timeframe: str, override) -> int:
    return _resolve(timeframe, override, MIN_SAMPLES_MAP, 80)


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
    parser.add_argument('--symbol',       type=str, help="Nur dieses Symbol scannen")
    parser.add_argument('--timeframe',    type=str, help="Nur diesen Timeframe scannen")
    parser.add_argument('--history-days', type=int, default=None,
                        help="History-Tage überschreiben (sonst auto nach Timeframe)")
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

    # Symbol/Timeframe-Paare: aus active_strategies ableiten (ODER explizite Overrides nutzen)
    active_strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    explicit_symbols = scan_cfg.get('symbols', [])
    explicit_timeframes = scan_cfg.get('timeframes', [])

    if explicit_symbols or explicit_timeframes:
        # Expliziter Override: kartesisches Produkt wie bisher
        symbols = explicit_symbols or list(dict.fromkeys(
            s['symbol'] for s in active_strategies if s.get('symbol')
        )) or ['BTC/USDT:USDT']
        timeframes_global = explicit_timeframes or ['4h']
        scan_pairs = [(sym, tf) for sym in symbols for tf in timeframes_global]
        logger.info(f"  Explizite Overrides — Scanne {len(scan_pairs)} Paare: {scan_pairs}")
    else:
        # Auto-Ableitung: (symbol, timeframe) direkt aus active_strategies
        seen = set()
        scan_pairs = []
        for s in active_strategies:
            sym = s.get('symbol')
            tf = s.get('timeframe')
            if sym and tf and (sym, tf) not in seen:
                scan_pairs.append((sym, tf))
                seen.add((sym, tf))
        if not scan_pairs:
            scan_pairs = [('BTC/USDT:USDT', '4h')]
        logger.info(
            f"  scan_settings.symbols/timeframes nicht gesetzt — "
            f"übernehme Paare aus active_strategies: {scan_pairs}"
        )

    # Manuelle Overrides: CLI hat Vorrang vor settings.json, dann auto nach Timeframe
    history_days_override    = args.history_days or scan_cfg.get('history_days', None)
    discovery_horizon_override = scan_cfg.get('discovery_horizon', None)
    move_threshold_override  = scan_cfg.get('move_threshold_pct', None)
    min_samples_override     = scan_cfg.get('min_samples_to_activate', None)
    sequence_lengths = genome_cfg.get('sequence_lengths', [4, 5, 6])
    min_winrate = genome_cfg.get('min_winrate', 0.45)
    min_score = genome_cfg.get('min_score', 0.08)
    half_life_days = genome_cfg.get('half_life_days', 180.0)

    # CLI-Filter
    if args.symbol and args.timeframe:
        scan_pairs = [(args.symbol, args.timeframe)]
    elif args.symbol:
        scan_pairs = [(args.symbol, tf) for (sym, tf) in scan_pairs if sym == args.symbol] or \
                     [(args.symbol, scan_cfg.get('timeframes', ['4h'])[0])]
    elif args.timeframe:
        scan_pairs = [(sym, args.timeframe) for (sym, _) in scan_pairs]

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

    for symbol, timeframe in scan_pairs:
        # Alle Scan-Parameter werden pro Timeframe automatisch aufgelöst
        history_days      = resolve_history_days(timeframe, history_days_override)
        discovery_horizon = resolve_discovery_horizon(timeframe, discovery_horizon_override)
        move_threshold    = resolve_move_threshold(timeframe, move_threshold_override)
        min_samples       = resolve_min_samples(timeframe, min_samples_override)

        logger.info(f"\n{'─' * 50}")
        logger.info(
            f"  Scanne: {symbol} ({timeframe}) | "
            f"history={history_days}d | horizon={discovery_horizon} | "
            f"move≥{move_threshold}% | min_samples={min_samples}"
        )
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
            move_threshold_pct=move_threshold,
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
                min_samples=min_samples,
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
