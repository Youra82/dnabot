#!/usr/bin/env python3
"""
analysis/seq_length_analysis.py — Sequenzlängen-Analyse (Option 14)

Vergleicht ob 4er, 5er oder 6er Genome-Sequenzen bessere Out-of-Sample Performance liefern.
Walk-Forward pro Sequenzlänge.
"""
import os, sys, argparse
from datetime import timedelta, timezone
from analysis.utils import *

SEQ_LENGTHS = [4, 5, 6, 'alle']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--lookback',    type=int,   default=2)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*60}\n  dnabot — Sequenzlängen-Analyse\n{'='*60}")

    pair_results = load_trades()
    if not pair_results:
        print(f"  {R}Keine Backtest-Daten.{NC}"); sys.exit(1)

    all_dts     = [t['entry_dt'] for r in pair_results for t in r['trades']]
    min_date    = min(all_dts)
    max_date    = max(all_dts)

    def next_mon(dt):
        d = (7 - dt.weekday()) % 7
        return (dt + timedelta(days=d)).replace(hour=0, minute=0, second=0,
                                                microsecond=0, tzinfo=timezone.utc)

    oos_start = next_mon(min_date + timedelta(weeks=args.lookback))
    week_starts = []
    w = oos_start
    while w < next_mon(max_date):
        week_starts.append(w); w += timedelta(weeks=1)

    def wf_seqlen(seq_filter):
        equity = args.capital
        curve  = []
        for ws in week_starts:
            is_s  = ws - timedelta(weeks=args.lookback)
            oos_e = ws + timedelta(weeks=1)
            # Portfolio aus In-Sample wählen
            portfolio = []
            for r in pair_results:
                is_trades = [t for t in r['trades'] if is_s <= t['entry_dt'] < ws
                             and (seq_filter == 'alle' or t.get('seq_len') == seq_filter)]
                if len(is_trades) < 2:
                    continue
                res = simulate(is_trades, 100.0, args.risk)
                if res['pnl_pct'] > 0:
                    portfolio.append(r)
            oos = []
            for p in portfolio:
                oos += [t for t in p['trades'] if ws <= t['entry_dt'] < oos_e
                        and (seq_filter == 'alle' or t.get('seq_len') == seq_filter)]
            if oos:
                equity, _, _ = (lambda r: (r['equity'], r['max_dd'], r['wr']))(simulate(oos, equity, args.risk))
            curve.append((oos_e, equity))
        return curve

    results = {}
    for sl in SEQ_LENGTHS:
        print(f"  Sequenzlänge {sl} ...", end='', flush=True)
        curve = wf_seqlen(sl)
        from analysis.utils import simulate as _sim
        pnl = (curve[-1][1] - args.capital) / args.capital * 100 if curve else 0
        eq_vals = [args.capital] + [e[1] for e in curve]
        peak = eq_vals[0]; max_dd = 0.0
        for e in eq_vals:
            if e > peak: peak = e
            if peak > 0:
                dd = (peak - e) / peak * 100
                if dd > max_dd: max_dd = dd
        calmar = pnl / max_dd if max_dd > 0 else pnl
        results[sl] = {'curve': curve, 'pnl': pnl, 'max_dd': max_dd, 'calmar': calmar}
        col = G if pnl > 0 else R
        print(f"  {col}PnL={pnl:+.1f}% | DD={max_dd:.1f}% | Calmar={calmar:.1f}{NC}")

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    for i, (sl, d) in enumerate(results.items()):
        dates    = [week_starts[0]] + [e[0] for e in d['curve']]
        equities = [args.capital]   + [e[1] for e in d['curve']]
        label    = f"Seq {sl}: {d['pnl']:+.0f}% | DD {d['max_dd']:.1f}% | Calmar {d['calmar']:.1f}"
        ax1.plot(dates, equities, color=COLORS[i % len(COLORS)], linewidth=2, label=label)

    ax1.axhline(args.capital, color='#475569', linestyle='--', alpha=0.5)
    ax1.set_title('Equity-Kurven nach Sequenzlänge (Walk-Forward OOS)', color='white')
    ax1.legend(fontsize=9, facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    try: ax1.set_yscale('log')
    except: pass

    sls     = list(results.keys())
    calmars = [results[sl]['calmar'] for sl in sls]
    bars    = ax2.bar([str(sl) for sl in sls], calmars,
                      color=[COLORS[i % len(COLORS)] for i in range(len(sls))], alpha=0.8)
    for bar, c in zip(bars, calmars):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                 f'{c:.1f}', ha='center', va='bottom', color='white', fontsize=11, fontweight='bold')
    best = sls[calmars.index(max(calmars))]
    bars[sls.index(best)].set_edgecolor('#fbbf24'); bars[sls.index(best)].set_linewidth(3)
    ax2.set_xlabel('Sequenzlänge')
    ax2.set_ylabel('Calmar Score (OOS)')
    ax2.set_title(f'Calmar pro Sequenzlänge | ★ Beste: {best}')

    fig.suptitle(f'dnabot Sequenzlängen-Analyse | {len(week_starts)} Test-Wochen OOS',
                 color='white', fontsize=11)
    plt.tight_layout()

    caption = (f"dnabot Sequenzlängen-Analyse\n"
               f"{len(week_starts)} Test-Wochen (Walk-Forward OOS)\n\n"
               + "\n".join(f"Seq {sl}: Calmar {results[sl]['calmar']:.1f} | "
                            f"PnL {results[sl]['pnl']:+.0f}% | DD {results[sl]['max_dd']:.1f}%"
                            for sl in sls)
               + f"\n\n★ Beste Sequenzlänge: {best}")
    save_send(fig, 'seq_length', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")

if __name__ == '__main__':
    main()
