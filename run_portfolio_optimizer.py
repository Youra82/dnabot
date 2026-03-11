#!/usr/bin/env python3
# run_portfolio_optimizer.py
# Automatische Portfolio-Optimierung:
# Liest gespeicherte Backtest-Ergebnisse, findet die beste
# Kombination von Pairs die den maximalen Profit bei einem
# vorgegebenen Drawdown-Limit liefert.
#
# Modell:
#   - Jedes Pair läuft UNABHÄNGIG auf sub_capital = capital / n_pairs
#   - Risiko pro Trade = risk_pct% des Sub-Kapitals
#   - Combined-Equity = Summe aller Sub-Equities
#   - MaxDD wird auf der kombinierten Equity-Kurve gemessen
#   - Portfolio-PnL% = Durchschnitt der Einzel-PnL%s
#
# Constraints:
#   - Max. 1 Timeframe pro Coin (Bitget erlaubt nur 1 Position/Coin)

import os
import sys
import json
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

G   = '\033[0;32m'
Y   = '\033[1;33m'
R   = '\033[0;31m'
C   = '\033[0;36m'
B   = '\033[1;37m'
NC  = '\033[0m'

RR_RATIO = 2.0


def coin_from_symbol(symbol: str) -> str:
    return symbol.split('/')[0].upper()


def load_all_results(start_date=None, end_date=None):
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
            'coin':      coin_from_symbol(data['market']),
            'trades':    trades,
            'stats':     data.get('stats', {}),
        })

    return results


def _simulate_pair_equity(trades: list, sub_capital: float, risk_pct: float) -> list:
    """
    Simuliert ein einzelnes Pair auf sub_capital.
    Gibt liste von (exit_time_str, equity) Tupeln zurück.
    """
    eq = sub_capital
    events = []
    for t in sorted(trades, key=lambda x: str(x.get('exit_time', x.get('entry_time', '')))):
        risk_amount = eq * (risk_pct / 100.0)
        outcome = t.get('outcome', 'LOSS')
        sl_pct  = max(t.get('sl_pct', 1.0), 0.01)

        if outcome == 'WIN':
            pnl = risk_amount * RR_RATIO
        elif outcome == 'LOSS':
            pnl = -risk_amount
        else:  # TIMEOUT
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)

        eq += pnl
        ts = str(t.get('exit_time', t.get('entry_time', '')))
        events.append((ts, eq))

    return events


def simulate_portfolio(pair_results: list, capital: float, risk_pct: float) -> dict:
    """
    Simuliert ein Portfolio.

    Jedes Pair bekommt sub_capital = capital / n_pairs.
    Trades laufen unabhängig, Combined-Equity = Summe aller Sub-Equities.
    MaxDD wird auf der kombinierten Equity-Kurve gemessen.
    """
    n = len(pair_results)
    if n == 0:
        return {
            'total_pnl_pct': 0.0, 'final_equity': capital,
            'max_dd': 0.0, 'n_trades': 0, 'win_rate': 0.0,
        }

    sub_capital = capital / n

    # Jeden Pair unabhängig simulieren → (timestamp, equity) Ereignisse
    pair_keys   = []
    pair_events = {}
    current_eq  = {}

    for pr in pair_results:
        key = f"{pr['market']}_{pr['timeframe']}"
        pair_keys.append(key)
        current_eq[key] = sub_capital
        pair_events[key] = _simulate_pair_equity(pr['trades'], sub_capital, risk_pct)

    # Alle Ereignisse chronologisch zusammenführen
    all_events = []
    for key, events in pair_events.items():
        for ts, eq in events:
            all_events.append((ts, key, eq))
    all_events.sort(key=lambda x: x[0])

    # Combined-Equity Drawdown berechnen
    combined = capital
    peak     = combined
    max_dd   = 0.0

    for ts, key, new_eq in all_events:
        combined += new_eq - current_eq[key]
        current_eq[key] = new_eq

        if combined > peak:
            peak = combined
        if peak > 0:
            dd = (peak - combined) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    final_equity  = sum(current_eq.values())
    total_pnl_pct = (final_equity - capital) / capital * 100.0

    n_trades = sum(len(pr['trades']) for pr in pair_results)
    wins     = sum(1 for pr in pair_results for t in pr['trades'] if t.get('outcome') == 'WIN')
    wr       = wins / n_trades if n_trades > 0 else 0.0

    return {
        'total_pnl_pct': total_pnl_pct,
        'final_equity':  final_equity,
        'max_dd':        max_dd,
        'n_trades':      n_trades,
        'win_rate':      wr,
    }


def greedy_optimize(all_results: list, capital: float, risk_pct: float,
                    max_dd_limit: float) -> list:
    """
    Greedy-Suche: fügt nacheinander das Pair hinzu, das den Portfolio-PnL%
    am meisten steigert, solange MaxDD <= max_dd_limit.

    Constraint: max. 1 Timeframe pro Coin (Bitget).
    """
    candidates     = [r for r in all_results if len(r['trades']) > 0]
    selected       = []
    selected_coins = set()
    remaining      = list(candidates)

    while remaining:
        best_pair    = None
        best_pnl     = -1e9

        for pair in remaining:
            if pair['coin'] in selected_coins:
                continue
            # Nur Pairs mit positivem Einzel-PnL berücksichtigen
            if pair['stats'].get('total_pnl_pct', 0) <= 0:
                continue

            trial   = selected + [pair]
            metrics = simulate_portfolio(trial, capital, risk_pct)

            if metrics['max_dd'] > max_dd_limit:
                continue
            if metrics['total_pnl_pct'] > best_pnl:
                best_pnl  = metrics['total_pnl_pct']
                best_pair = pair

        if best_pair is None:
            break

        selected.append(best_pair)
        selected_coins.add(best_pair['coin'])
        remaining.remove(best_pair)

    return selected


def print_optimization_result(selected: list, portfolio_metrics: dict,
                               capital: float, risk_pct: float, max_dd_limit: float):
    w = 72
    print(f"\n{'=' * w}")
    print(f"{B}  dnabot — Automatische Portfolio-Optimierung{NC}")
    print(f"  Ziel: Maximaler Profit bei maximal {max_dd_limit:.1f}% Drawdown.")
    print(f"{'=' * w}")

    if not selected:
        print(f"\n{R}  Kein Portfolio gefunden, das die Bedingungen erfüllt.{NC}")
        print(f"  → Erhöhe das Max-Drawdown-Limit oder führe zuerst Mode 1 aus.\n")
        return

    n = len(selected)
    sub_cap = capital / n
    print(f"\n  {G}Optimales Portfolio — {n} Coins, je {sub_cap:.2f} USDT Kapital{NC}")
    print(f"  Gesamt-Kapital: {capital:.0f} USDT | Risiko/Trade: {risk_pct}%/Sub-Kapital")
    print(f"\n  {'Markt':<24} {'TF':<6} {'Trades':>7} {'WR':>7} {'PnL%':>9} {'MaxDD':>8}")
    print(f"  {'-' * (w - 2)}")

    for pr in sorted(selected, key=lambda x: x['stats'].get('total_pnl_pct', 0), reverse=True):
        st  = pr['stats']
        n_t = st.get('total_trades', 0)
        wr  = st.get('win_rate', 0)
        pnl = st.get('total_pnl_pct', 0)
        dd  = st.get('max_drawdown_pct', 0)
        pnl_col = G if pnl > 0 else R
        wr_col  = G if wr >= 0.50 else (Y if wr >= 0.43 else R)
        sign = '+' if pnl >= 0 else ''
        print(
            f"  {pr['market']:<24} {pr['timeframe']:<6} {n_t:>7} "
            f"{wr_col}{wr:>6.1%}{NC} "
            f"{pnl_col}{sign}{pnl:>7.1f}%{NC} "
            f"{dd:>7.1f}%"
        )

    pm = portfolio_metrics
    pnl_col = G if pm['total_pnl_pct'] > 0 else R
    print(f"\n  {'─' * (w - 2)}")
    print(f"  {B}Portfolio gesamt (kombinierte Equity-Simulation):{NC}")
    print(f"  Trades total:  {pm['n_trades']}")
    print(f"  Win-Rate:      {pm['win_rate']:.1%}")
    print(f"  PnL:           {pnl_col}{'+' if pm['total_pnl_pct'] >= 0 else ''}{pm['total_pnl_pct']:.1f}%{NC}")
    print(f"  Final Equity:  {pm['final_equity']:.2f} USDT")
    print(f"  Max Drawdown:  {pm['max_dd']:.1f}%")
    print(f"{'=' * w}\n")


def write_to_settings(selected: list):
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
    parser.add_argument('--max-dd',     type=float, default=30.0)
    parser.add_argument('--start-date', type=str,   default=None)
    parser.add_argument('--end-date',   type=str,   default=None)
    parser.add_argument('--auto-write', action='store_true')
    args = parser.parse_args()

    date_range = ""
    if args.start_date or args.end_date:
        date_range = f" | {args.start_date or '...'} → {args.end_date or 'heute'}"

    print(f"\n{'─' * 72}")
    print(f"{B}  dnabot Automatische Portfolio-Optimierung{NC}")
    print(f"  Ziel: Maximaler Profit bei maximal {args.max_dd:.1f}% Drawdown.{date_range}")
    print(f"  Modell: Kapital wird gleichmäßig auf alle Coins aufgeteilt")
    print(f"  Constraint: max. 1 Timeframe pro Coin (Bitget-Regel)")
    print(f"{'─' * 72}\n")

    print("  Lade Backtest-Ergebnisse ...", end='', flush=True)
    all_results = load_all_results(args.start_date, args.end_date)
    if not all_results:
        print(f"\n{R}  Keine Backtest-Ergebnisse gefunden.{NC}")
        print("  Zuerst Mode 1 (Einzel-Backtest) ausführen!\n")
        sys.exit(1)

    with_trades = [r for r in all_results if len(r['trades']) > 0]
    coins_available = sorted(set(r['coin'] for r in with_trades))
    print(f" {len(all_results)} Dateien, {len(with_trades)} mit Trades, {len(coins_available)} Coins.")

    print("  Optimiere Portfolio (Greedy, 1 TF/Coin) ...\n")
    selected = greedy_optimize(with_trades, args.capital, args.risk, args.max_dd)

    if selected:
        portfolio_metrics = simulate_portfolio(selected, args.capital, args.risk)
    else:
        portfolio_metrics = {
            'total_pnl_pct': 0, 'final_equity': args.capital,
            'max_dd': 0, 'n_trades': 0, 'win_rate': 0,
        }

    print_optimization_result(selected, portfolio_metrics, args.capital, args.risk, args.max_dd)

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
