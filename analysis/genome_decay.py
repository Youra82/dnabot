#!/usr/bin/env python3
"""
analysis/genome_decay.py — Genome Decay Analysis (Option 10)

Zeigt wie schnell Genome-Muster ihre Vorhersagekraft verlieren.
Validiert den half_life_days Parameter in settings.json.
"""
import os, sys, argparse
from datetime import datetime, timezone
from analysis.utils import *

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Genome Decay Analysis\n{'='*60}")

    db      = load_genome_db()
    genomes = [g for g in db.get_all_genomes() if g['active'] and g['total_occurrences'] >= 10]
    db.close()

    if not genomes:
        print(f"  {R}Keine Genome.{NC}"); sys.exit(1)

    settings    = load_settings()
    half_life   = settings.get('genome_settings', {}).get('half_life_days', 180)
    now         = datetime.now(timezone.utc)

    # Alter berechnen (Tage seit last_seen)
    for g in genomes:
        try:
            ls = datetime.fromisoformat(g.get('last_seen', ''))
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            g['_age'] = (now - ls).days
        except Exception:
            g['_age'] = 9999

    # In Altersgruppen einteilen
    buckets = [(0, 30), (30, 60), (60, 90), (90, 180), (180, 365), (365, 9999)]
    labels  = ['0-30d', '30-60d', '60-90d', '90-180d', '180-365d', '>365d']
    bucket_stats = []
    for (lo, hi), label in zip(buckets, labels):
        grp = [g for g in genomes if lo <= g['_age'] < hi]
        if not grp:
            bucket_stats.append({'label': label, 'n': 0, 'wr': 0, 'score': 0})
            continue
        avg_wr    = sum(g['wins'] / max(g['total_occurrences'], 1) for g in grp) / len(grp)
        avg_score = sum(g['score'] for g in grp) / len(grp)
        bucket_stats.append({'label': label, 'n': len(grp), 'wr': avg_wr, 'score': avg_score})

    print(f"  half_life_days: {half_life} | {len(genomes)} Genome analysiert\n")
    print(f"  {'Alter':<12} {'Anzahl':>7} {'Ø Win-Rate':>11} {'Ø Score':>9}")
    print(f"  {'-'*44}")
    for s in bucket_stats:
        col = G if s['wr'] > 0.50 else (Y if s['wr'] > 0.44 else R)
        print(f"  {s['label']:<12} {s['n']:>7}  {col}{s['wr']:>10.1%}{NC}  {s['score']:>9.3f}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np, math

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    valid = [s for s in bucket_stats if s['n'] > 0]
    lbls  = [s['label'] for s in valid]
    wrs   = [s['wr']    for s in valid]
    scrs  = [s['score'] for s in valid]
    x     = range(len(lbls))

    ax1.bar(lbls, wrs, color='#2563eb', alpha=0.8)
    ax1.axhline(0.5, color='#ef4444', linestyle='--', linewidth=1.5, label='Baseline 50%')
    for i, w in enumerate(wrs):
        ax1.text(i, w + 0.003, f'{w:.1%}', ha='center', va='bottom', color='white', fontsize=9)
    ax1.set_xlabel('Alter des Genoms (seit last_seen)')
    ax1.set_ylabel('Ø Win-Rate')
    ax1.set_title('Win-Rate nach Genome-Alter')
    ax1.legend(facecolor='#1e293b', labelcolor='white')

    ax2.bar(lbls, scrs, color='#7c3aed', alpha=0.8)
    # Erwarteter Decay-Verlauf
    ages_mid = [15, 45, 75, 135, 272, 500]
    decay_curve = [math.exp(-a / half_life) for a in ages_mid[:len(valid)]]
    max_scr = max(scrs) if scrs else 1
    ax2.plot(list(range(len(valid))), [d * max_scr for d in decay_curve],
             color='#f59e0b', linewidth=2, marker='o', label=f'Erwarteter Decay (HL={half_life}d)')
    ax2.set_xlabel('Alter des Genoms')
    ax2.set_ylabel('Ø Score')
    ax2.set_title(f'Score nach Alter vs. erwartetem Decay (half_life={half_life}d)')
    ax2.legend(facecolor='#1e293b', labelcolor='white')

    fig.suptitle(f'dnabot Genome Decay Analysis | {len(genomes)} Genome | half_life={half_life}d',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Genome Decay Analysis\n"
               f"{len(genomes)} aktive Genome | half_life_days={half_life}\n\n"
               + "\n".join(f"{s['label']}: {s['n']} Genome | WR {s['wr']:.1%} | Score {s['score']:.3f}"
                            for s in valid)
               + "\n\nOrange Linie = erwarteter Decay-Verlauf (eingestellt)")
    save_send(fig, 'genome_decay', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
