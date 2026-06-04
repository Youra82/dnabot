#!/usr/bin/env python3
"""
analysis/confluence.py — Confluence Score (Option 15)

Simuliert was passiert wenn man nur dann handelt wenn mehrere Genome-Signale
gleichzeitig in dieselbe Richtung zeigen. Weniger Trades, aber bessere Qualität?
"""
import os, sys, argparse
from datetime import timedelta
from collections import defaultdict
from analysis.utils import *

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',       type=float, default=100.0)
    parser.add_argument('--risk',          type=float, default=2.5)
    parser.add_argument('--window-hours',  type=int,   default=4)
    parser.add_argument('--no-telegram',   action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Confluence Score Analyse\n{'='*60}")
    print(f"  Zeitfenster: ±{args.window_hours}h | Risk: {args.risk}%")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    window_sec = args.window_hours * 3600

    # Für jeden Trade: Confluence-Level (wie viele andere Trades in ±window_hours?)
    for i, t in enumerate(trades):
        t_dt = t['entry_dt']
        concurrent = [other for j, other in enumerate(trades)
                      if i != j
                      and abs((other['entry_dt'] - t_dt).total_seconds()) <= window_sec
                      and other.get('market', '') != t.get('market', '')]
        t['_conf'] = len(concurrent) + 1  # eigenes Signal = 1

    # Simuliere Portfolios nach Mindest-Confluence
    min_levels = [1, 2, 3, 4]
    results    = {}

    for min_conf in min_levels:
        filtered = [t for t in trades if t['_conf'] >= min_conf]
        if not filtered:
            results[min_conf] = None
            continue
        r = simulate(filtered, args.capital, args.risk)
        wins = sum(1 for t in filtered if t.get('outcome') == 'WIN')
        r['n_filtered'] = len(filtered)
        r['wr']         = wins / len(filtered) if filtered else 0
        results[min_conf] = r
        col = G if r['pnl_pct'] > 0 else R
        print(f"  Min {min_conf} Signal(e): {len(filtered):4d} Trades | "
              f"WR {r['wr']:.1%} | {col}PnL {r['pnl_pct']:+.1f}%{NC} | "
              f"Calmar {r['calmar']:.1f}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(*axes)

    valid   = [(k, v) for k, v in results.items() if v is not None]
    lvls    = [str(k) for k, _ in valid]
    wrs     = [v['wr']           for _, v in valid]
    calmars = [v['calmar']       for _, v in valid]
    ns      = [v['n_filtered']   for _, v in valid]

    axes[0].bar(lvls, wrs, color='#2563eb', alpha=0.8)
    axes[0].axhline(0.5, color='#ef4444', linestyle='--', linewidth=1.5)
    for i, w in enumerate(wrs):
        axes[0].text(i, w + 0.005, f'{w:.1%}', ha='center', va='bottom', color='white', fontsize=10)
    axes[0].set_xlabel('Min. gleichzeitige Signale')
    axes[0].set_ylabel('Win-Rate')
    axes[0].set_title('Win-Rate nach Confluence')

    axes[1].bar(lvls, calmars, color='#16a34a', alpha=0.8)
    for i, c in enumerate(calmars):
        axes[1].text(i, c + max(calmars)*0.01, f'{c:.0f}', ha='center', va='bottom', color='white', fontsize=10)
    axes[1].set_xlabel('Min. gleichzeitige Signale')
    axes[1].set_ylabel('Calmar')
    axes[1].set_title('Calmar Score nach Confluence')

    axes[2].bar(lvls, ns, color='#d97706', alpha=0.8)
    for i, n in enumerate(ns):
        axes[2].text(i, n + max(ns)*0.01, str(n), ha='center', va='bottom', color='white', fontsize=10)
    axes[2].set_xlabel('Min. gleichzeitige Signale')
    axes[2].set_ylabel('Anzahl Trades')
    axes[2].set_title('Handelsvolumen nach Confluence')

    fig.suptitle(f'dnabot Confluence Score | {len(trades)} Trades gesamt | ±{args.window_hours}h Fenster',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Confluence Score\n"
               f"{len(trades)} Trades | ±{args.window_hours}h Zeitfenster\n\n"
               + "\n".join(f"Min {k} Signal(e): WR {v['wr']:.1%} | "
                            f"Calmar {v['calmar']:.1f} | {v['n_filtered']} Trades"
                            for k, v in valid))
    save_send(fig, 'confluence', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
