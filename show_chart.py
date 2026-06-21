#!/usr/bin/env python3
"""
show_chart.py — Simuliert einen Genome-Chart und sendet ihn per Telegram.

Laedt OHLCV-Daten, sucht das beste Genome-Signal aus der DB und sendet
einen PNG-Chart mit hervorgehobenem Genome-Muster und Entry/SL/TP-Levels.
Kein echter Trade wird platziert.

Aufruf:
    .venv/bin/python show_chart.py
    .venv/bin/python show_chart.py --symbol BTC/USDT:USDT --timeframe 4h
    .venv/bin/python show_chart.py --symbol BTC/USDT:USDT --timeframe 4h --side long
"""
import argparse
import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.utils.trade_manager import _generate_genome_chart_png
from dnabot.utils.telegram import send_photo, send_message
from dnabot.genome.database import GenomeDB
from dnabot.strategy.genome_logic import get_genome_signal

logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(message)s')

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
TMP_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')


def _load_secrets():
    with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
        return json.load(f)


def _load_settings():
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        return json.load(f)


def _build_params(symbol: str, timeframe: str, settings: dict) -> dict:
    global_risk   = settings.get('risk_settings', {})
    global_genome = settings.get('genome_settings', {})
    return {
        'market':   {'symbol': symbol, 'timeframe': timeframe},
        'risk': {
            'risk_per_entry_pct': global_risk.get('risk_per_entry_pct', 1.0),
            'leverage':           global_risk.get('leverage', 5),
            'margin_mode':        global_risk.get('margin_mode', 'isolated'),
            'rr_ratio':           global_risk.get('rr_ratio', 2.0),
        },
        'genome': {
            'min_score':        global_genome.get('min_score', 0.08),
            'min_winrate':      global_genome.get('min_winrate', 0.45),
            'sequence_lengths': global_genome.get('sequence_lengths', [4, 5, 6]),
            'allowed_regimes':  global_genome.get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL']),
        },
        'behavior': {'use_longs': True, 'use_shorts': True},
    }


def _make_dummy_signal(df, side: str, params: dict) -> dict:
    """Baut ein Dummy-Signal mit ATR-basiertem SL/TP falls kein Genome-Signal."""
    import numpy as np
    closes = df['close'].values
    highs  = df['high'].values
    lows   = df['low'].values

    # ATR (14)
    n = len(df)
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(np.abs(highs[1:] - closes[:-1]),
                    np.abs(lows[1:] - closes[:-1])))
    atr = float(tr[-14:].mean()) if len(tr) >= 14 else float(tr.mean())

    entry  = float(closes[-1])
    rr     = float(params['risk'].get('rr_ratio', 2.0))
    sl_mult = 1.5
    sl     = entry - sl_mult * atr if side == 'long' else entry + sl_mult * atr
    tp     = entry + rr * abs(entry - sl) if side == 'long' else entry - rr * abs(entry - sl)

    return {
        'side':              side,
        'entry_price':       entry,
        'sl_price':          sl,
        'tp_price':          tp,
        'sl_pct':            abs(entry - sl) / entry * 100,
        'score':             0.0,
        'winrate':           0.0,
        'total_occurrences': 0,
        'genome_id':         'SIMULATION00000000',
        'sequence':          '[SIMULATION]',
        'seq_length':        4,
    }


def generate_and_send(exchange: Exchange, symbol: str, timeframe: str,
                      force_side: str, settings: dict, tg: dict) -> bool:
    params = _build_params(symbol, timeframe, settings)

    print(f"  Lade OHLCV {symbol} ({timeframe})...")
    df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=200)
    if df is None or df.empty or len(df) < 50:
        print(f"  WARNUNG: Nicht genug Daten.")
        return False
    df = df.iloc[:-1]

    db = GenomeDB(DB_PATH) if os.path.exists(DB_PATH) else None
    genome_signal = None

    if db:
        genome_signal = get_genome_signal(df, params, db)
        db.close()

    if genome_signal and not force_side:
        print(f"  Echtes Signal: {genome_signal['side'].upper()} | "
              f"Score {genome_signal['score']:.3f} | "
              f"WR {genome_signal['winrate']:.1%} | "
              f"n={genome_signal['total_occurrences']}")
        print(f"  Seq: {genome_signal['sequence']}")
    else:
        side = force_side or 'long'
        genome_signal = _make_dummy_signal(df, side, params)
        print(f"  Kein Genome-Signal — simuliere {side.upper()} mit ATR-Levels")

    print(f"  Entry: {genome_signal['entry_price']:.6g} | "
          f"SL: {genome_signal['sl_price']:.6g} | "
          f"TP: {genome_signal['tp_price']:.6g}")

    os.makedirs(TMP_DIR, exist_ok=True)
    path = _generate_genome_chart_png(df, genome_signal, symbol, timeframe)

    if not path or not os.path.exists(path):
        print("  FEHLER: PNG konnte nicht erstellt werden.")
        return False

    side_label = 'LONG' if genome_signal['side'] == 'long' else 'SHORT'
    caption = (
        f"[SIMULATION] DNABOT | {symbol} ({timeframe})\n"
        f"{side_label} @ {genome_signal['entry_price']:.6g}  |  "
        f"SL: {genome_signal['sl_price']:.6g}  |  TP: {genome_signal['tp_price']:.6g}\n"
        f"Score: {genome_signal['score']:.3f}  |  "
        f"WR: {genome_signal['winrate']:.1%}  |  "
        f"n={genome_signal['total_occurrences']}"
    )
    send_photo(tg['bot_token'], tg['chat_id'], path, caption)
    os.remove(path)
    print("  Chart gesendet.")
    return True


def main():
    parser = argparse.ArgumentParser(description='Genome-Chart simulieren und per Telegram senden')
    parser.add_argument('--symbol',    type=str, help='Symbol (z.B. BTC/USDT:USDT)')
    parser.add_argument('--timeframe', type=str, help='Timeframe (z.B. 4h)')
    parser.add_argument('--side',      type=str, default='',
                        choices=['long', 'short', ''],
                        help='Richtung erzwingen (default: echtes Genome-Signal)')
    args = parser.parse_args()

    secrets  = _load_secrets()
    settings = _load_settings()

    tg = secrets.get('telegram', {})
    if not tg.get('bot_token') or not tg.get('chat_id'):
        print("FEHLER: Kein Telegram-Token/Chat-ID in secret.json.")
        sys.exit(1)

    accounts = secrets.get('dnabot', [])
    if not accounts:
        print("FEHLER: Kein 'dnabot'-Account in secret.json.")
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        print(f"WARNUNG: Genome-DB nicht gefunden ({DB_PATH}) — Simulation ohne echtes Signal.")

    print("Initialisiere Exchange...")
    exchange = Exchange(accounts[0])
    if not exchange.markets:
        print("FEHLER: Exchange konnte nicht initialisiert werden.")
        sys.exit(1)

    active = settings.get('live_trading_settings', {}).get('active_strategies', [])

    if args.symbol or args.timeframe:
        targets = [
            s for s in active
            if (not args.symbol    or s['symbol']    == args.symbol)
            and (not args.timeframe or s['timeframe'] == args.timeframe)
        ]
        if not targets and (args.symbol or args.timeframe):
            sym = args.symbol or (active[0]['symbol'] if active else 'BTC/USDT:USDT')
            tf  = args.timeframe or '4h'
            targets = [{'symbol': sym, 'timeframe': tf, 'active': True}]
    else:
        targets = [s for s in active if s.get('active', False)]

    if not targets:
        print("Keine passenden Strategien gefunden.")
        sys.exit(1)

    print(f"\n{len(targets)} Strategie(n) — generiere Charts...\n")
    send_message(tg['bot_token'], tg['chat_id'],
                 f"DNABOT Chart-Simulation ({len(targets)} Strategie(n))")

    ok = 0
    for s in targets:
        symbol    = s['symbol']
        timeframe = s['timeframe']
        print(f"[{symbol} / {timeframe}]")
        try:
            if generate_and_send(exchange, symbol, timeframe, args.side, settings, tg):
                ok += 1
        except Exception as e:
            print(f"  FEHLER: {e}")

    print(f"\nFertig: {ok}/{len(targets)} Charts gesendet.")


if __name__ == '__main__':
    main()
