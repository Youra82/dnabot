#!/usr/bin/env python3
# backtest_momentum_exit.py
# Backtest-Validator fuer die 'momentum_exit'-Strategie (Fund AQ, siehe
# Memory research_dnabot_direction_calibration.md) GEGEN ECHTE, FRISCHE
# Bitget-Daten -- und WICHTIG: ueber die ECHTE Live-Signalfunktion
# (dnabot.strategy.momentum_exit_logic.get_momentum_exit_signal) und die
# ECHTE, bereits validierte Ausfuehrungsfunktion
# (dnabot.analysis.backtester.simulate_trade), nicht ueber eine
# Nachbildung wie in recherche/risk_exit_genetic_test.py. Damit ist
# sichergestellt, dass Live und Backtest exakt denselben Code durchlaufen
# (siehe feedback_live_backtest_must_match.md).
#
# Nutzung:
#   .venv/bin/python3 backtest_momentum_exit.py --symbol BTC/USDT:USDT --timeframe 6h
#
# Default-Parameter = Fund AQ's validierter 6h-Champion (seq_len=5, rr=1.5,
# trailing=0.5%, risk=1.0%) -- per CLI ueberschreibbar zum Testen anderer
# Timeframes/Pairs (bisher NICHT validiert ausserhalb 6h, siehe Fund AQ).

import os
import sys
import argparse
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.strategy.momentum_exit_logic import get_momentum_exit_signal, MIN_CANDLES_REQUIRED
from dnabot.analysis.backtester import (
    simulate_trade, simulate_trade_subset, print_backtest_summary,
    save_results, FEE_PCT_PER_SIDE,
)
from dnabot.utils.config_loader import HISTORY_DAYS_MAP, load_secrets

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

FETCH_LIMIT = 200  # identisch zu trade_manager.py::FETCH_LIMIT (Live-Fenstergroesse)


def main():
    parser = argparse.ArgumentParser(description="Backtest-Validator: momentum_exit-Strategie (Fund AQ)")
    parser.add_argument('--symbol', type=str, required=True)
    parser.add_argument('--timeframe', type=str, required=True)
    parser.add_argument('--history-days', type=int, default=None)
    parser.add_argument('--capital', type=float, default=1000.0)
    parser.add_argument('--risk', type=float, default=1.0, help="Fund AQ 6h-Default: 1.0")
    parser.add_argument('--rr-ratio', type=float, default=1.5, help="Fund AQ 6h-Default: 1.5")
    parser.add_argument('--trailing-callback-pct', type=float, default=0.5, help="Fund AQ 6h-Default: 0.5")
    parser.add_argument('--seq-len', type=int, default=5, help="Fund AQ 6h-Default: 5")
    parser.add_argument('--oos-weeks', type=int, default=26)
    args = parser.parse_args()

    history_days = args.history_days or HISTORY_DAYS_MAP.get(args.timeframe, 730)

    secrets = load_secrets()
    accounts = secrets.get('dnabot', [])
    if not accounts:
        logger.critical("Keine dnabot-Accounts in secret.json gefunden.")
        sys.exit(1)
    exchange = Exchange(accounts[0])

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=history_days)
    logger.info(f"Lade {args.symbol} ({args.timeframe}) | {history_days}d History...")
    df = exchange.fetch_historical_ohlcv(
        args.symbol, args.timeframe,
        start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'),
    )
    if df is None or df.empty:
        logger.error("Keine Daten geladen.")
        sys.exit(1)
    logger.info(f"{len(df)} Kerzen geladen.")

    params = {
        'market': {'symbol': args.symbol, 'timeframe': args.timeframe},
        'risk': {'rr_ratio': args.rr_ratio},
        'momentum_exit': {'enabled': True, 'seq_len': args.seq_len},
    }
    trailing_callback_pct = args.trailing_callback_pct / 100.0
    dummy_genome = {'genome_id': 'MOM', 'score': 0.0, 'wins': 0, 'total_occurrences': 1}

    trades = []
    busy_until_idx = -1
    n = len(df)
    warmup = max(args.seq_len, MIN_CANDLES_REQUIRED)

    for i in range(warmup, n):
        if i <= busy_until_idx:
            continue
        # Live-treue Fenstergroesse: dieselben letzten FETCH_LIMIT Kerzen,
        # die trade_manager.py::full_trade_cycle() via fetch_recent_ohlcv sehen wuerde.
        window = df.iloc[max(0, i + 1 - FETCH_LIMIT):i + 1]
        signal = get_momentum_exit_signal(window, params)
        if signal is None:
            continue

        sim_signal = {
            'seq_len': args.seq_len,
            'direction': signal['side'].upper(),
            'rr_ratio': args.rr_ratio,
            'genome': dummy_genome,
        }
        # max_hold_candles wie im urspruenglichen Fund-AQ-Research: kein
        # separates Timeout-Limit noetig ueber die reguläre simulate_trade()-
        # Kappung hinaus -- Standard 20 Kerzen (backtester.py-Default).
        trade = simulate_trade(sim_signal, df, i, max_hold_candles=20,
                                trailing_callback_pct=trailing_callback_pct)
        trades.append(trade)
        busy_until_idx = trade['exit_idx']

    logger.info(f"{len(trades)} Trades simuliert.")

    stats = simulate_trade_subset(trades, args.capital, args.risk, leverage=1, fee_pct=FEE_PCT_PER_SIDE)
    results = {'stats': stats, 'trades': trades}
    print_backtest_summary(results, args.symbol, args.timeframe, label="momentum_exit (voller Zeitraum)")

    if args.oos_weeks and not df.empty:
        oos_cutoff = df.index.max() - timedelta(weeks=args.oos_weeks)
        oos_trades = [t for t in trades if pd.Timestamp(str(t['entry_time'])).tz_localize(None)
                      >= pd.Timestamp(oos_cutoff).tz_localize(None)]
        if oos_trades:
            oos_stats = simulate_trade_subset(oos_trades, args.capital, args.risk, leverage=1, fee_pct=FEE_PCT_PER_SIDE)
            oos_results = {'stats': oos_stats, 'trades': oos_trades}
            print_backtest_summary(oos_results, args.symbol, args.timeframe,
                                    label=f"momentum_exit, Out-of-Sample letzte {args.oos_weeks}W ab {oos_cutoff.date()}")
        else:
            logger.info(f"Keine Trades in den letzten {args.oos_weeks} Wochen fuer OOS-Report.")

    save_results(results, args.symbol, f"{args.timeframe}_momentum_exit")


if __name__ == '__main__':
    main()
