# src/dnabot/analysis/show_results.py
# Zeigt Backtest-Ergebnisse und Genome-Library-Stats an
import os
import sys
import json
import glob

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')


def show_backtest_results():
    result_files = glob.glob(os.path.join(RESULTS_DIR, 'backtest_*.json'))
    if not result_files:
        print("Keine Backtest-Ergebnisse gefunden.")
        return

    print(f"\n{'=' * 70}")
    print(f"  BACKTEST ERGEBNISSE — dnabot")
    print(f"{'=' * 70}")

    summaries = []
    for path in sorted(result_files):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            stats = data.get('stats', {})
            summaries.append({
                'market': data.get('market', '?'),
                'timeframe': data.get('timeframe', '?'),
                'trades': stats.get('total_trades', 0),
                'win_rate': stats.get('win_rate', 0),
                'pnl_pct': stats.get('total_pnl_pct', 0),
                'profit_factor': stats.get('profit_factor', 0),
                'max_dd': stats.get('max_drawdown_pct', 0),
                'run_at': data.get('run_at', ''),
            })
        except Exception as e:
            print(f"Fehler beim Lesen von {path}: {e}")

    summaries.sort(key=lambda x: x['pnl_pct'], reverse=True)

    print(f"  {'Markt':<20} {'TF':<5} {'Trades':>6} {'WR':>7} {'PnL%':>8} {'PF':>6} {'MaxDD':>7}")
    print(f"  {'-' * 63}")
    for s in summaries:
        pnl_sign = '+' if s['pnl_pct'] >= 0 else ''
        print(
            f"  {s['market']:<20} {s['timeframe']:<5} "
            f"{s['trades']:>6} {s['win_rate']:>6.1%} "
            f"{pnl_sign}{s['pnl_pct']:>7.1f}% "
            f"{s['profit_factor']:>6.2f} "
            f"{s['max_dd']:>6.1f}%"
        )
    print(f"{'=' * 70}\n")


def show_genome_library():
    from dnabot.genome.database import GenomeDB
    from dnabot.genome.evolver import print_genome_report

    if not os.path.exists(DB_PATH):
        print("Genome-Datenbank nicht gefunden. Erst scan_and_learn.py ausführen.")
        return

    db = GenomeDB(DB_PATH)
    print_genome_report(db)
    db.close()


if __name__ == "__main__":
    show_backtest_results()
    show_genome_library()
