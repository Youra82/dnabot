#!/usr/bin/env python3
"""
analysis/time_analysis.py — Tageszeit-Analyse (Option 17)

Zeigt zu welchen Uhrzeiten (UTC) und Wochentagen der Bot am besten performt.
Hilft schlechte Handelszeiten zu identifizieren und zu vermeiden.
"""
import os, sys, argparse
from collections import defaultdict
from analysis.utils import *

DAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Tageszeit-Analyse\n{'='*60}")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    # Stunden- und Wochentag-Statistiken
    hour_stats = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    day_stats  = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})

    for t in trades:
        h   = t['entry_dt'].hour
        d   = t['entry_dt'].weekday()
        win = t.get('outcome') == 'WIN'
        pnl = t.get('pnl_pct', 0.0)
        hour_stats[h]['total'] += 1
        hour_stats[h]['pnl']   += pnl
        if win:
            hour_stats[h]['wins'] += 1
        day_stats[d]['total'] += 1
        day_stats[d]['pnl']   += pnl
        if win:
            day_stats[d]['wins'] += 1

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.patch.set_facecolor('#0f172a')
    style_axes(*[ax for row in axes for ax in row])

    hours   = list(range(24))
    h_wrs   = [hour_stats[h]['wins'] / max(hour_stats[h]['total'], 1) for h in hours]
    h_tots  = [hour_stats[h]['total'] for h in hours]
    h_avpnl = [hour_stats[h]['pnl'] / max(hour_stats[h]['total'], 1) for h in hours]

    # Heatmap vorbereiten (24h × 7 Tage)
    heatmap = np.zeros((7, 24))
    for t in trades:
        h = t['entry_dt'].hour
        d = t['entry_dt'].weekday()
        heatmap[d, h] += 1 if t.get('outcome') == 'WIN' else 0

    heatmap_total = np.zeros((7, 24))
    for t in trades:
        heatmap_total[t['entry_dt'].weekday(), t['entry_dt'].hour] += 1

    with np.errstate(divide='ignore', invalid='ignore'):
        wr_heatmap = np.where(heatmap_total > 0, heatmap / heatmap_total, np.nan)

    # Panel 1: Win-Rate nach Stunde
    bar_cols = ['#16a34a' if w > 0.50 else ('#f59e0b' if w > 0.44 else '#ef4444') for w in h_wrs]
    axes[0][0].bar(hours, h_wrs, color=bar_cols, alpha=0.8)
    axes[0][0].axhline(0.5, color='white', linestyle='--', linewidth=1, alpha=0.5)
    axes[0][0].set_xlabel('Stunde (UTC)')
    axes[0][0].set_ylabel('Win-Rate')
    axes[0][0].set_title('Win-Rate nach Tageszeit (UTC)')
    axes[0][0].set_xticks(hours); axes[0][0].set_xticklabels([str(h) for h in hours], fontsize=7)

    # Panel 2: Anzahl Trades nach Stunde
    axes[0][1].bar(hours, h_tots, color='#2563eb', alpha=0.8)
    axes[0][1].set_xlabel('Stunde (UTC)')
    axes[0][1].set_ylabel('Anzahl Trades')
    axes[0][1].set_title('Handelsvolumen nach Tageszeit')
    axes[0][1].set_xticks(hours); axes[0][1].set_xticklabels([str(h) for h in hours], fontsize=7)

    # Panel 3: Win-Rate nach Wochentag
    d_wrs  = [day_stats[d]['wins'] / max(day_stats[d]['total'], 1) for d in range(7)]
    d_tots = [day_stats[d]['total'] for d in range(7)]
    dcols  = ['#16a34a' if w > 0.50 else ('#f59e0b' if w > 0.44 else '#ef4444') for w in d_wrs]
    axes[1][0].bar(DAYS, d_wrs, color=dcols, alpha=0.8)
    axes[1][0].axhline(0.5, color='white', linestyle='--', linewidth=1, alpha=0.5)
    for i, (w, n) in enumerate(zip(d_wrs, d_tots)):
        axes[1][0].text(i, w + 0.005, f'{w:.1%}\n({n})', ha='center', va='bottom',
                         color='white', fontsize=8)
    axes[1][0].set_xlabel('Wochentag')
    axes[1][0].set_ylabel('Win-Rate')
    axes[1][0].set_title('Win-Rate nach Wochentag')

    # Panel 4: Heatmap Wochentag × Stunde
    masked = np.ma.masked_invalid(wr_heatmap)
    im = axes[1][1].imshow(masked, cmap='RdYlGn', vmin=0.3, vmax=0.7, aspect='auto')
    axes[1][1].set_xticks(range(0, 24, 2)); axes[1][1].set_xticklabels(range(0, 24, 2), fontsize=7)
    axes[1][1].set_yticks(range(7)); axes[1][1].set_yticklabels(DAYS, color='white')
    plt.colorbar(im, ax=axes[1][1], label='Win-Rate')
    axes[1][1].set_xlabel('Stunde (UTC)')
    axes[1][1].set_title('Heatmap: Wochentag × Stunde (Win-Rate)')

    best_h = max(hours, key=lambda h: hour_stats[h]['wins'] / max(hour_stats[h]['total'], 1)
                 if hour_stats[h]['total'] >= 5 else 0)
    best_d = max(range(7), key=lambda d: day_stats[d]['wins'] / max(day_stats[d]['total'], 1)
                 if day_stats[d]['total'] >= 5 else 0)

    fig.suptitle(f'dnabot Tageszeit-Analyse (UTC) | {len(trades)} Trades | '
                 f'Beste Stunde: {best_h}:00 | Bester Tag: {DAYS[best_d]}',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Tageszeit-Analyse (UTC)\n"
               f"{len(trades)} Trades\n\n"
               f"Win-Rate nach Wochentag:\n"
               + "\n".join(f"  {DAYS[d]}: {d_wrs[d]:.1%} ({day_stats[d]['total']} Trades)"
                            for d in range(7))
               + f"\n\nBeste Stunde: {best_h}:00 UTC ({h_wrs[best_h]:.1%})"
               + f"\nBester Tag: {DAYS[best_d]} ({d_wrs[best_d]:.1%})")
    save_send(fig, 'time_analysis', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
