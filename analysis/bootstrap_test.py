#!/usr/bin/env python3
"""
analysis/bootstrap_test.py — Bootstrap Signifikanztest (Option 4)

Testet ob die Genome-Win-Raten statistisch signifikant über dem Zufallsniveau (50%)
liegen. Verwendet den Binomial-Test pro Genome.

Frage: Wie viele der "aktiven" Genome können ihr Ergebnis NICHT durch Zufall erklären?
"""
import os, sys, json, argparse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))
from analysis.utils import *

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-samples', type=int,   default=10)
    parser.add_argument('--alpha',       type=float, default=0.05)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Bootstrap Signifikanztest\n{'='*60}")
    print(f"  Min. Samples: {args.min_samples} | Alpha: {args.alpha}")
    print()

    try:
        from scipy import stats
    except ImportError:
        print(f"  {R}scipy fehlt: pip install scipy{NC}")
        sys.exit(1)

    db = load_genome_db()
    genomes = [g for g in db.get_all_genomes() if g['active'] and g['total_occurrences'] >= args.min_samples]
    db.close()

    if not genomes:
        print(f"  {R}Keine aktiven Genome gefunden.{NC}"); sys.exit(1)

    print(f"  {len(genomes)} aktive Genome mit ≥{args.min_samples} Samples geladen.")

    results = []
    for g in genomes:
        n, w = g['total_occurrences'], g['wins']
        wr = w / n
        # Einseitig: ist WR > 50% signifikant?
        res = stats.binomtest(w, n, p=0.5, alternative='greater')
        results.append({'seq': g['sequence'][:20], 'market': g['market'], 'tf': g['timeframe'],
                         'dir': g['direction'], 'wr': wr, 'n': n, 'p': res.pvalue, 'score': g['score']})

    sig  = [r for r in results if r['p'] < args.alpha]
    nsig = [r for r in results if r['p'] >= args.alpha]

    print(f"  Signifikant (p<{args.alpha}): {G}{len(sig)}{NC} / {len(results)} "
          f"({len(sig)/len(results)*100:.1f}%)")
    print(f"  Nicht signifikant:         {Y}{len(nsig)}{NC} / {len(results)}")
    print()

    # Top signifikante
    top = sorted(sig, key=lambda x: x['p'])[:10]
    print(f"  Top 10 signifikanteste Genome:")
    for r in top:
        print(f"  p={r['p']:.4f} | WR={r['wr']:.1%} | n={r['n']:4d} | "
              f"{r['dir']:<5} {r['market']} {r['tf']}")

    # Chart
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    wrs = [r['wr'] for r in results]
    ps  = [r['p']  for r in results]
    ns  = [r['n']  for r in results]
    colors_p = ['#16a34a' if p < args.alpha else '#64748b' for p in ps]

    ax1.scatter(ns, wrs, c=colors_p, alpha=0.6, s=20)
    ax1.axhline(0.5, color='#ef4444', linestyle='--', linewidth=1.5, label='Baseline 50%')
    ax1.set_xlabel('Sample-Größe (n)')
    ax1.set_ylabel('Win-Rate')
    ax1.set_title(f'Win-Rate vs. Sample-Größe\n'
                  f'{G if len(sig) > 0 else R}{len(sig)} signifikant (grün){NC}, '
                  f'{len(nsig)} nicht signifikant (grau)')
    ax1.legend(facecolor='#1e293b', labelcolor='white')

    ax2.hist(ps, bins=40, color='#2563eb', alpha=0.8, edgecolor='none')
    ax2.axvline(args.alpha, color='#ef4444', linewidth=2, linestyle='--',
                label=f'α = {args.alpha}')
    ax2.set_xlabel('p-Wert')
    ax2.set_ylabel('Anzahl Genome')
    ax2.set_title('p-Wert Verteilung aller aktiven Genome')
    ax2.legend(facecolor='#1e293b', labelcolor='white')

    pct = len(sig)/len(results)*100
    fig.suptitle(f'dnabot Bootstrap Signifikanztest | {len(results)} Genome | '
                 f'{pct:.1f}% statistisch signifikant (p<{args.alpha})',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Bootstrap Signifikanztest\n"
               f"{len(results)} aktive Genome analysiert\n"
               f"Signifikant (p<{args.alpha}): {len(sig)} ({pct:.1f}%)\n"
               f"Nicht signifikant: {len(nsig)} ({100-pct:.1f}%)\n"
               f"Interpretation: Nur signifikante Genome haben echte Vorhersagekraft.")
    save_send(fig, 'bootstrap_test', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
