#!/usr/bin/env python3
"""
analysis/volatility_filter.py — Volatilitäts-Filter Optimierung (Option 16)

Nutzt sl_pct als Volatilitäts-Proxy: breiter SL = hohe Volatilität.
Testet verschiedene Schwellwerte: Trades mit sl_pct > X werden herausgefiltert.
"""
import os, sys, argparse
import numpy as np
from analysis.utils import *

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Volatilitäts-Filter Optimierung\n{'='*60}")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    sl_pcts = [t.get('sl_pct', 1.0) for t in trades]
    print(f"  {len(trades)} Trades | SL% Median: {np.median(sl_pcts):.2f}% | "
          f"Max: {max(sl_pcts):.2f}%")

    # Schwellwerte: Perzentile des sl_pct
    percentiles  = [50, 60, 70, 75, 80, 85, 90, 95, 100]
    thresholds   = [np.percentile(sl_pcts, p) for p in percentiles]

    print(f"\n  {'SL%-Max':>10} {'Perzentil':>10} {'Trades':>8} {'WR':>7} "
          f"{'PnL%':>8} {'Calmar':>8}")
    print(f"  {'-'*56}")

    results = []
    for pct, thresh in zip(percentiles, thresholds):
        filtered = [t for t in trades if t.get('sl_pct', 1.0) <= thresh]
        if len(filtered) < 10:
            continue
        r = simulate(filtered, args.capital, args.risk)
        wins = sum(1 for t in filtered if t.get('outcome') == 'WIN')
        wr   = wins / len(filtered)
        results.append({'pct': pct, 'thresh': thresh, 'n': len(filtered),
                         'wr': wr, 'pnl': r['pnl_pct'], 'calmar': r['calmar']})
        col = G if r['pnl_pct'] > 0 else R
        print(f"  {thresh:>9.2f}%  {pct:>9}%  {len(filtered):>8}  "
              f"{wr:>6.1%}  {col}{r['pnl_pct']:>+7.1f}%{NC}  {r['calmar']:>8.1f}")

    best = max(results, key=lambda x: x['calmar']) if results else None

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(*axes)

    lbls    = [f"p{r['pct']}" for r in results]
    calmars = [r['calmar'] for r in results]
    wrs     = [r['wr']     for r in results]
    pnls    = [r['pnl']    for r in results]
    ns      = [r['n']      for r in results]

    # Histogramm sl_pct
    axes[0].hist(sl_pcts, bins=50, color='#2563eb', alpha=0.8, edgecolor='none')
    if best:
        axes[0].axvline(best['thresh'], color='#fbbf24', linewidth=2, linestyle='--',
                        label=f'Optimal: {best["thresh"]:.2f}%')
    axes[0].set_xlabel('SL% (Volatilitäts-Proxy)')
    axes[0].set_ylabel('Anzahl Trades')
    axes[0].set_title('Verteilung der SL-Distanzen')
    axes[0].legend(facecolor='#1e293b', labelcolor='white')

    bar_cols = ['#fbbf24' if r.get('pct') == (best['pct'] if best else -1) else '#2563eb'
                for r in results]
    axes[1].bar(lbls, calmars, color=bar_cols, alpha=0.8)
    axes[1].set_xlabel('SL%-Schwelle (Perzentil)')
    axes[1].set_ylabel('Calmar')
    axes[1].set_title('Calmar pro Volatilitäts-Schwelle\n(gelb = optimal)')

    axes[2].plot(lbls, wrs, color='#16a34a', marker='o', linewidth=2, label='Win-Rate')
    ax2r = axes[2].twinx()
    ax2r.plot(lbls, ns, color='#f59e0b', marker='s', linewidth=2, linestyle='--', label='Anzahl Trades')
    ax2r.tick_params(colors='#f59e0b')
    axes[2].axhline(0.5, color='#ef4444', linestyle='--', linewidth=1)
    axes[2].set_xlabel('SL%-Schwelle')
    axes[2].set_ylabel('Win-Rate', color='#16a34a')
    axes[2].set_title('Win-Rate & Handelsvolumen')
    axes[2].legend(facecolor='#1e293b', labelcolor='white', loc='lower left')
    ax2r.legend(facecolor='#1e293b', labelcolor='white', loc='upper right')

    fig.suptitle(f'dnabot Volatilitäts-Filter | {len(trades)} Trades | '
                 f'Optimal: SL≤{best["thresh"]:.2f}% (p{best["pct"]})',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Volatilitäts-Filter Optimierung\n"
               f"{len(trades)} Trades | sl_pct als Volatilitäts-Proxy\n\n"
               + "\n".join(f"p{r['pct']} (SL≤{r['thresh']:.2f}%): "
                            f"Calmar {r['calmar']:.1f} | WR {r['wr']:.1%} | {r['n']} Trades"
                            for r in results)
               + (f"\n\n★ Optimal: SL≤{best['thresh']:.2f}% (Calmar {best['calmar']:.1f})"
                  if best else ""))
    save_send(fig, 'volatility_filter', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
