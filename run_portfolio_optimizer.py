#!/usr/bin/env python3
# run_portfolio_optimizer.py
# Automatische Portfolio-Optimierung:
# Liest gespeicherte Backtest-Ergebnisse, findet die beste
# Kombination von Pairs die den maximalen Profit bei einem
# vorgegebenen Drawdown-Limit liefert.

import os
import sys
import json
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

G   = '\033[0;32m'
Y   = '\033[1;33m'
R   = '\033[0;31m'
C   = '\033[0;36m'
B   = '\033[1;37m'
NC  = '\033[0m'

RR_RATIO = 2.0  # muss mit Backtester übereinstimmen


def load_all_results(start_date=None, end_date=None):
    """Lädt alle Backtest-JSONs aus artifacts/results/."""
    results = []
    if not os.path.isdir(RESULTS_DIR):
        return results

    sd = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc) if start_date else None
    ed = datetime.fromisoformat(end_date + 'T23:59:59').replace(tzinfo=timezone.utc) if end_date else None

    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        path = os.path.join(RESULTS_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue

        trades = data.get('trades', [])
        if sd or ed:
            filtered = []
            for t in trades:
                ts = t.get('entry_time', '')
                if not ts:
                    continue
                try:
                    t_dt = datetime.fromisoformat(str(ts))
                    if t_dt.tzinfo is None:
                        t_dt = t_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if sd and t_dt < sd:
                    continue
                if ed and t_dt > ed:
                    continue
                filtered.append(t)
            trades = filtered

        results.append({
            'market':    data['market'],
            'timeframe': data['timeframe'],
            'trades':    trades,
            'stats':     data.get('stats', {}),
        })

    return results


def simulate_portfolio(pair_results: list, capital: float, risk_pct: float) -> dict:
    """
    Simuliert ein Portfolio aus mehreren Pairs.
    Trades werden chronologisch zusammengeführt.
    Jeder Trade riskiert risk_pct% des aktuellen Equity.
    """
    all_trades = []
    for pr in pair_results:
        for t in pr['trades']:
            all_trades.append({
                'market':    pr['market'],
                'timeframe': pr['timeframe'],
                'outcome':   t.get('outcome', 'LOSS'),
                'pnl_pct':   t.get('pnl_pct', 0.0),
                'sl_pct':    t.get('sl_pct', 1.0),
                'entry_time': t.get('entry_time', ''),
            })

    # Chronologisch sortieren
    all_trades.sort(key=lambda t: str(t['entry_time']))

    equity = capital
    peak   = equity
    max_dd = 0.0
    equity_curve = [equity]

    for t in all_trades:
        risk_amount = equity * (risk_pct / 100.0)
        outcome     = t['outcome']
        sl_pct      = max(t['sl_pct'], 0.01)

        if outcome == 'WIN':
            pnl = risk_amount * RR_RATIO
        elif outcome == 'LOSS':
            pnl = -risk_amount
        else:  # TIMEOUT
            # pnl_pct enthält den tatsächlichen %-Gewinn relativ zum SL
            raw_pnl_pct = t['pnl_pct']
            pnl = risk_amount * (raw_pnl_pct / sl_pct)

        equity += pnl
        equity_curve.append(equity)

        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    total_pnl_pct = (equity - capital) / capital * 100.0 if capital > 0 else 0.0
    n_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t['outcome'] == 'WIN')
    wr   = wins / n_trades if n_trades > 0 else 0.0

    return {
        'total_pnl_pct': total_pnl_pct,
        'final_equity':  equity,
        'max_dd':        max_dd,
        'n_trades':      n_trades,
        'win_rate':      wr,
        'equity_curve':  equity_curve,
    }


def greedy_optimize(all_results: list, capital: float, risk_pct: float,
                    max_dd_limit: float) -> list:
    """
    Greedy-Suche: fügt nacheinander das Pair hinzu, das den Portfolio-PnL
    am meisten steigert, solange MaxDD unter max_dd_limit bleibt.
    """
    # Pairs mit mind. 1 Trade
    candidates = [r for r in all_results if len(r['trades']) > 0]
    if not candidates:
        return []

    selected = []
    remaining = list(candidates)

    while remaining:
        best_pair   = None
        best_pnl    = -1e9
        best_metrics = None

        for pair in remaining:
            trial = selected + [pair]
            metrics = simulate_portfolio(trial, capital, risk_pct)
            if metrics['max_dd'] > max_dd_limit:
                continue
            if metrics['total_pnl_pct'] > best_pnl:
                best_pnl    = metrics['total_pnl_pct']
                best_pair   = pair
                best_metrics = metrics

        if best_pair is None:
            break  # Kein weiteres Pair passt ohne DD-Verletzung

        # Nur hinzufügen wenn es den PnL verbessert
        current_metrics = simulate_portfolio(selected, capital, risk_pct) if selected else {'total_pnl_pct': -1e9}
        if best_pnl <= current_metrics['total_pnl_pct'] and selected:
            break

        selected.append(best_pair)
        remaining.remove(best_pair)

    return selected


def print_optimization_result(selected: list, portfolio_metrics: dict,
                               capital: float, risk_pct: float):
    w = 70
    print(f"\n{'=' * w}")
    print(f"{B}  dnabot — Automatische Portfolio-Optimierung{NC}")
    print(f"{'=' * w}")

    if not selected:
        print(f"\n{R}  Kein Portfolio gefunden, das die Bedingungen erfüllt.{NC}")
        print(f"  → Erhöhe das Max-Drawdown-Limit oder füge mehr Backtest-Daten hinzu.\n")
        return

    print(f"\n  {G}Optimales Portfolio — {len(selected)} Pairs{NC}")
    print(f"  Kapital: {capital:.0f} USDT | Risiko/Trade: {risk_pct}%")
    print(f"\n  {'Markt':<24} {'TF':<6} {'Trades':>7} {'WR':>7} {'PnL%':>9} {'MaxDD':>8}")
    print(f"  {'-' * (w - 2)}")

    for pr in selected:
        st = pr['stats']
        n  = st.get('total_trades', 0)
        wr = st.get('win_rate', 0)
        pnl = st.get('total_pnl_pct', 0)
        dd  = st.get('max_drawdown_pct', 0)
        pnl_col = G if pnl > 0 else R
        wr_col  = G if wr >= 0.50 else (Y if wr >= 0.43 else R)
        sign = '+' if pnl >= 0 else ''
        print(
            f"  {pr['market']:<24} {pr['timeframe']:<6} {n:>7} "
            f"{wr_col}{wr:>6.1%}{NC} "
            f"{pnl_col}{sign}{pnl:>7.1f}%{NC} "
            f"{dd:>7.1f}%"
        )

    pm = portfolio_metrics
    pnl_col = G if pm['total_pnl_pct'] > 0 else R
    print(f"\n  {'─' * (w - 2)}")
    print(f"  {B}Portfolio gesamt:{NC}")
    print(f"  Trades:   {pm['n_trades']}")
    print(f"  Win-Rate: {pm['win_rate']:.1%}")
    print(f"  PnL:      {pnl_col}{'+' if pm['total_pnl_pct'] >= 0 else ''}{pm['total_pnl_pct']:.1f}%{NC}")
    print(f"  Final:    {pm['final_equity']:.2f} USDT")
    print(f"  Max-DD:   {pm['max_dd']:.1f}%")
    print(f"{'=' * w}\n")


def write_to_settings(selected: list):
    """Schreibt die optimalen Pairs in settings.json active_strategies."""
    try:
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"{R}Fehler beim Lesen von settings.json: {e}{NC}")
        return False

    new_strategies = [
        {"symbol": pr['market'], "timeframe": pr['timeframe'], "active": True}
        for pr in selected
    ]

    live = settings.setdefault('live_trading_settings', {})
    live['active_strategies'] = new_strategies

    try:
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(settings, f, indent=2)
        print(f"\n{G}✓ settings.json aktualisiert — {len(new_strategies)} Strategie(n) eingetragen.{NC}\n")
        return True
    except Exception as e:
        print(f"{R}Fehler beim Schreiben von settings.json: {e}{NC}")
        return False


def main():
    parser = argparse.ArgumentParser(description="dnabot Portfolio Optimizer")
    parser.add_argument('--capital',    type=float, default=1000.0)
    parser.add_argument('--risk',       type=float, default=1.0)
    parser.add_argument('--max-dd',     type=float, default=30.0,
                        help="Maximaler Drawdown in %")
    parser.add_argument('--start-date', type=str,   default=None)
    parser.add_argument('--end-date',   type=str,   default=None)
    parser.add_argument('--auto-write', action='store_true',
                        help="Ohne Nachfrage direkt in settings.json schreiben")
    args = parser.parse_args()

    date_range = ""
    if args.start_date or args.end_date:
        date_range = f" | {args.start_date or '...'} → {args.end_date or 'heute'}"

    print(f"\n{'─' * 70}")
    print(f"{B}  dnabot Automatische Portfolio-Optimierung{NC}")
    print(f"  Ziel: Maximaler Profit bei maximal {args.max_dd:.1f}% Drawdown.{date_range}")
    print(f"{'─' * 70}\n")

    print("  Lade Backtest-Ergebnisse ...", end='', flush=True)
    all_results = load_all_results(args.start_date, args.end_date)
    if not all_results:
        print(f"\n{R}  Keine Backtest-Ergebnisse gefunden.{NC}")
        print("  Zuerst Mode 1 (Einzel-Backtest) ausführen!\n")
        sys.exit(1)

    # Nur Pairs mit Trades
    with_trades = [r for r in all_results if len(r['trades']) > 0]
    print(f" {len(all_results)} Dateien geladen ({len(with_trades)} mit Trades).")

    print("  Optimiere Portfolio (Greedy-Suche) ...\n")
    selected = greedy_optimize(with_trades, args.capital, args.risk, args.max_dd)

    if selected:
        portfolio_metrics = simulate_portfolio(selected, args.capital, args.risk)
    else:
        portfolio_metrics = {'total_pnl_pct': 0, 'final_equity': args.capital,
                             'max_dd': 0, 'n_trades': 0, 'win_rate': 0}

    print_optimization_result(selected, portfolio_metrics, args.capital, args.risk)

    if not selected:
        sys.exit(0)

    if args.auto_write:
        write_to_settings(selected)
    else:
        try:
            ans = input("  Sollen die optimalen Ergebnisse automatisch in settings.json eingetragen werden? (j/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans in ('j', 'ja', 'y', 'yes'):
            write_to_settings(selected)
        else:
            print(f"\n{Y}  settings.json wurde NICHT geändert.{NC}\n")


if __name__ == '__main__':
    main()
