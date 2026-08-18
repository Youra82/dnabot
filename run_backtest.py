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
from dnabot.analysis.backtester import run_backtest, save_results, print_backtest_summary, FINE_TF_MAP, LazyFineData
from dnabot.genome.scoring import breakeven_winrate
from dnabot.genome.alphabet_store import resolve_alphabet, resolve_rr_ratio
from dnabot.utils.strategy_overrides import find_strategy_overrides
from scan_and_learn import (
    HISTORY_DAYS_MAP, resolve_history_days, resolve_min_samples, get_min_samples_override,
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

    # Ein LEERER --symbol/--timeframe (im Unterschied zu "gar nicht angegeben")
    # ist immer ein Aufrufer-Bug (z.B. run_pipeline.sh mit einer leeren
    # Coin-Liste) -- NIEMALS still auf active_strategies/Env-Overrides
    # zurueckfallen. Laut fehlgeschlagen statt lautlos falsch (siehe
    # scan_and_learn.py, derselbe Fix).
    if args.symbol is not None and not args.symbol.strip():
        logger.critical("--symbol wurde leer uebergeben -- Abbruch statt stillem Fallback.")
        sys.exit(1)
    if args.timeframe is not None and not args.timeframe.strip():
        logger.critical("--timeframe wurde leer uebergeben -- Abbruch statt stillem Fallback.")
        sys.exit(1)

    settings = load_settings()
    secrets  = load_secrets()

    scan_cfg    = settings.get('scan_settings', {})
    genome_cfg  = settings.get('genome_settings', {})
    risk_cfg    = settings.get('risk_settings', {})
    ob_cfg      = settings.get('order_block_settings', {})
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
    _rr_ratio = risk_cfg.get('rr_ratio', 2.0)
    params = {
        'genome': {
            'min_score':        genome_cfg.get('min_score', 0.08),
            # explizit gesetzt hat Vorrang, sonst aus rr_ratio abgeleitet
            'min_winrate':      genome_cfg.get('min_winrate') or breakeven_winrate(_rr_ratio),
            'sequence_lengths': genome_cfg.get('sequence_lengths', [4, 5, 6]),
            # min_samples wird PRO PAIR gesetzt (siehe Schleife unten) -- haengt
            # vom jeweiligen Timeframe ab (scan_settings.min_samples_by_timeframe,
            # z.B. per analysis/min_samples_sweep.py optimiert, sonst
            # min_samples_to_activate/MIN_SAMPLES_MAP als Fallback).
            'half_life_days':   genome_cfg.get('half_life_days', 180.0),
            'use_daily_trend_filter':  genome_cfg.get('use_daily_trend_filter', False),
            'use_cvd_filter':          genome_cfg.get('use_cvd_filter', False),
            'cvd_slope_period':        genome_cfg.get('cvd_slope_period', 5),
            'allowed_regimes':         genome_cfg.get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL']),
        },
        'risk': {
            'rr_ratio': _rr_ratio,
            'trailing_callback_rate_pct': risk_cfg.get('trailing_callback_rate_pct'),
        },
        # Order Block: dieselbe Aufloesung wie live in strategy/run.py::
        # load_config() -- ohne das haette order_block_settings.enabled=true
        # in settings.json KEINE Wirkung auf Backtests, obwohl der Live-Bot
        # es sehen wuerde (die genaue Live/Backtest-Divergenz, vor der dieses
        # Projekt sich an mehreren Stellen ausdruecklich schuetzt).
        'order_block': {
            'enabled':              ob_cfg.get('enabled', False),
            'impulse_length':       ob_cfg.get('impulse_length', 3),
            'zone_max_age_candles': ob_cfg.get('zone_max_age_candles', 100),
            'assumed_winrate':      ob_cfg.get('assumed_winrate', 0.5),
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

        # min_samples PRO PAIR (haengt vom Timeframe ab, siehe scan_and_learn.py::
        # resolve_min_samples -- dieselbe Aufloesung, die auch der Evolver nutzt).
        # War hier trotz gegenteiligem Kommentar nie tatsaechlich gesetzt --
        # backtester.py::_find_best_signal() fiel dadurch lautlos auf seinen
        # eigenen Default (20) zurueck, unabhaengig vom viel niedrigeren
        # scan_settings.min_samples_to_activate (z.B. 2), mit dem der Evolver
        # tatsaechlich aktiviert hat -- Backtest fand dadurch oft 0 Trades,
        # obwohl der Evolver-Report viele aktive Genome zeigte.
        params['genome']['min_samples'] = resolve_min_samples(
            timeframe, get_min_samples_override(scan_cfg, timeframe)
        )

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

        # Feinere Kerzen fuer die Trailing-Stop-Intrabar-Simulation (oraclebot-Muster) --
        # deckt denselben Zeitraum wie df ab. Best-effort: bei Fehler faellt
        # simulate_trade auf die Coarse-Kerzen-Naeherung zurueck.
        fine_tf = FINE_TF_MAP.get(timeframe)
        fine_df = LazyFineData(symbol, fine_tf) if fine_tf else None

        # Alphabet ist PRO PAIR gesetzt (siehe analysis/alphabet_optimizer.py) --
        # muss zum Alphabet passen, mit dem die Genome-DB fuer dieses Pair
        # befuellt wurde, sonst matchen die hier gebauten Sequenzen nichts in der DB.
        params['genome']['alphabet'] = resolve_alphabet(symbol, timeframe, settings)

        # RR-Ratio ebenfalls PRO PAIR (vom Alphabet-Optimizer gemeinsam mit dem
        # Alphabet gesucht/bestaetigt) -- veraendert die TP-Distanz und damit
        # min_winrate muss dazu konsistent aus DERSELBEN rr_ratio abgeleitet
        # werden, nicht aus dem globalen Default.
        pair_rr_ratio = resolve_rr_ratio(symbol, timeframe, settings)
        params['risk']['rr_ratio'] = pair_rr_ratio
        params['genome']['min_winrate'] = genome_cfg.get('min_winrate') or breakeven_winrate(pair_rr_ratio)

        # Kelly-Sizing-Config PRO PAIR (dieselbe Aufloesung wie live in
        # strategy/run.py::load_config() -- sonst validiert der Backtest eine
        # Positionsgroesse, die live gar nicht zustande kommt, sobald
        # use_kelly_sizing fuer ein Pair per risk_overrides aktiviert ist).
        pair_risk_ov = find_strategy_overrides(symbol, timeframe, settings)['risk']
        params['risk']['use_kelly_sizing'] = pair_risk_ov.get(
            'use_kelly_sizing', risk_cfg.get('use_kelly_sizing', False))
        params['risk']['kelly_min_mult'] = pair_risk_ov.get(
            'kelly_min_mult', risk_cfg.get('kelly_min_mult', 0.5))
        params['risk']['kelly_max_mult'] = pair_risk_ov.get(
            'kelly_max_mult', risk_cfg.get('kelly_max_mult', 3.0))
        params['risk']['kelly_dampening'] = pair_risk_ov.get(
            'kelly_dampening', risk_cfg.get('kelly_dampening', 0.3))

        results = run_backtest(
            df=df,
            market=symbol,
            timeframe=timeframe,
            db=db,
            params=params,
            start_capital=capital,
            risk_per_trade_pct=risk_pct,
            leverage=leverage,
            fine_df=fine_df,
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
