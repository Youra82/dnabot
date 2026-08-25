#!/usr/bin/env python3
"""
analysis/time_analysis_momentum_exit.py — Tageszeit-Analyse (momentum_exit)

Zeigt zu welchen Uhrzeiten (UTC) und Wochentagen der Bot am besten performt.
Wiederbelebung von analysis/time_analysis.py (Genome-System, beim Cleanup
2026-08-24 entfernt) -- Methodik identisch, Datenquelle auf momentum_exit
umgestellt.
"""
import os
import sys
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from momentum_exit_utils import all_trades_flat, style_axes, save_send, G, Y, R, NC

DAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']


def main():
    parser = argparse.ArgumentParser(description='dnabot Tageszeit-Analyse (momentum_exit)')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'=' * 60}\n  dnabot — Tageszeit-Analyse (momentum_exit)\n{'=' * 60}")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine momentum_exit-Backtest-Daten. Erst ./run_momentum_exit_pipeline.sh ausführen.{NC}")
        sys.exit(1)

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

    hours  = list(range(24))
    h_wrs  = [hour_stats[h]['wins'] / max(hour_stats[h]['total'], 1) for h in hours]
    h_tots = [hour_stats[h]['total'] for h in hours]

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

    bar_cols = ['#16a34a' if w > 0.35 else ('#f59e0b' if w > 0.28 else '#ef4444') for w in h_wrs]
    axes[0][0].bar(hours, h_wrs, color=bar_cols, alpha=0.8)
    axes[0][0].set_xlabel('Stunde (UTC)')
    axes[0][0].set_ylabel('Win-Rate')
    axes[0][0].set_title('Win-Rate nach Tageszeit (UTC)')
    axes[0][0].set_xticks(hours); axes[0][0].set_xticklabels([str(h) for h in hours], fontsize=7)

    axes[0][1].bar(hours, h_tots, color='#2563eb', alpha=0.8)
    axes[0][1].set_xlabel('Stunde (UTC)')
    axes[0][1].set_ylabel('Anzahl Trades')
    axes[0][1].set_title('Handelsvolumen nach Tageszeit')
    axes[0][1].set_xticks(hours); axes[0][1].set_xticklabels([str(h) for h in hours], fontsize=7)

    d_wrs  = [day_stats[d]['wins'] / max(day_stats[d]['total'], 1) for d in range(7)]
    d_tots = [day_stats[d]['total'] for d in range(7)]
    dcols  = ['#16a34a' if w > 0.35 else ('#f59e0b' if w > 0.28 else '#ef4444') for w in d_wrs]
    axes[1][0].bar(DAYS, d_wrs, color=dcols, alpha=0.8)
    for i, (w, n) in enumerate(zip(d_wrs, d_tots)):
        axes[1][0].text(i, w + 0.005, f'{w:.1%}\n({n})', ha='center', va='bottom',
                         color='white', fontsize=8)
    axes[1][0].set_xlabel('Wochentag')
    axes[1][0].set_ylabel('Win-Rate')
    axes[1][0].set_title('Win-Rate nach Wochentag')

    masked = np.ma.masked_invalid(wr_heatmap)
    im = axes[1][1].imshow(masked, cmap='RdYlGn', vmin=0.15, vmax=0.45, aspect='auto')
    axes[1][1].set_xticks(range(0, 24, 2)); axes[1][1].set_xticklabels(range(0, 24, 2), fontsize=7)
    axes[1][1].set_yticks(range(7)); axes[1][1].set_yticklabels(DAYS, color='white')
    plt.colorbar(im, ax=axes[1][1], label='Win-Rate')
    axes[1][1].set_xlabel('Stunde (UTC)')
    axes[1][1].set_title('Heatmap: Wochentag × Stunde (Win-Rate)')

    best_h = max(hours, key=lambda h: hour_stats[h]['wins'] / max(hour_stats[h]['total'], 1)
                 if hour_stats[h]['total'] >= 5 else 0)
    best_d = max(range(7), key=lambda d: day_stats[d]['wins'] / max(day_stats[d]['total'], 1)
                 if day_stats[d]['total'] >= 5 else 0)

    fig.suptitle(f'dnabot momentum_exit Tageszeit-Analyse (UTC) | {len(trades)} Trades | '
                 f'Beste Stunde: {best_h}:00 | Bester Tag: {DAYS[best_d]}',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot momentum_exit Tageszeit-Analyse (UTC)\n"
               f"{len(trades)} Trades\n\n"
               f"Win-Rate nach Wochentag:\n"
               + "\n".join(f"  {DAYS[d]}: {d_wrs[d]:.1%} ({day_stats[d]['total']} Trades)"
                            for d in range(7))
               + f"\n\nBeste Stunde: {best_h}:00 UTC ({h_wrs[best_h]:.1%})"
               + f"\nBester Tag: {DAYS[best_d]} ({d_wrs[best_d]:.1%})"
               + f"\n\nHinweis: momentum_exit hat kein Vorhersage-Anspruch beim Entry -- "
                 f"eine niedrige Basis-Winrate (~30%) ist erwartet, der Edge steckt im Exit.")
    save_send(fig, 'time_analysis', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
