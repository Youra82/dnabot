#!/usr/bin/env python3
"""
analysis/sensitivity.py — Parameter Sensitivity Analysis (Option 8)

Zeigt wie robust das System gegen Parameteränderungen ist.
Jeder Parameter wird ±30% variiert. Großer Calmar-Einbruch = fragil.

Methoden pro Parameter:
  rr_ratio              → simulate() mit variiertem RR
  min_score             → Trades mit genome_score < threshold filtern
  min_winrate           → Trades mit genome_winrate < threshold filtern
  trailing_callback_pct → effektives RR = rr × (1 - callback%)
  half_life_days        → nicht aus Backtest-Daten simulierbar (zeigt 0)
"""
import os, sys, argparse
from analysis.utils import *


def vary(base_val, steps=7):
    factors = [-0.30, -0.20, -0.10, 0.0, +0.10, +0.20, +0.30]
    return [round(base_val * (1 + f), 4) for f in factors]


def simulate_for_param(pname, v, trades, capital, risk_pct, base_rr, leverage):
    """Führt die korrekte Simulation für jeden Parameter durch."""
    if pname == 'rr_ratio':
        return simulate(trades, capital, risk_pct, rr=v, leverage=leverage)

    if pname == 'min_score':
        filtered = [t for t in trades if t.get('genome_score', 0) >= v]
        if not filtered:
            return {'calmar': 0.0, 'n': 0}
        return simulate(filtered, capital, risk_pct, rr=base_rr, leverage=leverage)

    if pname == 'min_winrate':
        filtered = [t for t in trades if t.get('genome_winrate', 1.0) >= v]
        if not filtered:
            return {'calmar': 0.0, 'n': 0}
        return simulate(filtered, capital, risk_pct, rr=base_rr, leverage=leverage)

    if pname == 'trailing_callback_pct':
        # Callback reduziert den effektiven Gewinn: Trailing Stop löst früher aus
        effective_rr = base_rr * (1.0 - v / 100.0)
        effective_rr = max(effective_rr, 0.1)
        return simulate(trades, capital, risk_pct, rr=effective_rr, leverage=leverage)

    # half_life_days: nicht aus fertigen Backtest-Daten ableitbar
    return {'calmar': 0.0, 'n': 0, '_not_simulable': True}


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
    leverage  = int(settings.get('risk_settings', {}).get('leverage', 1))
    base_rr   = settings.get('risk_settings', {}).get('rr_ratio', 2.0)

    base_result = simulate(trades, args.capital, args.risk, rr=base_rr, leverage=leverage)
    base_calmar = base_result['calmar']
    print(f"  {len(trades)} Trades | Basis Calmar: {base_calmar:.1f} | RR: {base_rr} | Leverage: {leverage}x")
    print()

    PARAMS = {
        'rr_ratio':              ('risk_settings',   'rr_ratio',               2.0),
        'min_score':             ('genome_settings', 'min_score',              0.08),
        'min_winrate':           ('genome_settings', 'min_winrate',            0.45),
        'trailing_callback_pct': ('risk_settings',   'trailing_callback_rate_pct', 1.0),
        'half_life_days':        ('genome_settings', 'half_life_days',         180.0),
    }

    sensitivity = {}
    not_simulable = set()

    for pname, (section, key, default) in PARAMS.items():
        base_val = settings.get(section, {}).get(key, default)
        values   = vary(base_val)
        calmars  = []
        for v in values:
            r = simulate_for_param(pname, v, trades, args.capital, args.risk, base_rr, leverage)
            calmars.append(r['calmar'])
            if r.get('_not_simulable'):
                not_simulable.add(pname)

        sensitivity[pname] = {'base': base_val, 'values': values, 'calmars': calmars}
        span = max(calmars) - min(calmars)
        note = '  ← nicht simulierbar (0)' if pname in not_simulable else ''
        print(f"  {pname:<28} span={span:>8.1f}  base_calmar={calmars[3]:.1f}{note}")

    if not_simulable:
        print(f"\n  {Y}Hinweis: {', '.join(not_simulable)} können nicht aus Backtest-Daten")
        print(f"  variiert werden — sie steuern welche Genome gehandelt werden,")
        print(f"  nicht wie ein Trade verläuft. Für diese Parameter: run_pipeline.sh neu.{NC}")

    # ── Tornado-Diagramm ──────────────────────────────────────────────────────
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    # Nicht-simulierbare Parameter aus Diagramm ausblenden
    plot_params = {k: v for k, v in sensitivity.items() if k not in not_simulable}

    fig, ax = plt.subplots(figsize=(12, max(5, len(plot_params) * 1.2 + 2)))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax)

    names, low_impacts, high_impacts, base_vals_label = [], [], [], []
    for pname, d in plot_params.items():
        base_c = d['calmars'][3]
        low    = min(d['calmars']) - base_c
        high   = max(d['calmars']) - base_c
        names.append(pname.replace('_', ' '))
        low_impacts.append(low)
        high_impacts.append(high)
        base_vals_label.append(f"Basis={d['base']}")

    order        = sorted(range(len(names)),
                          key=lambda i: abs(high_impacts[i]) + abs(low_impacts[i]), reverse=True)
    names        = [names[i]           for i in order]
    low_impacts  = [low_impacts[i]     for i in order]
    high_impacts = [high_impacts[i]    for i in order]
    base_labels  = [base_vals_label[i] for i in order]

    y = list(range(len(names)))
    ax.barh(y, high_impacts, left=0, color='#16a34a', alpha=0.8, label='+30% Variation')
    ax.barh(y, low_impacts,  left=0, color='#ef4444', alpha=0.8, label='-30% Variation')
    ax.axvline(0, color='white', linewidth=1.5)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{n}  ({b})" for n, b in zip(names, base_labels)], color='white')
    ax.set_xlabel('Calmar-Änderung gegenüber Basis')
    ax.set_title('Parameter Sensitivity — Tornado Diagramm\n'
                 'Breiter Balken = größere Sensitivität = fragiler Parameter',
                 color='white')
    ax.legend(facecolor='#1e293b', labelcolor='white')

    # half_life Hinweis im Chart
    if not_simulable:
        ax.text(0.99, 0.02,
                f"Nicht dargestellt: {', '.join(not_simulable)}\n(benötigt Backtester-Neustart)",
                transform=ax.transAxes, ha='right', va='bottom',
                color='#94a3b8', fontsize=8)

    fig.suptitle(f'dnabot Parameter Sensitivity | Basis Calmar: {base_calmar:.1f}',
                 color='white', fontsize=11)
    plt.tight_layout()

    lines = [f"dnabot Parameter Sensitivity",
             f"Basis Calmar: {base_calmar:.1f} | {len(trades)} Trades\n"]
    for n, l, h in zip(names, low_impacts, high_impacts):
        span = abs(h) + abs(l)
        robust = "robust" if span < base_calmar * 0.1 else ("sensitiv" if span < base_calmar * 0.5 else "FRAGIL")
        lines.append(f"{n}: span {span:.0f} → {robust}")
    if not_simulable:
        lines.append(f"\n(half_life_days nicht simulierbar)")

    save_send(fig, 'sensitivity', "\n".join(lines), args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
