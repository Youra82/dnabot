#!/usr/bin/env python3
"""
analysis/kelly_sizing.py — Kelly Position Sizing (Option 12)

Berechnet die mathematisch optimale Positionsgröße (Kelly Criterion) pro Pair
und vergleicht mit der aktuellen fixen Risk%-Strategie.

Kelly% = p - (1-p)/b   wobei p = Win-Rate, b = R:R (z.B. 2.0)
"""
import os, sys, argparse
from analysis.utils import *

def kelly(wr, rr):
    """Volle Kelly-Fraction in %."""
    if rr <= 0:
        return 0.0
    k = wr - (1 - wr) / rr
    return max(k * 100.0, 0.0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--half-kelly',  action='store_true', default=True,
                        help='Half Kelly (sicherer, Standard: an)')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Kelly Position Sizing\n{'='*60}")
    print(f"  Aktuelles Risk%: {args.risk}% | Half-Kelly: {args.half_kelly}")

    pair_results = load_trades()
    if not pair_results:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    rr = load_settings().get('risk_settings', {}).get('rr_ratio', 2.0)

    pair_stats = []
    for r in pair_results:
        trades = r['trades']
        if len(trades) < 5:
            continue
        wins = sum(1 for t in trades if t.get('outcome') == 'WIN')
        wr   = wins / len(trades)
        k    = kelly(wr, rr)
        if args.half_kelly:
            k /= 2.0
        # Simulation: fixer Risk% vs Kelly%
        fixed_r  = simulate(trades, args.capital, args.risk, rr=rr)
        kelly_r  = simulate(trades, args.capital, k, rr=rr) if k > 0 else fixed_r
        pair_stats.append({
            'label':       f"{r['coin']}/{r['timeframe']}",
            'wr':          wr,
            'kelly_pct':   k,
            'fixed_calmar': fixed_r['calmar'],
            'kelly_calmar': kelly_r['calmar'],
            'fixed_pnl':   fixed_r['pnl_pct'],
            'kelly_pnl':   kelly_r['pnl_pct'],
        })

    pair_stats.sort(key=lambda x: x['kelly_pct'], reverse=True)

    print(f"\n  {'Pair':<22} {'WR':>6} {'Kelly%':>8} {'Calmar(fix)':>12} {'Calmar(kelly)':>14}")
    print(f"  {'-'*66}")
    for p in pair_stats[:20]:
        diff = p['kelly_calmar'] - p['fixed_calmar']
        col  = G if diff > 0 else R
        print(f"  {p['label']:<22} {p['wr']:>5.1%} {p['kelly_pct']:>7.1f}%  "
              f"{p['fixed_calmar']:>11.1f}  {col}{p['kelly_calmar']:>13.1f}{NC}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    n       = min(len(pair_stats), 15)
    top     = pair_stats[:n]
    labels  = [p['label'] for p in top]
    kellys  = [p['kelly_pct'] for p in top]
    x       = np.arange(n)
    w       = 0.35

    ax1.bar(x - w/2, [p['fixed_calmar'] for p in top], w, label=f'Fix {args.risk}%',
            color='#2563eb', alpha=0.8)
    ax1.bar(x + w/2, [p['kelly_calmar'] for p in top], w, label='Kelly%',
            color='#16a34a', alpha=0.8)
    ax1.set_xticks(x); ax1.set_xticklabels(labels, rotation=45, ha='right', fontsize=8)
    ax1.set_ylabel('Calmar Score')
    ax1.set_title(f'Calmar: Fix {args.risk}% vs {"Half-" if args.half_kelly else ""}Kelly%')
    ax1.legend(facecolor='#1e293b', labelcolor='white')

    ax2.bar(labels, kellys, color='#7c3aed', alpha=0.8)
    ax2.axhline(args.risk, color='#ef4444', linestyle='--', linewidth=2,
                label=f'Aktuell {args.risk}%')
    ax2.set_xticks(range(n)); ax2.set_xticklabels(labels, rotation=45, ha='right', fontsize=8)
    ax2.set_ylabel('Kelly%')
    ax2.set_title(f'{"Half-" if args.half_kelly else ""}Kelly Position Size pro Pair')
    ax2.legend(facecolor='#1e293b', labelcolor='white')

    fig.suptitle(f'dnabot Kelly Position Sizing | RR={rr} | {len(pair_stats)} Pairs',
                 color='white', fontsize=11)
    plt.tight_layout()

    improved = sum(1 for p in pair_stats if p['kelly_calmar'] > p['fixed_calmar'])
    caption = (f"dnabot Kelly Criterion Analyse\n"
               f"RR={rr} | {'Half-Kelly' if args.half_kelly else 'Full Kelly'}\n"
               f"Verbessert durch Kelly: {improved}/{len(pair_stats)} Pairs\n\n"
               f"Top Kelly%:\n"
               + "\n".join(f"{p['label']}: {p['kelly_pct']:.1f}% (WR {p['wr']:.1%})"
                            for p in pair_stats[:8]))
    save_send(fig, 'kelly_sizing', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
