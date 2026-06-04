#!/usr/bin/env python3
"""
analysis/sensitivity.py — Parameter Sensitivity Analysis (Option 8)

Zeigt wie robust das System gegen Parameteränderungen ist.
Jeder Parameter wird ±30% variiert. Großer Calmar-Einbruch = fragil (Overfitting).
"""
import os, sys, argparse
from analysis.utils import *

PARAMS = {
    'rr_ratio':              ('risk_settings',    'rr_ratio',              2.0),
    'min_score':             ('genome_settings',  'min_score',             0.08),
    'min_winrate':           ('genome_settings',  'min_winrate',           0.45),
    'half_life_days':        ('genome_settings',  'half_life_days',        180.0),
    'trailing_callback_pct': ('risk_settings',    'trailing_callback_rate_pct', 1.0),
}

def vary(base_val, pct):
    return [round(base_val * (1 + f), 4) for f in
            [-0.30, -0.20, -0.10, 0.0, +0.10, +0.20, +0.30]]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Parameter Sensitivity Analysis\n{'='*60}")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    settings = load_settings()
    base_result = simulate(trades, args.capital, args.risk)
    base_calmar = base_result['calmar']
    print(f"  {len(trades)} Trades | Basis Calmar: {base_calmar:.1f}")
    print()

    sensitivity = {}
    for pname, (section, key, default) in PARAMS.items():
        base_val = settings.get(section, {}).get(key, default)
        values   = vary(base_val, 0.30)
        calmars  = []
        for v in values:
            rr_use = v if pname == 'rr_ratio' else args.risk
            r = simulate(trades, args.capital, args.risk,
                         rr=v if pname == 'rr_ratio' else None)
            calmars.append(r['calmar'])
        sensitivity[pname] = {'base': base_val, 'values': values, 'calmars': calmars}
        span = max(calmars) - min(calmars)
        print(f"  {pname:<28} span={span:>8.1f}  base_calmar={calmars[3]:.1f}")

    # Tornado-Diagramm
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 7))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax)

    names, low_impacts, high_impacts = [], [], []
    for pname, d in sensitivity.items():
        base_c = d['calmars'][3]
        low  = min(d['calmars']) - base_c
        high = max(d['calmars']) - base_c
        names.append(pname.replace('_', ' '))
        low_impacts.append(low)
        high_impacts.append(high)

    order = sorted(range(len(names)),
                   key=lambda i: abs(high_impacts[i]) + abs(low_impacts[i]), reverse=True)
    names        = [names[i] for i in order]
    low_impacts  = [low_impacts[i] for i in order]
    high_impacts = [high_impacts[i] for i in order]

    y = range(len(names))
    ax.barh(y, high_impacts, left=0, color='#16a34a', alpha=0.8, label='+30% Variation')
    ax.barh(y, low_impacts,  left=0, color='#ef4444', alpha=0.8, label='-30% Variation')
    ax.axvline(0, color='white', linewidth=1.5)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, color='white')
    ax.set_xlabel('Calmar-Änderung gegenüber Basis')
    ax.set_title('Parameter Sensitivity — Tornado Diagramm\n'
                 'Breiter Balken = größere Sensitivität = fragiler Parameter',
                 color='white')
    ax.legend(facecolor='#1e293b', labelcolor='white')

    fig.suptitle(f'dnabot Parameter Sensitivity | Basis Calmar: {base_calmar:.1f}',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Parameter Sensitivity\n"
               f"Basis Calmar: {base_calmar:.1f}\n"
               f"Breiter Balken = sensitiver Parameter\n"
               f"Schmaler Balken = robuster Parameter\n\n"
               + "\n".join(f"{n}: span {abs(h)+abs(l):.0f}"
                            for n, l, h in zip(names, low_impacts, high_impacts)))
    save_send(fig, 'sensitivity', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
