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
import pandas as pd
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.genome.discovery import discover_genomes
from dnabot.genome.evolver import evolve, print_genome_report
from dnabot.genome.scoring import breakeven_winrate
from dnabot.genome.regime import get_atr_ratio
from dnabot.genome.alphabet_store import resolve_alphabet, alphabet_hash as compute_alphabet_hash

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

# Mindest-Vorkommen für Aktivierung.
# Da jeder Candle-Scan BEIDE Richtungen (LONG + SHORT) aufzeichnet, akkumulieren
# Genome realistischere Statistiken. Trotzdem bleiben Werte bewusst niedrig, da
# Crypto-Patterns selten ≥ 30× in einem üblichen Datensatz wiederkehren.
MIN_SAMPLES_MAP = {
    '5m':  20,
    '15m': 15,
    '30m': 10,
    '1h':  8,
    '2h':  6,
    '4h':  5,
    '6h':  4,
    '8h':  4,
    '12h': 3,
    '1d':  3,
    '1w':  2,
}


def _resolve(tf: str, override, mapping: dict, fallback):
    """Override hat Vorrang, sonst Mapping-Wert, sonst Fallback."""
    return override if override is not None else mapping.get(tf, fallback)


def resolve_history_days(timeframe: str, override) -> int:
    return _resolve(timeframe, override, HISTORY_DAYS_MAP, 730)


def resolve_discovery_horizon(timeframe: str, override) -> int:
    return _resolve(timeframe, override, DISCOVERY_HORIZON_MAP, 6)


def resolve_min_samples(timeframe: str, override) -> int:
    return _resolve(timeframe, override, MIN_SAMPLES_MAP, 80)


def get_min_samples_override(scan_cfg: dict, timeframe: str):
    """
    Ermittelt den min_samples-Override fuer EINEN Timeframe.

    Prioritaet: scan_settings.min_samples_by_timeframe[timeframe] (z.B. per
    analysis/min_samples_sweep.py Optuna-optimiert) > scan_settings.
    min_samples_to_activate (pauschaler fester Wert) > None (dann greift
    resolve_min_samples()'s MIN_SAMPLES_MAP).
    """
    by_tf = scan_cfg.get('min_samples_by_timeframe', {})
    if timeframe in by_tf:
        return by_tf[timeframe]
    return scan_cfg.get('min_samples_to_activate', None)


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


_warned_symbols: set = set()

def _send_telegram_warning(message: str, secrets: dict, dedup_key: str = None):
    if dedup_key:
        if dedup_key in _warned_symbols:
            return
        _warned_symbols.add(dedup_key)
    try:
        import requests
        acc = secrets.get('dnabot', [{}])[0]
        token = acc.get('telegram_bot_token', '') or secrets.get('telegram', {}).get('bot_token', '')
        chat_id = acc.get('telegram_chat_id', '') or secrets.get('telegram', {}).get('chat_id', '')
        if not token or not chat_id:
            return
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={'chat_id': chat_id, 'text': message},
            timeout=10,
        )
    except Exception:
        pass


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

    # Ein LEERER --symbol/--timeframe (im Unterschied zu "gar nicht angegeben",
    # args ist dann None) ist immer ein Aufrufer-Bug (z.B. run_pipeline.sh, das
    # eine leere Coin-Liste weiterreicht) -- NIEMALS still auf den vollen
    # scan_settings-Pool zurueckfallen (siehe CLI-Filter unten: ein leerer
    # String ist falsy und wuerde sonst unbemerkt ALLE Paare scannen statt der
    # eigentlich gewuenschten expliziten Auswahl). Laut fehlgeschlagen statt
    # lautlos falsch.
    if args.symbol is not None and not args.symbol.strip():
        logger.critical("--symbol wurde leer uebergeben -- Abbruch statt stillem Fallback auf den vollen Scan-Pool.")
        sys.exit(1)
    if args.timeframe is not None and not args.timeframe.strip():
        logger.critical("--timeframe wurde leer uebergeben -- Abbruch statt stillem Fallback auf den vollen Scan-Pool.")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  dnabot — Genome Discovery (scan_and_learn.py)")
    logger.info("=" * 60)

    settings = load_settings()
    secrets = load_secrets()

    scan_cfg = settings.get('scan_settings', {})
    genome_cfg = settings.get('genome_settings', {})

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
    elif scan_cfg.get('scan_all_db_pairs', False):
        # Alle (market, timeframe)-Paare aus der Genome-DB scannen
        _db_tmp = GenomeDB(DB_PATH)
        db_pairs = _db_tmp.get_all_market_pairs()
        _db_tmp.close()
        if db_pairs:
            scan_pairs = db_pairs
            logger.info(f"  scan_all_db_pairs=true → {len(scan_pairs)} Paare aus Genome-DB")
        else:
            scan_pairs = [('BTC/USDT:USDT', '4h')]
            logger.warning("  scan_all_db_pairs=true aber DB leer — Fallback auf BTC/USDT:USDT 4h")
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
    sequence_lengths = genome_cfg.get('sequence_lengths', [4, 5, 6])
    min_score = genome_cfg.get('min_score', 0.08)
    half_life_days = genome_cfg.get('half_life_days', 180.0)
    risk_cfg = settings.get('risk_settings', {})
    rr_ratio = risk_cfg.get('rr_ratio', 2.0)
    # min_winrate: explizit gesetzt hat Vorrang, sonst aus rr_ratio abgeleitet
    # (Breakeven-Winrate + Sicherheitspuffer statt pauschaler fester Zahl).
    min_winrate = genome_cfg.get('min_winrate') or breakeven_winrate(rr_ratio)

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
        min_samples       = resolve_min_samples(timeframe, get_min_samples_override(scan_cfg, timeframe))

        logger.info(f"\n{'─' * 50}")
        logger.info(
            f"  Scanne: {symbol} ({timeframe}) | "
            f"history={history_days}d | horizon={discovery_horizon} | "
            f"rr={rr_ratio} | min_samples={min_samples}"
        )
        logger.info(f"{'─' * 50}")

        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is None:
            msg = f"⚠️ dnabot Discovery: {symbol} ({timeframe}) nicht auf Bitget verfügbar — übersprungen."
            logger.warning(msg)
            _send_telegram_warning(msg, secrets, dedup_key=f"{symbol}_{timeframe}")
            continue

        alphabet = resolve_alphabet(symbol, timeframe, settings)
        a_hash = compute_alphabet_hash(alphabet)

        # Inkrementeller Modus: nur neue Kerzen seit letztem Scan verarbeiten
        start_candle_index = 0
        last_scan = db.get_last_scan(symbol, timeframe)

        # Alphabet seit letztem Scan geaendert (z.B. per analysis/
        # alphabet_optimizer.py uebernommen)? Dann sind alle bisherigen
        # Sequenz-Strings fuer dieses Pair unter dem neuen Alphabet nicht mehr
        # erreichbar -- alte Genome loeschen und komplett neu scannen, statt
        # inkrementell auf einer inkonsistenten Mischung weiterzuschreiben.
        if last_scan and last_scan.get('alphabet_hash') and last_scan['alphabet_hash'] != a_hash:
            logger.warning(
                f"  Alphabet fuer {symbol} ({timeframe}) geaendert seit letztem Scan "
                f"({last_scan['alphabet_hash']} → {a_hash}) — loesche alte Genome, vollstaendige Neu-Discovery."
            )
            db.delete_pair(symbol, timeframe)
            last_scan = None

        if last_scan and last_scan.get('data_end_date'):
            try:
                last_end_ts = pd.Timestamp(last_scan['data_end_date'])
                if df.index.tzinfo is not None and last_end_ts.tzinfo is None:
                    last_end_ts = last_end_ts.tz_localize('UTC')
                elif df.index.tzinfo is None and last_end_ts.tzinfo is not None:
                    last_end_ts = last_end_ts.tz_localize(None)
                new_mask = df.index > last_end_ts
                if new_mask.any():
                    start_candle_index = int(new_mask.argmax())
                    logger.info(
                        f"  Inkrementell: {int(new_mask.sum())} neue Kerzen "
                        f"(ab Index {start_candle_index}, nach {last_scan['data_end_date'][:10]})"
                    )
                else:
                    logger.info(
                        f"  Keine neuen Kerzen seit {last_scan['data_end_date'][:10]} — "
                        f"Discovery übersprungen, Evolver läuft weiter."
                    )
                    start_candle_index = len(df)
            except Exception as e:
                logger.warning(f"  Inkrementell-Check fehlgeschlagen ({e}) — vollständiger Scan.")
                start_candle_index = 0
        else:
            logger.info(f"  Erster Scan für {symbol} ({timeframe}) — vollständige Discovery.")

        # Discovery
        result = discover_genomes(
            df=df,
            market=symbol,
            timeframe=timeframe,
            db=db,
            sequence_lengths=sequence_lengths,
            discovery_horizon=discovery_horizon,
            rr_ratio=rr_ratio,
            start_candle_index=start_candle_index,
            alphabet=alphabet,
            alphabet_hash=a_hash,
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
