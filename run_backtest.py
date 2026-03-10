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
    parser.add_argument('--symbol',    type=str,   default=None)
    parser.add_argument('--timeframe', type=str,   default=None)
    parser.add_argument('--capital',   type=float, default=1000.0)
    parser.add_argument('--risk',      type=float, default=1.0)
    args = parser.parse_args()

    settings = load_settings()
    secrets  = load_secrets()

    scan_cfg    = settings.get('scan_settings', {})
    genome_cfg  = settings.get('genome_settings', {})
    risk_cfg    = settings.get('risk_settings', {})
    active_strats = settings.get('live_trading_settings', {}).get('active_strategies', [])

    # Pairs bestimmen
    if args.symbol and args.timeframe:
        pairs = [(args.symbol, args.timeframe)]
    else:
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

    print(f"\n{'=' * 60}")
    print(f"  dnabot — Einzel-Backtest")
    print(f"  Kapital: {capital:.0f} USDT | Risiko: {risk_pct}% | Pairs: {len(pairs)}")
    print(f"{'=' * 60}\n")

    all_stats = []
    for symbol, timeframe in pairs:
        history_days = resolve_history_days(timeframe, scan_cfg.get('history_days'))
        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is None:
            continue

        results = run_backtest(
            df=df,
            market=symbol,
            timeframe=timeframe,
            db=db,
            params=params,
            start_capital=capital,
            risk_per_trade_pct=risk_pct,
        )
        print_backtest_summary(results, symbol, timeframe)
        save_results(results, symbol, timeframe)
        all_stats.append((symbol, timeframe, results.get('stats', {})))

    db.close()

    if len(all_stats) > 1:
        print(f"\n{'=' * 60}")
        print(f"  ZUSAMMENFASSUNG — alle Pairs")
        print(f"{'=' * 60}")
        print(f"  {'Markt':<22} {'TF':<5} {'Trades':>7} {'WR':>7} {'PnL%':>8} {'MaxDD':>7}")
        print(f"  {'-' * 55}")
        for sym, tf, st in sorted(all_stats, key=lambda x: x[2].get('total_pnl_pct', 0), reverse=True):
            if not st.get('total_trades'):
                continue
            sign = '+' if st['total_pnl_pct'] >= 0 else ''
            print(
                f"  {sym:<22} {tf:<5} {st['total_trades']:>7} "
                f"{st['win_rate']:>6.1%} {sign}{st['total_pnl_pct']:>7.1f}% "
                f"{st['max_drawdown_pct']:>6.1f}%"
            )
        print(f"{'=' * 60}\n")


if __name__ == '__main__':
    main()
