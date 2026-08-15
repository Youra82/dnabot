#!/usr/bin/env python3
"""
analysis/drawdown_duration.py — Drawdown Duration Analysis (Option 19)

Analysiert wie lange Drawdown-Phasen typischerweise dauern und wie lange
das System zur Erholung braucht.

Fragen:
  - Wie tief ist der typische Drawdown?
  - Wie lange dauert die Erholungsphase nach einem Drawdown?
  - Gibt es einen Zusammenhang zwischen Tiefe und Dauer?
"""
import os, sys, argparse
from datetime import timezone
from analysis.utils import *


def find_drawdown_periods(trades, capital, risk_pct):
    """
    Berechnet alle Drawdown-Perioden aus der chronologischen Trade-Sequenz.
    Gibt Liste von dicts zurück: depth_pct, duration_days, recovery_days, n_trades
    """
    if not trades:
        return [], []

    sorted_trades = sorted(trades, key=lambda t: t.get('entry_dt', ''))
    equity = capital
    peak   = equity
    peak_dt = sorted_trades[0].get('entry_dt')
    in_dd   = False
    dd_start_dt = None
    dd_nadir    = 0.0
    dd_nadir_eq = equity
    periods     = []
    equity_curve = [(sorted_trades[0].get('entry_dt'), equity)]

    for t in sorted_trades:
        sl_pct      = max(t.get('sl_pct', 1.0), 0.01)
        risk_amount = min(equity * (risk_pct / 100.0), MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome     = t.get('outcome', 'LOSS')
        # WIN nutzt wie TIMEOUT die tatsaechlich simulierte Bewegung statt
        # einer pauschalen RR-Konstante (siehe analysis/utils.py::simulate()).
        if outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        equity += pnl
        dt = t.get('entry_dt')
        equity_curve.append((dt, equity))

        if equity > peak:
            if in_dd:
                # Erholung abgeschlossen
                rec_days = (dt - dd_start_dt).days if dd_start_dt and dt else 0
                periods.append({
                    'depth_pct':    dd_nadir,
                    'duration_days': rec_days,
                    'start_dt':     dd_start_dt,
                    'recovery_dt':  dt,
                })
                in_dd = False
                dd_nadir = 0.0
            peak    = equity
            peak_dt = dt
        else:
            dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
            if dd > 1.0:
                if not in_dd:
                    in_dd       = True
                    dd_start_dt = peak_dt
                    dd_nadir    = dd
                else:
                    if dd > dd_nadir:
                        dd_nadir = dd

    return periods, equity_curve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Drawdown Duration Analysis\n{'='*60}")
    print(f"  Kapital: {args.capital} USDT | Risk/Trade: {args.risk}%\n")

    trades = all_trades_flat()
    if not trades:
        print(f"  {R}Keine Backtest-Daten. Erst show_results.sh → Mode 1.{NC}")
        sys.exit(1)

    periods, eq_curve = find_drawdown_periods(trades, args.capital, args.risk)

    if not periods:
        print(f"  {G}Keine signifikanten Drawdown-Perioden gefunden (>1%).{NC}\n")
        sys.exit(0)

    depths    = [p['depth_pct']    for p in periods]
    durations = [p['duration_days'] for p in periods]

    avg_depth    = sum(depths) / len(depths)
    max_depth    = max(depths)
    avg_duration = sum(durations) / len(durations)
    max_duration = max(durations)
    sorted_dur   = sorted(durations)
    p90_duration = sorted_dur[int(0.90 * len(sorted_dur))]

    print(f"  Drawdown-Perioden gefunden: {len(periods)}")
    print(f"\n  {'Metrik':<32} {'Wert':>10}")
    print(f"  {'-'*44}")
    print(f"  {'Ø Drawdown-Tiefe:':<32} {avg_depth:>9.1f}%")
    print(f"  {'Maximaler Drawdown:':<32} {max_depth:>9.1f}%")
    print(f"  {'Ø Erholungsdauer:':<32} {avg_duration:>9.0f}d")
    print(f"  {'90. Perz. Erholungsdauer:':<32} {p90_duration:>9.0f}d")
    print(f"  {'Längste Erholung:':<32} {max_duration:>9.0f}d")

    col = G if avg_duration < 30 else (Y if avg_duration < 90 else R)
    print(f"\n  Interpretation: {col}{'Schnelle' if avg_duration < 30 else ('Moderate' if avg_duration < 90 else 'Langsame')} Erholung im Schnitt{NC}")
    print(f"  → Du musst typischerweise {col}{avg_duration:.0f} Tage{NC} einen Verlust aussitzen.")

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    fig.patch.set_facecolor('#0f172a')
    style_axes(*axes)
    ax1, ax2, ax3 = axes

    # Panel 1: Scatter — Tiefe vs. Dauer
    colors = ['#ef4444' if d > 20 else '#f59e0b' if d > 10 else '#16a34a' for d in depths]
    ax1.scatter(depths, durations, c=colors, alpha=0.7, s=60, edgecolors='none')
    if len(depths) >= 2:
        z = np.polyfit(depths, durations, 1)
        p = np.poly1d(z)
        x_line = np.linspace(min(depths), max(depths), 50)
        ax1.plot(x_line, p(x_line), '--', color='#94a3b8', linewidth=1.5, alpha=0.7, label='Trend')
    ax1.set_xlabel('Drawdown-Tiefe (%)')
    ax1.set_ylabel('Erholungsdauer (Tage)')
    ax1.set_title('Tiefe vs. Erholungsdauer\n(rot>20%, orange>10%, grün≤10%)')
    ax1.legend(facecolor='#1e293b', labelcolor='white')

    # Panel 2: Histogramm Erholungsdauer
    ax2.hist(durations, bins=min(20, max(5, len(durations)//2)),
             color='#2563eb', alpha=0.8, edgecolor='none')
    ax2.axvline(avg_duration, color='#fbbf24', linewidth=2, linestyle='-',
                label=f'Ø {avg_duration:.0f}d')
    ax2.axvline(p90_duration, color='#ef4444', linewidth=2, linestyle='--',
                label=f'90. Perz. {p90_duration:.0f}d')
    ax2.set_xlabel('Erholungsdauer (Tage)')
    ax2.set_ylabel('Häufigkeit')
    ax2.set_title('Verteilung der Erholungsdauern')
    ax2.legend(facecolor='#1e293b', labelcolor='white')

    # Panel 3: Equity-Kurve mit Drawdown-Markierungen
    if eq_curve:
        dts    = [e[0] for e in eq_curve if e[0] is not None]
        equities = [e[1] for e in eq_curve if e[0] is not None]
        if dts:
            ax3.plot(dts, equities, color='#2563eb', linewidth=1.2, label='Equity')
            peak_eq = equities[0]
            peak_line = []
            for e in equities:
                if e > peak_eq:
                    peak_eq = e
                peak_line.append(peak_eq)
            ax3.plot(dts, peak_line, color='#16a34a', linewidth=0.8,
                     linestyle='--', alpha=0.6, label='Peak')
            ax3.fill_between(dts, equities, peak_line,
                             where=[e < p for e, p in zip(equities, peak_line)],
                             color='#ef4444', alpha=0.25, label='Drawdown')
            ax3.set_xlabel('Datum')
            ax3.set_ylabel('Equity (USDT)')
            ax3.set_title('Equity-Kurve mit Drawdown-Zonen')
            ax3.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)
            try:
                fig.autofmt_xdate(rotation=30)
            except Exception:
                pass

    fig.suptitle(
        f'dnabot Drawdown Duration | {len(periods)} Perioden | '
        f'Ø Tiefe {avg_depth:.1f}% | Ø Erholung {avg_duration:.0f}d',
        color='white', fontsize=11
    )
    plt.tight_layout()

    caption = (
        f"dnabot Drawdown Duration Analysis\n"
        f"{len(periods)} Drawdown-Perioden analysiert\n\n"
        f"Ø Tiefe:           {avg_depth:.1f}%\n"
        f"Max. Tiefe:        {max_depth:.1f}%\n"
        f"Ø Erholung:        {avg_duration:.0f} Tage\n"
        f"90. Perz. Erholung: {p90_duration:.0f} Tage\n"
        f"Längste Erholung:  {max_duration:.0f} Tage\n\n"
        f"→ Typische Aussitz-Dauer: {avg_duration:.0f}d"
    )
    save_send(fig, 'drawdown_duration', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
