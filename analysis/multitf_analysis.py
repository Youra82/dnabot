#!/usr/bin/env python3
"""
analysis/multitf_analysis.py — Multi-Timeframe Confirmation (Option 9)

Untersucht ob Trades die gleichzeitig auf mehreren Coins/Timeframes signalisieren
eine höhere Win-Rate haben als einzelne Signale.
"""
import os, sys, argparse
from collections import defaultdict
from analysis.utils import *

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-hours', type=int,   default=2,
                        help='Zeitfenster in Stunden für gleichzeitige Signale')
    parser.add_argument('--no-telegram',  action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Multi-TF Confirmation Analyse\n{'='*60}")
    print(f"  Zeitfenster: ±{args.window_hours}h für gleichzeitige Signale")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    from datetime import timedelta
    window = timedelta(hours=args.window_hours)

    # Zähle für jeden Trade wie viele andere Trades im Zeitfenster lagen --
    # nur auf ANDEREN Markt/Timeframe-Paaren (wie confluence.py), sonst zählen
    # zwei rein sequenzielle Trades desselben Pairs faelschlich als "mehrere
    # Coins bestaetigen sich", obwohl das Script genau diese Multi-Symbol-
    # Bestaetigung untersuchen soll (siehe Docstring).
    concurrent_counts = []
    for i, t in enumerate(trades):
        t_dt = t['entry_dt']
        count = sum(1 for j, other in enumerate(trades)
                    if i != j
                    and abs((other['entry_dt'] - t_dt).total_seconds()) <= window.total_seconds()
                    and other.get('market', '') != t.get('market', ''))
        concurrent_counts.append(count)

    # Win-Rate nach Confluence-Level
    max_conf = min(max(concurrent_counts), 8)
    conf_stats = defaultdict(lambda: {'wins': 0, 'total': 0})
    for t, cnt in zip(trades, concurrent_counts):
        level = min(cnt, max_conf)
        conf_stats[level]['total'] += 1
        if t.get('outcome') == 'WIN':
            conf_stats[level]['wins'] += 1

    print(f"\n  {'Signale gleichzeitig':>20} {'Trades':>7} {'Win-Rate':>9}")
    print(f"  {'-'*40}")
    for lvl in sorted(conf_stats):
        s = conf_stats[lvl]
        wr = s['wins'] / s['total'] if s['total'] > 0 else 0
        col = G if wr > 0.46 else (Y if wr > 0.42 else R)
        print(f"  {lvl:>20}   {s['total']:>7}  {col}{wr:>8.1%}{NC}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    lvls = sorted(conf_stats)
    wrs  = [conf_stats[l]['wins'] / conf_stats[l]['total']
            if conf_stats[l]['total'] > 0 else 0 for l in lvls]
    tots = [conf_stats[l]['total'] for l in lvls]

    bar_cols = ['#16a34a' if w > 0.46 else ('#f59e0b' if w > 0.42 else '#ef4444') for w in wrs]
    ax1.bar([str(l) for l in lvls], wrs, color=bar_cols, alpha=0.8)
    ax1.axhline(0.5, color='#ef4444', linestyle='--', linewidth=1.5, label='Break-Even 50%')
    for i, (l, wr) in enumerate(zip(lvls, wrs)):
        ax1.text(i, wr + 0.002, f'{wr:.1%}', ha='center', va='bottom', color='white', fontsize=9)
    ax1.set_xlabel('Anzahl gleichzeitiger Signale')
    ax1.set_ylabel('Win-Rate')
    ax1.set_title(f'Win-Rate nach Confluence-Level (±{args.window_hours}h Fenster)')
    ax1.legend(facecolor='#1e293b', labelcolor='white')

    ax2.bar([str(l) for l in lvls], tots, color='#2563eb', alpha=0.8)
    ax2.set_xlabel('Anzahl gleichzeitiger Signale')
    ax2.set_ylabel('Anzahl Trades')
    ax2.set_title('Handelsvolumen pro Confluence-Level')

    total = sum(tots)
    fig.suptitle(f'dnabot Multi-TF Confirmation | {total} Trades | '
                 f'Zeitfenster: ±{args.window_hours}h', color='white', fontsize=11)
    plt.tight_layout()

    best_lvl = max(lvls, key=lambda l: conf_stats[l]['wins']/max(conf_stats[l]['total'],1))
    caption = (f"dnabot Multi-TF Confirmation\n"
               f"{total} Trades | ±{args.window_hours}h Zeitfenster\n\n"
               + "\n".join(f"{l} gleichzeitige Signale: WR {conf_stats[l]['wins']/max(conf_stats[l]['total'],1):.1%} "
                            f"({conf_stats[l]['total']} Trades)" for l in sorted(conf_stats))
               + f"\n\nBeste Confluence: {best_lvl} gleichzeitige Signale")
    save_send(fig, 'multitf', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
