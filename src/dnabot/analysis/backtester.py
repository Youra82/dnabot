# src/dnabot/analysis/backtester.py
# Backtest-Kernfunktionen fuer die momentum_exit-Strategie.
#
# Frueher auch das Genome-System-Backtest-Herzstueck (run_backtest(), Regime-/
# Order-Block-/CVD-/Daily-Bias-Filter) -- nach der Entfernung des Genome-
# Systems (2026-08-24) bleibt nur das uebrig, was momentum_exit tatsaechlich
# nutzt: simulate_trade() (Kern-Trade-Simulation, live UND Backtest identisch,
# siehe feedback_live_backtest_must_match), simulate_trade_subset() (OOS-
# Teilmengen-Reporting) und die Ergebnis-Speicherung/-Anzeige.

import os
import json
import logging
from datetime import datetime, timezone

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

logger = logging.getLogger(__name__)

RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
MAX_NOTIONAL_USDT = 200_000.0
# Bitget-Taker. Nur fuer die Dollar-Umrechnung in simulate_trade_subset() --
# NICHT fuer trade['pnl_pct'] (siehe simulate_trade()-Kommentar dort): der
# rohe, gebuehrenfreie Kursverlauf landet unveraendert im Trade-Dict, Aufrufer
# rechnen selbst in Dollar um und ziehen dabei ihre eigene fee_pct-Stufe ab.
FEE_PCT_PER_SIDE = 0.06


def simulate_trade(signal: dict, df: pd.DataFrame, entry_idx: int,
                    max_hold_candles: int = 20,
                    trailing_callback_pct: float = None) -> dict:
    """
    Simuliert einen Trade auf historischen Daten -- identische Funktion fuer
    Live (ueber trade_manager.py::place_entry_orders() nachgebildetes
    Verhalten) und Backtest (backtest_momentum_exit.py, risk_genome_discover.py).

    Entry = Close der Signal-Kerze
    SL = Low/High der letzten seq_len Kerzen
    trailing_callback_pct (0-1, z.B. 0.01 = 1%): tp_price wird als
    AKTIVIERUNGS-Preis fuer einen Trailing Stop behandelt (wie live via
    place_trailing_stop_order), statt als sofortiger Take-Profit-Exit.

    KEIN fee_pct-Parameter hier bewusst -- pnl_pct bleibt roh, siehe Kommentar
    bei der pnl_pct-Berechnung unten.
    """
    seq_len = signal['seq_len']
    direction = signal['direction']
    rr_ratio = signal['rr_ratio']

    entry_price = float(df['close'].iloc[entry_idx])

    seq_df = df.iloc[max(0, entry_idx - seq_len + 1): entry_idx + 1]
    if direction == 'LONG':
        sl_price = float(seq_df['low'].min())
        sl_dist = entry_price - sl_price
        if sl_dist <= 0:
            sl_price = entry_price * 0.98
            sl_dist = entry_price - sl_price
        tp_price = entry_price + rr_ratio * sl_dist
    else:
        sl_price = float(seq_df['high'].max())
        sl_dist = sl_price - entry_price
        if sl_dist <= 0:
            sl_price = entry_price * 1.02
            sl_dist = sl_price - entry_price
        tp_price = entry_price - rr_ratio * sl_dist

    sl_pct = sl_dist / entry_price * 100.0

    entry_time = df.index[entry_idx]
    last_idx = min(entry_idx + max_hold_candles, len(df) - 1)
    end_time = df.index[last_idx]
    walk_bars = df.iloc[entry_idx + 1: last_idx + 1]

    outcome = 'TIMEOUT'
    exit_price = float(df['close'].iloc[last_idx])
    exit_time = end_time
    trailing_active = False
    peak_price = None

    for ts, bar in walk_bars.iterrows():
        h, l = float(bar['high']), float(bar['low'])

        if direction == 'LONG':
            if l <= sl_price:
                outcome, exit_price, exit_time = 'LOSS', sl_price, ts
                break
            if trailing_callback_pct is None:
                if h >= tp_price:
                    outcome, exit_price, exit_time = 'WIN', tp_price, ts
                    break
            else:
                if not trailing_active and h >= tp_price:
                    trailing_active = True
                    peak_price = h
                if trailing_active:
                    peak_price = max(peak_price, h)
                    trail_level = peak_price * (1 - trailing_callback_pct)
                    if l <= trail_level:
                        outcome, exit_price, exit_time = 'WIN', trail_level, ts
                        break
        else:
            if h >= sl_price:
                outcome, exit_price, exit_time = 'LOSS', sl_price, ts
                break
            if trailing_callback_pct is None:
                if l <= tp_price:
                    outcome, exit_price, exit_time = 'WIN', tp_price, ts
                    break
            else:
                if not trailing_active and l <= tp_price:
                    trailing_active = True
                    peak_price = l
                if trailing_active:
                    peak_price = min(peak_price, l)
                    trail_level = peak_price * (1 + trailing_callback_pct)
                    if h >= trail_level:
                        outcome, exit_price, exit_time = 'WIN', trail_level, ts
                        break

    exit_idx = last_idx if outcome == 'TIMEOUT' else df.index.get_loc(exit_time)

    if direction == 'LONG':
        pnl_pct = (exit_price - entry_price) / entry_price * 100.0
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100.0
    # pnl_pct bleibt bewusst ROH (keine Gebuehren) -- Aufrufer (simulate_trade_subset(),
    # risk_genome_db.py::record_trade()) rechnen selbst in Dollar um und ziehen dabei
    # ihre eigene fee_pct-Stufe ab. Gebuehren hier zusaetzlich einzurechnen wuerde zu
    # einer Doppelzaehlung fuehren.

    return {
        'entry_time': str(df.index[entry_idx]),
        'exit_time': str(exit_time),
        'direction': direction,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'sl_price': sl_price,
        'tp_price': tp_price,
        'sl_pct': sl_pct,
        'outcome': outcome,
        'pnl_pct': pnl_pct,
        'genome_id': signal['genome']['genome_id'],
        'genome_score': signal['genome']['score'],
        'genome_winrate': signal['genome']['wins'] / max(signal['genome']['total_occurrences'], 1),
        'genome_total_occurrences': signal['genome']['total_occurrences'],
        'seq_len': seq_len,
        'exit_idx': exit_idx,
    }


def _compute_stats(trades: list[dict], equity_curve: list[float], start_capital: float) -> dict:
    if not trades:
        return {"total_trades": 0}

    wins = [t for t in trades if t['outcome'] == 'WIN']
    losses = [t for t in trades if t['outcome'] == 'LOSS']
    timeouts = [t for t in trades if t['outcome'] == 'TIMEOUT']

    total = len(trades)
    win_rate = len(wins) / total if total > 0 else 0.0

    pnl_list = [t.get('pnl_usdt', 0) for t in trades]
    total_pnl = sum(pnl_list)
    avg_win = sum(t['pnl_usdt'] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t['pnl_usdt'] for t in losses) / len(losses) if losses else 0.0

    profit_factor = (
        abs(sum(t['pnl_usdt'] for t in wins)) /
        abs(sum(t['pnl_usdt'] for t in losses))
        if losses and sum(t['pnl_usdt'] for t in losses) != 0 else float('inf')
    )

    eq = equity_curve if equity_curve else [start_capital]
    peak = eq[0]
    max_dd = 0.0
    for e in eq:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "win_rate": win_rate,
        "total_pnl_usdt": total_pnl,
        "total_pnl_pct": (total_pnl / start_capital) * 100,
        "avg_win_usdt": avg_win,
        "avg_loss_usdt": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "final_equity": equity_curve[-1] if equity_curve else start_capital,
    }


def simulate_trade_subset(trades: list[dict], start_capital: float,
                           risk_per_trade_pct: float, leverage: int = 1,
                           fee_pct: float = FEE_PCT_PER_SIDE) -> dict:
    """Simuliert eine Trade-Liste (chronologisch) mit fixer Positionsgroesse
    (risk_per_trade_pct % der jeweils AKTUELLEN Equity) ab einer frischen
    Startequity -- genutzt fuer volle Zeitraum- UND OOS-Teilmengen-Reports
    (backtest_momentum_exit.py)."""
    equity = start_capital
    equity_curve = [equity]
    new_trades = []
    for t in sorted(trades, key=lambda x: x['entry_time']):
        sl_pct = t.get('sl_pct', 0.0)
        if sl_pct <= 0:
            continue
        risk_amount = equity * (risk_per_trade_pct / 100.0) * t.get('kelly_multiplier', 1.0)
        position_size = risk_amount / (sl_pct / 100.0)
        max_position = equity * max(leverage, 1)
        if position_size > max_position:
            position_size = max_position
            risk_amount = position_size * (sl_pct / 100.0)
        if position_size > MAX_NOTIONAL_USDT:
            position_size = MAX_NOTIONAL_USDT
            risk_amount = position_size * (sl_pct / 100.0)
        actual_pnl = position_size * (t['pnl_pct'] / 100.0)
        if fee_pct:
            actual_pnl -= position_size * (fee_pct / 100.0) * 2.0
        equity += actual_pnl
        nt = dict(t)
        nt['pnl_usdt'] = actual_pnl
        nt['equity_after'] = equity
        new_trades.append(nt)
        equity_curve.append(equity)
    return _compute_stats(new_trades, equity_curve, start_capital)


def save_results(results: dict, market: str, timeframe: str):
    """Speichert Backtest-Ergebnisse als JSON."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe_name = f"{market.replace('/', '').replace(':', '')}_{timeframe}"
    path = os.path.join(RESULTS_DIR, f"backtest_{safe_name}.json")

    output = {
        "market": market,
        "timeframe": timeframe,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "stats": results.get("stats", {}),
        "trades": results.get("trades", []),
    }
    with open(path, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"Backtest-Ergebnisse gespeichert: {path}")
    return path


def print_backtest_summary(results: dict, market: str, timeframe: str, label: str = None):
    stats = results.get("stats", {})

    header = f"BACKTEST: {market} ({timeframe})" + (f" — {label}" if label else "")
    print(f"\n{'=' * 60}")
    print(f"  {header}")
    print(f"{'=' * 60}")
    print(f"  Trades gesamt:   {stats.get('total_trades', 0)}")
    print(f"  Wins / Losses:   {stats.get('wins', 0)} / {stats.get('losses', 0)}")
    print(f"  Win-Rate:        {stats.get('win_rate', 0):.1%}")
    print(f"  Profit Factor:   {stats.get('profit_factor', 0):.2f}")
    print(f"  Total PnL:       {stats.get('total_pnl_usdt', 0):+.2f} USDT ({stats.get('total_pnl_pct', 0):+.1f}%)")
    print(f"  Avg Win:         {stats.get('avg_win_usdt', 0):+.2f} USDT")
    print(f"  Avg Loss:        {stats.get('avg_loss_usdt', 0):+.2f} USDT")
    print(f"  Max Drawdown:    {stats.get('max_drawdown_pct', 0):.1f}%")
    print(f"  Final Equity:    {stats.get('final_equity', 0):.2f} USDT")
    print(f"{'=' * 60}\n")
