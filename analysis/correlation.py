#!/usr/bin/env python3
"""
analysis/correlation.py — Anti-Korrelations-Portfolio (Option 11)

Berechnet die Korrelation zwischen den wöchentlichen Returns aller Pairs.
Zeigt welche Kombinationen am wenigsten gleichzeitig verlieren.
"""
import os, sys, argparse
from datetime import timedelta, timezone
from collections import defaultdict
from analysis.utils import *

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Anti-Korrelations-Portfolio\n{'='*60}")

    pair_results = load_trades()
    if not pair_results:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    # Wöchentliche Returns pro Pair
    from datetime import datetime
    all_dts = [t['entry_dt'] for r in pair_results for t in r['trades']]
    min_dt  = min(all_dts)
    max_dt  = max(all_dts)

    weeks = []
    w = min_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while w < max_dt:
        weeks.append(w)
        w += timedelta(weeks=1)

    pair_returns = {}
    for r in pair_results:
        label = f"{r['coin']}/{r['timeframe']}"
        weekly_r = []
        for ws in weeks:
            we = ws + timedelta(weeks=1)
            wt = [t for t in r['trades'] if ws <= t['entry_dt'] < we]
            if wt:
                res = simulate(wt, 100.0, args.risk)
                weekly_r.append(res['pnl_pct'])
            else:
                weekly_r.append(0.0)
        pair_returns[label] = weekly_r

    if len(pair_returns) < 2:
        print(f"  {R}Zu wenig Pairs.{NC}"); sys.exit(1)

    # Korrelationsmatrix
    import numpy as np
    labels  = list(pair_returns.keys())
    matrix  = np.array([pair_returns[l] for l in labels])
    corr    = np.corrcoef(matrix)

    # Paare mit geringster Korrelation
    pairs_corr = []
    for i in range(len(labels)):
        for j in range(i+1, len(labels)):
            pairs_corr.append((corr[i,j], labels[i], labels[j]))
    pairs_corr.sort(key=lambda x: x[0])

    print(f"  {len(labels)} Pairs | {len(weeks)} Wochen\n")
    print(f"  Top 10 am wenigsten korrelierte Pairs:")
    for c, a, b in pairs_corr[:10]:
        col = G if c < 0 else (Y if c < 0.3 else R)
        print(f"  {col}ρ={c:>+.3f}{NC}  {a:<20} ↔  {b}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    n = min(len(labels), 20)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    sub_labels = labels[:n]
    sub_corr   = corr[:n, :n]
    im = ax1.imshow(sub_corr, cmap='RdYlGn', vmin=-1, vmax=1, aspect='auto')
    ax1.set_xticks(range(n)); ax1.set_xticklabels(sub_labels, rotation=90, fontsize=7)
    ax1.set_yticks(range(n)); ax1.set_yticklabels(sub_labels, fontsize=7)
    plt.colorbar(im, ax=ax1, label='Korrelation')
    ax1.set_title('Korrelationsmatrix (grün = gut diversifiziert)', color='white')

    top_anti = pairs_corr[:15]
    y_labels = [f"{b}\n↔{a}" for _, a, b in top_anti]
    corr_vals = [c for c, _, _ in top_anti]
    bar_cols = ['#16a34a' if c < 0 else ('#f59e0b' if c < 0.3 else '#ef4444') for c in corr_vals]
    ax2.barh(range(len(y_labels)), corr_vals, color=bar_cols, alpha=0.8)
    ax2.axvline(0, color='white', linewidth=1)
    ax2.set_yticks(range(len(y_labels)))
    ax2.set_yticklabels(y_labels, fontsize=8, color='white')
    ax2.set_xlabel('Korrelation ρ')
    ax2.set_title('15 am wenigsten korrelierte Pair-Kombinationen\n(negativ = ideale Diversifikation)')

    fig.suptitle(f'dnabot Anti-Korrelations-Analyse | {len(labels)} Pairs | {len(weeks)} Wochen',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Anti-Korrelations-Portfolio\n"
               f"{len(labels)} Pairs | {len(weeks)} Wochen\n\n"
               f"Beste Kombinationen (niedrigste Korrelation):\n"
               + "\n".join(f"ρ={c:+.3f}: {a} ↔ {b}" for c, a, b in pairs_corr[:8]))
    save_send(fig, 'correlation', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
