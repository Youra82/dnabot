#!/usr/bin/env python3
"""
analysis/regime_analysis.py — Regime Performance Analysis (Option 13)

Zeigt in welchen Marktregimen (TREND/RANGE/NEUTRAL) der Bot pro Coin gut/schlecht performt.
Quelle: Genome-Datenbank (occ_trend, wins_trend etc.)
"""
import os, sys, argparse
from collections import defaultdict
from analysis.utils import *

REGIMES = ['TREND', 'RANGE', 'NEUTRAL']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-samples', type=int, default=10)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Regime Performance Analysis\n{'='*60}")

    db      = load_genome_db()
    genomes = [g for g in db.get_all_genomes() if g['active']]
    db.close()

    if not genomes:
        print(f"  {R}Keine aktiven Genome.{NC}"); sys.exit(1)

    # Pro Markt: Win-Rate per Regime
    market_regime = defaultdict(lambda: {r: {'wins': 0, 'occ': 0} for r in REGIMES})
    for g in genomes:
        key = f"{g['market'].split('/')[0]}/{g['timeframe']}"
        for reg in REGIMES:
            occ  = g.get(f'occ_{reg.lower()}', 0) or 0
            wins = g.get(f'wins_{reg.lower()}', 0) or 0
            if occ >= args.min_samples:
                market_regime[key][reg]['occ']  += occ
                market_regime[key][reg]['wins'] += wins

    if not market_regime:
        print(f"  {R}Keine Regime-Daten (min_samples={args.min_samples}).{NC}"); sys.exit(1)

    markets = sorted(market_regime.keys())
    wr_matrix = []
    for m in markets:
        row = []
        for reg in REGIMES:
            d   = market_regime[m][reg]
            occ = d['occ']
            row.append(d['wins'] / occ if occ >= args.min_samples else float('nan'))
        wr_matrix.append(row)

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, max(6, len(markets) * 0.4 + 2)))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    mat = np.array(wr_matrix, dtype=float)
    masked = np.ma.masked_invalid(mat)
    im = ax1.imshow(masked, cmap='RdYlGn', vmin=0.3, vmax=0.7, aspect='auto')
    ax1.set_xticks(range(len(REGIMES))); ax1.set_xticklabels(REGIMES, color='white')
    ax1.set_yticks(range(len(markets))); ax1.set_yticklabels(markets, fontsize=8, color='white')
    plt.colorbar(im, ax=ax1, label='Win-Rate')
    for i in range(len(markets)):
        for j in range(len(REGIMES)):
            if not np.isnan(mat[i, j]):
                ax1.text(j, i, f'{mat[i,j]:.0%}', ha='center', va='center',
                         color='black' if 0.3 < mat[i,j] < 0.7 else 'white', fontsize=7)
    ax1.set_title('Win-Rate Heatmap: Coin × Regime\n(grün=gut, rot=schlecht, grau=zu wenig Daten)')

    # Beste Regimes pro Coin
    best_regimes = {}
    for m, row in zip(markets, wr_matrix):
        valid = [(wr, reg) for wr, reg in zip(row, REGIMES) if not np.isnan(wr)]
        if valid:
            best_wr, best_reg = max(valid)
            best_regimes[m] = (best_reg, best_wr)

    avg_by_regime = {}
    for j, reg in enumerate(REGIMES):
        vals = [mat[i, j] for i in range(len(markets)) if not np.isnan(mat[i, j])]
        avg_by_regime[reg] = sum(vals) / len(vals) if vals else 0

    ax2.bar(REGIMES, [avg_by_regime[r] for r in REGIMES],
            color=['#2563eb', '#16a34a', '#f59e0b'], alpha=0.8)
    ax2.axhline(0.5, color='#ef4444', linestyle='--', linewidth=1.5, label='Break-Even 50%')
    for i, (reg, val) in enumerate(avg_by_regime.items()):
        ax2.text(i, val + 0.005, f'{val:.1%}', ha='center', va='bottom', color='white', fontsize=11)
    ax2.set_xlabel('Markt-Regime')
    ax2.set_ylabel('Durchschnittliche Win-Rate')
    ax2.set_title('Ø Win-Rate pro Regime (alle Coins)')
    ax2.legend(facecolor='#1e293b', labelcolor='white')
    ax2.set_ylim(0, max(avg_by_regime.values()) * 1.15)

    fig.suptitle(f'dnabot Regime Performance | {len(markets)} Pairs | '
                 f'min_samples={args.min_samples}', color='white', fontsize=11)
    plt.tight_layout()

    best_regime = max(avg_by_regime, key=avg_by_regime.get)
    caption = (f"dnabot Regime Performance Analysis\n"
               f"{len(markets)} Pairs | min_samples={args.min_samples}\n\n"
               f"Ø Win-Rate pro Regime:\n"
               + "\n".join(f"  {reg}: {avg_by_regime[reg]:.1%}" for reg in REGIMES)
               + f"\n\nBestes Regime: {best_regime} ({avg_by_regime[best_regime]:.1%})")
    save_send(fig, 'regime_analysis', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
