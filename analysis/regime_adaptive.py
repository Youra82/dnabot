#!/usr/bin/env python3
"""
analysis/regime_adaptive.py — Regime-adaptive Parameter (Option 18)

Testet ob verschiedene RR-Ratios pro Timeframe-Gruppe die Performance verbessern.
Schnelle TFs (1h/2h) vs. langsame TFs (4h/6h/1d) haben unterschiedliche Charakteristika.
"""
import os, sys, argparse
from analysis.utils import *

FAST_TFS = ['15m', '30m', '1h', '2h']
SLOW_TFS = ['4h', '6h', '8h', '12h', '1d']

RR_TEST_VALUES = [1.0, 1.5, 2.0, 2.5, 3.0]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Regime-adaptive Parameter\n{'='*60}")

    pair_results = load_trades()
    if not pair_results:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    fast_trades = [t for r in pair_results for t in r['trades'] if r['timeframe'] in FAST_TFS]
    slow_trades = [t for r in pair_results for t in r['trades'] if r['timeframe'] in SLOW_TFS]
    all_t       = [t for r in pair_results for t in r['trades']]

    print(f"  Schnelle TFs ({', '.join(FAST_TFS)}): {len(fast_trades)} Trades")
    print(f"  Langsame TFs ({', '.join(SLOW_TFS)}): {len(slow_trades)} Trades")
    print()

    # Für jede RR-Kombination (fast_rr, slow_rr) simulieren
    best_calmar = -999
    best_combo  = None
    results_fast, results_slow = {}, {}

    for rr in RR_TEST_VALUES:
        rf = simulate(fast_trades, args.capital, args.risk, rr=rr) if fast_trades else {'calmar': 0, 'pnl_pct': 0}
        rs = simulate(slow_trades, args.capital, args.risk, rr=rr) if slow_trades else {'calmar': 0, 'pnl_pct': 0}
        results_fast[rr] = rf
        results_slow[rr] = rs
        print(f"  RR={rr}: Fast Calmar={rf['calmar']:.1f} ({rf['pnl_pct']:+.0f}%) | "
              f"Slow Calmar={rs['calmar']:.1f} ({rs['pnl_pct']:+.0f}%)")

    # Beste Kombination: Fast RR × Slow RR
    best_combo = None
    best_combo_calmar = -999
    for rr_f in RR_TEST_VALUES:
        for rr_s in RR_TEST_VALUES:
            # Gemischte Simulation
            mixed_trades = (
                [(t, rr_f) for t in fast_trades] +
                [(t, rr_s) for t in slow_trades]
            )
            mixed_trades.sort(key=lambda x: x[0].get('entry_dt'))
            equity = args.capital
            peak   = equity
            max_dd = 0.0
            for t, rr_use in mixed_trades:
                sl_pct = max(t.get('sl_pct', 1.0), 0.01)
                ra = min(equity * (args.risk / 100.0), MAX_NOTIONAL_USDT * (sl_pct / 100.0))
                if t.get('outcome') == 'WIN':    pnl = ra * rr_use
                elif t.get('outcome') == 'LOSS': pnl = -ra
                else:                             pnl = ra * (t.get('pnl_pct', 0.0) / sl_pct)
                equity += pnl
                if equity > peak: peak = equity
                if peak > 0:
                    dd = (peak - equity) / peak * 100
                    if dd > max_dd: max_dd = dd
            pnl_pct = (equity - args.capital) / args.capital * 100
            calmar  = pnl_pct / max_dd if max_dd > 0 else pnl_pct
            if calmar > best_combo_calmar:
                best_combo_calmar = calmar
                best_combo = (rr_f, rr_s, pnl_pct, max_dd)

    print(f"\n  ★ Beste Kombination: Fast RR={best_combo[0]} | Slow RR={best_combo[1]}")
    print(f"    Calmar={best_combo_calmar:.1f} | PnL={best_combo[2]:+.1f}% | DD={best_combo[3]:.1f}%")

    # Uniform (RR=2.0 für alle)
    uniform = simulate(all_t, args.capital, args.risk, rr=2.0)
    print(f"  Vergleich Uniform RR=2.0: Calmar={uniform['calmar']:.1f}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(*axes)

    rr_lbls = [str(r) for r in RR_TEST_VALUES]
    axes[0].plot(rr_lbls, [results_fast[r]['calmar'] for r in RR_TEST_VALUES],
                 color='#2563eb', marker='o', linewidth=2, label='Fast TFs')
    axes[0].plot(rr_lbls, [results_slow[r]['calmar'] for r in RR_TEST_VALUES],
                 color='#16a34a', marker='s', linewidth=2, label='Slow TFs')
    axes[0].axhline(uniform['calmar'], color='#f59e0b', linestyle='--', label=f'Uniform RR=2')
    axes[0].set_xlabel('RR-Ratio')
    axes[0].set_ylabel('Calmar')
    axes[0].set_title('Calmar pro RR-Ratio nach TF-Gruppe')
    axes[0].legend(facecolor='#1e293b', labelcolor='white')

    # Heatmap Fast × Slow RR
    heatmap_data = np.zeros((len(RR_TEST_VALUES), len(RR_TEST_VALUES)))
    for i, rr_f in enumerate(RR_TEST_VALUES):
        for j, rr_s in enumerate(RR_TEST_VALUES):
            mixed = ([(t, rr_f) for t in fast_trades] + [(t, rr_s) for t in slow_trades])
            mixed.sort(key=lambda x: x[0].get('entry_dt'))
            eq = args.capital; peak = eq; mdd = 0.0
            for t, ru in mixed:
                sp = max(t.get('sl_pct', 1.0), 0.01)
                ra = min(eq * (args.risk/100.0), MAX_NOTIONAL_USDT * (sp/100.0))
                if t.get('outcome') == 'WIN': pnl = ra * ru
                elif t.get('outcome') == 'LOSS': pnl = -ra
                else: pnl = ra * (t.get('pnl_pct', 0.0) / sp)
                eq += pnl
                if eq > peak: peak = eq
                if peak > 0:
                    dd = (peak-eq)/peak*100
                    if dd > mdd: mdd = dd
            pp = (eq - args.capital) / args.capital * 100
            heatmap_data[i, j] = pp / mdd if mdd > 0 else pp

    im = axes[1].imshow(heatmap_data, cmap='RdYlGn', aspect='auto')
    axes[1].set_xticks(range(len(RR_TEST_VALUES)))
    axes[1].set_xticklabels([str(r) for r in RR_TEST_VALUES], color='white')
    axes[1].set_yticks(range(len(RR_TEST_VALUES)))
    axes[1].set_yticklabels([str(r) for r in RR_TEST_VALUES], color='white')
    axes[1].set_xlabel('Slow TF RR-Ratio')
    axes[1].set_ylabel('Fast TF RR-Ratio')
    axes[1].set_title('Calmar Heatmap: Fast × Slow RR')
    plt.colorbar(im, ax=axes[1])

    cats = ['Uniform\nRR=2.0', f'Optimal\nFast={best_combo[0]}\nSlow={best_combo[1]}']
    vals = [uniform['calmar'], best_combo_calmar]
    axes[2].bar(cats, vals, color=['#2563eb', '#16a34a'], alpha=0.8, width=0.4)
    for i, v in enumerate(vals):
        axes[2].text(i, v + max(vals)*0.01, f'{v:.1f}', ha='center', va='bottom',
                     color='white', fontsize=12, fontweight='bold')
    axes[2].set_ylabel('Calmar')
    axes[2].set_title('Uniform vs. Adaptive RR')

    fig.suptitle(f'dnabot Regime-adaptive RR | Beste Kombi: Fast={best_combo[0]}, Slow={best_combo[1]}',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Regime-adaptive Parameter\n\n"
               f"Beste Kombination:\n"
               f"  Schnelle TFs (1h/2h): RR = {best_combo[0]}\n"
               f"  Langsame TFs (4h+):   RR = {best_combo[1]}\n"
               f"  Calmar: {best_combo_calmar:.1f}\n\n"
               f"Vergleich Uniform RR=2.0: Calmar {uniform['calmar']:.1f}\n"
               f"Verbesserung: {best_combo_calmar - uniform['calmar']:+.1f}")
    save_send(fig, 'regime_adaptive', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
