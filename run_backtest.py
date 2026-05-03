#!/usr/bin/env python3
# run_backtest.py
# Führt Backtests für alle aktiven Strategies aus active_strategies durch.
#
# Ausführung:
#   .venv/bin/python3 run_backtest.py
#   .venv/bin/python3 run_backtest.py --symbol BTC/USDT:USDT --timeframe 4h

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.analysis.backtester import run_backtest, save_results, print_backtest_summary
from scan_and_learn import (
    HISTORY_DAYS_MAP, resolve_history_days,
    load_settings, load_secrets,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(PROJECT_ROOT, 'logs', 'backtest.log'), mode='a'),
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')

TIMEFRAME_MINUTES = {'15m': 15, '30m': 30, '1h': 60, '2h': 120, '4h': 240, '6h': 360, '1d': 1440}


def get_warmup_start_date(start_date_str: str, timeframe: str, warmup_candles: int = 35) -> str:
    """Früheres Startdatum für Indikator-Warmup (warmup_candles Kerzen vor start_date_str)."""
    tf_minutes = TIMEFRAME_MINUTES.get(timeframe, 60)
    warmup_days = max(int(warmup_candles * tf_minutes / (24 * 60)) + 1, 2)
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return (start_dt - timedelta(days=warmup_days)).strftime('%Y-%m-%d')


def fetch_history(exchange: Exchange, symbol: str, timeframe: str, history_days: int):
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=history_days)
    logger.info(f"  Lade Daten: {symbol} ({timeframe}) | {history_days}d History")
    df = exchange.fetch_historical_ohlcv(
        symbol, timeframe,
        start_date.strftime('%Y-%m-%d'),
        end_date.strftime('%Y-%m-%d'),
    )
    if df is None or df.empty:
        logger.warning(f"  Keine Daten für {symbol} ({timeframe}).")
        return None
    logger.info(f"  {len(df)} Kerzen geladen.")
    return df


def main():
    os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)

    parser = argparse.ArgumentParser(description="dnabot Backtester")
    parser.add_argument('--symbol',      type=str,   default=None)
    parser.add_argument('--timeframe',   type=str,   default=None)
    parser.add_argument('--capital',     type=float, default=1000.0)
    parser.add_argument('--risk',        type=float, default=1.0)
    parser.add_argument('--all-from-db', action='store_true',
                        help="Alle (market, timeframe)-Paare aus der DB backtesten")
    parser.add_argument('--start-date',  type=str,   default=None,
                        help="Startdatum für Backtest (YYYY-MM-DD)")
    parser.add_argument('--end-date',    type=str,   default=None,
                        help="Enddatum für Backtest (YYYY-MM-DD)")
    args = parser.parse_args()

    settings = load_settings()
    secrets  = load_secrets()

    scan_cfg    = settings.get('scan_settings', {})
    genome_cfg  = settings.get('genome_settings', {})
    risk_cfg    = settings.get('risk_settings', {})
    active_strats = settings.get('live_trading_settings', {}).get('active_strategies', [])

    # Pairs bestimmen (Priorität: CLI > Env-Overrides > active_strategies)
    override_coins = os.environ.get('DNABOT_OVERRIDE_COINS', '').strip()
    override_tfs   = os.environ.get('DNABOT_OVERRIDE_TFS', '').strip()

    if args.all_from_db:
        db_temp = GenomeDB(DB_PATH)
        pairs = db_temp.get_all_market_pairs()
        db_temp.close()
        if not pairs:
            logger.warning("DB enthält keine Genome. Fallback auf active_strategies.")
            args.all_from_db = False
    if args.symbol and args.timeframe:
        pairs = [(args.symbol, args.timeframe)]
    elif not args.all_from_db and (override_coins or override_tfs):
        # Gleiche Logik wie run_pipeline.sh: kartesisches Produkt der Overrides
        def _to_symbol(coin: str) -> str:
            coin = coin.strip().upper()
            return coin if '/' in coin else f"{coin}/USDT:USDT"

        auto_coins = list(dict.fromkeys(
            s['symbol'] for s in active_strats if s.get('symbol')
        )) or ['BTC/USDT:USDT']
        auto_tfs = list(dict.fromkeys(
            s['timeframe'] for s in active_strats if s.get('timeframe')
        )) or ['4h']

        coins = [_to_symbol(c) for c in override_coins.split()] if override_coins else auto_coins
        tfs   = [t.strip() for t in override_tfs.split()] if override_tfs else auto_tfs
        seen, pairs = set(), []
        for sym in coins:
            for tf in tfs:
                if (sym, tf) not in seen:
                    pairs.append((sym, tf))
                    seen.add((sym, tf))
    elif not args.all_from_db:
        seen, pairs = set(), []
        for s in active_strats:
            sym, tf = s.get('symbol'), s.get('timeframe')
            if sym and tf and (sym, tf) not in seen:
                pairs.append((sym, tf))
                seen.add((sym, tf))
        if not pairs:
            pairs = [('BTC/USDT:USDT', '4h')]

    # Exchange
    accounts = secrets.get('dnabot', [])
    if not accounts:
        logger.critical("Kein 'dnabot'-Account in secret.json gefunden.")
        sys.exit(1)
    exchange = Exchange(accounts[0])

    db = GenomeDB(DB_PATH)

    # Backtest-Parameter
    params = {
        'genome': {
            'min_score':        genome_cfg.get('min_score', 0.08),
            'min_winrate':      genome_cfg.get('min_winrate', 0.45),
            'sequence_lengths': genome_cfg.get('sequence_lengths', [4, 5, 6]),
        },
        'risk': {
            'rr_ratio': risk_cfg.get('rr_ratio', 2.0),
        },
    }
    capital  = args.capital
    risk_pct = args.risk or risk_cfg.get('risk_per_entry_pct', 1.0)
    leverage = int(risk_cfg.get('leverage', 1))

    date_range = ""
    if args.start_date or args.end_date:
        date_range = f" | {args.start_date or '...'} → {args.end_date or 'heute'}"

    print(f"\n{'=' * 60}")
    print(f"  dnabot — Einzel-Backtest")
    print(f"  Kapital: {capital:.0f} USDT | Risiko: {risk_pct}% | Pairs: {len(pairs)}{date_range}")
    print(f"{'=' * 60}\n")

    all_stats = []
    for symbol, timeframe in pairs:
        history_days = resolve_history_days(timeframe, scan_cfg.get('history_days'))
        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is None:
            continue

        # Datumsfilter: Warmup-Puffer vor start_date laden, damit Indikatoren
        # schon fertig aufgewärmt sind wenn der gewünschte Zeitraum beginnt.
        if args.start_date:
            warmup_from = get_warmup_start_date(args.start_date, timeframe)
            df = df[df.index >= pd.Timestamp(warmup_from, tz='UTC')]
        if args.end_date:
            df = df[df.index <= pd.Timestamp(args.end_date + ' 23:59:59', tz='UTC')]
        if df.empty:
            logger.warning(f"Keine Daten im angegebenen Zeitraum für {symbol} ({timeframe}).")
            continue

        results = run_backtest(
            df=df,
            market=symbol,
            timeframe=timeframe,
            db=db,
            params=params,
            start_capital=capital,
            risk_per_trade_pct=risk_pct,
            leverage=leverage,
        )

        # Trades auf gewünschten Zeitraum einschränken (Warmup-Trades herausfiltern)
        if args.start_date:
            sd = pd.Timestamp(args.start_date, tz='UTC')
            filtered_trades = []
            for t in results.get('trades', []):
                ts = pd.Timestamp(str(t['entry_time']))
                if ts.tzinfo is None:
                    ts = ts.tz_localize('UTC')
                if ts >= sd:
                    filtered_trades.append(t)
            results['trades'] = filtered_trades

        print_backtest_summary(results, symbol, timeframe)
        save_results(results, symbol, timeframe)
        all_stats.append((symbol, timeframe, results.get('stats', {})))

    db.close()

    if len(all_stats) > 1:
        G  = '\033[0;32m'   # grün
        Y  = '\033[1;33m'   # gelb
        R  = '\033[0;31m'   # rot
        C  = '\033[0;36m'   # cyan (Header)
        NC = '\033[0m'

        w = 68
        print(f"\n{'=' * w}")
        print(f"  ZUSAMMENFASSUNG — alle Pairs")
        print(f"{'=' * w}")
        print(
            f"{C}  {'Markt':<22} {'TF':<5} {'Trades':>7} {'WR':>7} {'PnL%':>9} {'PF':>6} {'MaxDD':>7}{NC}"
        )
        print(f"  {'-' * (w - 2)}")
        for sym, tf, st in sorted(all_stats, key=lambda x: x[2].get('total_pnl_pct', 0), reverse=True):
            if not st.get('total_trades'):
                continue
            pnl   = st['total_pnl_pct']
            wr    = st['win_rate']
            pf    = st.get('profit_factor', 0)
            dd    = st['max_drawdown_pct']
            n     = st['total_trades']
            sign  = '+' if pnl >= 0 else ''
            pf_str = f"{pf:.2f}" if pf != float('inf') else "∞"

            pnl_col = G if pnl > 0 else (Y if pnl == 0 else R)
            wr_col  = G if wr >= 0.50 else (Y if wr >= 0.43 else R)

            print(
                f"  {sym:<22} {tf:<5} {n:>7} "
                f"{wr_col}{wr:>6.1%}{NC} "
                f"{pnl_col}{sign}{pnl:>7.1f}%{NC} "
                f"{pf_str:>6} "
                f"{dd:>6.1f}%"
            )
        print(f"{'=' * w}\n")


if __name__ == '__main__':
    main()
