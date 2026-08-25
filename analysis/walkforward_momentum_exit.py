#!/usr/bin/env python3
"""
analysis/walkforward_momentum_exit.py — Walk-Forward Analyse: Optimaler
Lookback-Zeitraum (momentum_exit)

Untersucht welcher Lookback-Zeitraum (Wochen zurueck) fuer den woechentlichen
Auto-Optimizer (risk_genome_discover.py / run_portfolio_optimizer_momentum_
exit.py) am besten Out-of-Sample performt.

Methode: Rolling Walk-Forward (kein Lookahead)
  Fuer jeden Lookback (1, 2, 4, 8, 12, 26 Wochen):
    Pro Test-Woche:
      In-Sample     -> letzte N Wochen: Portfolio auswaehlen (Greedy Calmar,
                        identischer Algorithmus wie run_portfolio_optimizer_
                        momentum_exit.py::optimize_portfolio())
      Out-of-Sample -> naechste Woche: Portfolio anwenden, Equity akkumulieren

  Alle Lookbacks laufen auf demselben OOS-Zeitraum (fairer Vergleich).

Wiederbelebung von walk_forward_test.py (Genome-System, beim Cleanup
2026-08-24 entfernt) -- Algorithmus identisch, Datenquelle auf momentum_exit
umgestellt. NEU: schreibt den besten Lookback automatisch in
optimization_settings.backtest_lookback_weeks (das alte Skript hat das nur
empfohlen, nicht geschrieben).

Ausfuehrung:
    python3 analysis/walkforward_momentum_exit.py
    python3 analysis/walkforward_momentum_exit.py --risk 1.0 --no-telegram
    python3 analysis/walkforward_momentum_exit.py --no-write   # nur anzeigen, nicht eintragen
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from momentum_exit_utils import (
    load_trades, get_telegram, send_photo, load_settings,
    PROJECT_ROOT, SETTINGS_PATH, TMP_DIR, DOCS_DIR,
    MAX_NOTIONAL_USDT, FEE_PCT_PER_SIDE, G, Y, R, C, B, NC,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from run_portfolio_optimizer_momentum_exit import scaled_min_trades

LOOKBACK_WINDOWS = [1, 2, 4, 8, 12, 26]   # Wochen


def _parse_dt(ts_str):
    try:
        dt = datetime.fromisoformat(str(ts_str))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def next_monday(dt):
    days = (7 - dt.weekday()) % 7
    return (dt + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0,
                                               tzinfo=timezone.utc)


def load_all_trades_wf():
    """Laedt ALLE momentum_exit-Backtest-Trades -- kein active_strategies-Filter,
    der Walk-Forward bewertet den Auto-Optimizer, der alle discovered Pairs sieht."""
    pair_results = load_trades()
    for r in pair_results:
        for t in r['trades']:
            t['entry_dt'] = t.get('entry_dt') or _parse_dt(t.get('entry_time', ''))
    return [r for r in pair_results if r['trades']]


def simulate_trades(trades, equity, risk_pct, leverage=1, fee_pct=FEE_PCT_PER_SIDE):
    peak   = equity
    max_dd = 0.0
    wins   = 0
    for t in sorted(trades, key=lambda x: x['entry_dt']):
        sl_pct       = max(t.get('sl_pct', 1.0), 0.01)
        leverage_cap = equity * max(leverage, 1) * (sl_pct / 100.0)
        risk_amount  = min(equity * (risk_pct / 100.0), leverage_cap, MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome      = t.get('outcome', 'LOSS')
        if outcome == 'WIN':
            wins += 1
        if outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        if fee_pct:
            position_size = risk_amount / (sl_pct / 100.0)
            pnl -= position_size * (fee_pct / 100.0) * 2.0
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return equity, max_dd, wins


def _combined_pnl_dd(pairs, risk_pct, leverage=1, fee_pct=FEE_PCT_PER_SIDE):
    all_trades = [t for p in pairs for t in p['_is_trades']]
    if not all_trades:
        return 0.0, 0.0
    final_eq, max_dd, _ = simulate_trades(all_trades, 100.0, risk_pct, leverage=leverage, fee_pct=fee_pct)
    return (final_eq - 100.0) / 100.0 * 100.0, max_dd


def select_portfolio(all_results, is_start, is_end, min_trades, risk_pct, leverage=1,
                      max_dd_limit=30.0, require_persistence=False, fee_pct=FEE_PCT_PER_SIDE):
    """Waehlt das Portfolio fuer das In-Sample-Fenster [is_start, is_end) --
    derselbe Greedy-Algorithmus wie run_portfolio_optimizer_momentum_exit.py::
    optimize_portfolio(). Constraint: max 1 Timeframe pro Coin."""
    lookback = is_end - is_start
    prev_start, prev_end = is_start - lookback, is_start

    candidates = []
    for r in all_results:
        is_trades = [t for t in r['trades'] if is_start <= t['entry_dt'] < is_end]
        if len(is_trades) < min_trades:
            continue
        final_eq, max_dd, wins = simulate_trades(is_trades, 100.0, risk_pct, leverage=leverage, fee_pct=fee_pct)
        pnl_pct = (final_eq - 100.0) / 100.0 * 100.0
        if pnl_pct <= 0:
            continue

        if require_persistence:
            prev_trades = [t for t in r['trades'] if prev_start <= t['entry_dt'] < prev_end]
            if len(prev_trades) < min_trades:
                continue
            prev_eq, _, _ = simulate_trades(prev_trades, 100.0, risk_pct, leverage=leverage, fee_pct=fee_pct)
            if (prev_eq - 100.0) <= 0:
                continue

        calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
        candidates.append({**r, '_is_trades': is_trades, '_calmar': calmar,
                            '_pnl': pnl_pct, '_n': len(is_trades)})

    coin_best = {}
    for c in candidates:
        if c['coin'] not in coin_best or c['_calmar'] > coin_best[c['coin']]['_calmar']:
            coin_best[c['coin']] = c

    eligible = list(coin_best.values())
    if not eligible:
        return []

    eligible.sort(key=lambda c: c['_calmar'], reverse=True)

    best_team = [eligible[0]]
    best_pnl, best_dd = _combined_pnl_dd(best_team, risk_pct, leverage=leverage, fee_pct=fee_pct)
    best_score = best_pnl / best_dd if best_dd > 0 else best_pnl
    candidate_pool = eligible[1:]

    while candidate_pool:
        best_addition   = None
        best_score_with = best_score
        for cand in candidate_pool:
            pnl, dd = _combined_pnl_dd(best_team + [cand], risk_pct, leverage=leverage, fee_pct=fee_pct)
            if dd > max_dd_limit:
                continue
            score = pnl / dd if dd > 0 else pnl
            if score > best_score_with:
                best_score_with = score
                best_addition   = cand
        if best_addition:
            best_team.append(best_addition)
            best_score = best_score_with
            candidate_pool.remove(best_addition)
        else:
            break

    return best_team


def run_walk_forward(all_results, lookback_weeks, risk_pct, week_starts, capital,
                      leverage=1, max_dd_limit=30.0, require_persistence=False,
                      fee_pct=FEE_PCT_PER_SIDE):
    min_trades  = scaled_min_trades(lookback_weeks)
    equity      = capital
    curve       = []
    total_n     = 0
    total_wins  = 0
    empty_weeks = 0
    trade_log   = []

    for week_start in week_starts:
        is_start = week_start - timedelta(weeks=lookback_weeks)
        oos_end  = week_start + timedelta(weeks=1)

        portfolio = select_portfolio(all_results, is_start, week_start, min_trades, risk_pct,
                                      leverage=leverage, max_dd_limit=max_dd_limit,
                                      require_persistence=require_persistence, fee_pct=fee_pct)

        oos_trades = []
        for p in portfolio:
            oos_trades.extend(
                (p['market'], p['timeframe'], t) for t in p['trades']
                if week_start <= t['entry_dt'] < oos_end
            )

        if not oos_trades:
            empty_weeks += 1
            curve.append((oos_end, equity, len(portfolio), 0))
            continue

        raw_trades = [t for _, _, t in oos_trades]
        equity, _, wins = simulate_trades(raw_trades, equity, risk_pct, leverage=leverage, fee_pct=fee_pct)
        total_n    += len(oos_trades)
        total_wins += wins
        curve.append((oos_end, equity, len(portfolio), len(oos_trades)))
        for market, timeframe, t in oos_trades:
            trade_log.append({
                'week': week_start, 'market': market, 'timeframe': timeframe,
                'entry_dt': t['entry_dt'], 'outcome': t.get('outcome', 'LOSS'),
                'pnl_pct': t.get('pnl_pct', 0.0), 'sl_pct': t.get('sl_pct', 1.0),
            })

    return curve, total_n, total_wins, empty_weeks, trade_log


def compute_stats(curve, capital):
    if not curve:
        return 0.0, 0.0, 0.0
    final_eq = curve[-1][1]
    pnl_pct  = (final_eq - capital) / capital * 100.0
    eq_vals  = [capital] + [e[1] for e in curve]
    peak     = eq_vals[0]
    max_dd   = 0.0
    for e in eq_vals:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
    return calmar, pnl_pct, max_dd


def create_chart(results, week_starts, risk_pct, capital):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print(f"  {R}matplotlib nicht installiert.{NC}")
        return None

    COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed', '#0891b2']
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 11))
    fig.patch.set_facecolor('#0f172a')
    for ax in (ax1, ax2):
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')

    ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5, linewidth=0.8)
    ax1.text(week_starts[0], capital * 1.02, f'Start {capital:.0f} USDT', color='#475569', fontsize=8)

    summary_data = []
    bar_labels, bar_calmar, bar_colors = [], [], []

    for i, (weeks, (curve, n_total, n_wins, empty_w)) in enumerate(sorted(results.items())):
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        wr = n_wins / n_total * 100 if n_total > 0 else 0.0

        dates    = [week_starts[0]] + [e[0] for e in curve]
        equities = [capital]        + [e[1] for e in curve]

        color = COLORS[i % len(COLORS)]
        label = f"{weeks:2}W Lookback: {pnl_pct:+.0f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f}"
        ax1.plot(dates, equities, color=color, linewidth=2, label=label, zorder=3)

        bar_labels.append(f'{weeks}W')
        bar_calmar.append(calmar)
        bar_colors.append(color)

        summary_data.append({'weeks': weeks, 'calmar': calmar, 'pnl_pct': pnl_pct,
                              'max_dd': max_dd, 'n_total': n_total, 'wr': wr, 'empty_w': empty_w})

    ax1.set_title(
        f'dnabot momentum_exit Walk-Forward — Lookback-Vergleich (Out-of-Sample)\n'
        f'Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT | Test-Wochen: {len(week_starts)}',
        color='white', fontsize=12, pad=10
    )
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=9, loc='upper left', framealpha=0.3, facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    import matplotlib.pyplot as plt
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')

    try:
        all_eq = [capital] + [e[1] for c, _ in results.items() for e in _[0]]
        min_eq = min(e for e in all_eq if e > 0)
        ax1.set_yscale('log')
        ax1.set_ylim(bottom=max(1, min_eq * 0.5))
    except Exception:
        pass

    ax2.set_title('Calmar Score pro Lookback (Out-of-Sample, höher = besser)', color='white', fontsize=11, pad=8)
    bars = ax2.bar(bar_labels, bar_calmar, color=bar_colors, alpha=0.8, edgecolor='#1e293b', linewidth=1.2)
    for bar, score in zip(bars, bar_calmar):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + abs(max(bar_calmar, default=1)) * 0.01,
                 f'{score:.1f}', ha='center', va='bottom', color='white', fontsize=11, fontweight='bold')
    if bar_calmar:
        best_idx = bar_calmar.index(max(bar_calmar))
        bars[best_idx].set_edgecolor('#fbbf24')
        bars[best_idx].set_linewidth(3)
        ax2.text(bars[best_idx].get_x() + bars[best_idx].get_width() / 2,
                 -abs(max(bar_calmar, default=1)) * 0.08, '★ BEST',
                 ha='center', va='top', color='#fbbf24', fontsize=9, fontweight='bold')
    ax2.set_xlabel('Lookback-Zeitraum', color='#94a3b8')
    ax2.set_ylabel('Calmar Score (OOS)', color='#94a3b8')
    ax2.axhline(y=0, color='#475569', linewidth=0.8)

    plt.tight_layout(pad=2.5)
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, 'dnabot_momentum_exit_walkforward.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    os.makedirs(DOCS_DIR, exist_ok=True)
    docs_path = os.path.join(DOCS_DIR, 'momentum_exit_walkforward_latest.png')
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()

    return path, summary_data


def write_lookback_to_settings(best_weeks: int) -> bool:
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            settings = json.load(f)
        settings.setdefault('optimization_settings', {})['backtest_lookback_weeks'] = best_weeks
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"  {R}Fehler beim Schreiben von settings.json: {e}{NC}")
        return False


def main():
    parser = argparse.ArgumentParser(description='dnabot Walk-Forward Lookback Analyse (momentum_exit)')
    parser.add_argument('--risk',        type=float, default=None,
                        help='Risiko pro Trade in %% (Standard: 1.0)')
    parser.add_argument('--capital',     type=float, default=1000.0)
    parser.add_argument('--max-dd',      type=float, default=30.0)
    parser.add_argument('--oos-weeks',   type=int, default=None)
    parser.add_argument('--persistence', action='store_true')
    parser.add_argument('--fee-pct',     type=float, default=FEE_PCT_PER_SIDE)
    parser.add_argument('--no-write',    action='store_true',
                        help='Nur anzeigen, NICHT automatisch in settings.json eintragen.')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    settings = load_settings()
    risk_pct = args.risk or 1.0
    leverage = int(settings.get('risk_settings', {}).get('leverage', 1))
    capital  = args.capital

    print(f"\n{'=' * 62}")
    print(f"  {B}dnabot momentum_exit — Walk-Forward Lookback Analyse{NC}")
    print(f"{'=' * 62}")
    print(f"  Risk/Trade:   {risk_pct}%")
    print(f"  Startkapital: {capital} USDT")
    print(f"  Leverage:     {leverage}x (aus settings.json)")
    print(f"  Min. Trades:  {scaled_min_trades(LOOKBACK_WINDOWS[0])}-{scaled_min_trades(LOOKBACK_WINDOWS[-1])} "
          f"(skaliert mit Lookback)")
    print(f"  Max Drawdown: {args.max_dd}% (Team-Auswahl)")
    print(f"  Persistenz:   {'ja' if args.persistence else 'nein (Standard)'}")
    print(f"  Gebühr/Seite: {args.fee_pct}%")
    print(f"  Lookbacks:    {LOOKBACK_WINDOWS} Wochen")
    print()

    print("  Lade momentum_exit-Backtest-Daten...", end='', flush=True)
    all_results = load_all_trades_wf()
    if not all_results:
        print(f"\n  {R}Keine momentum_exit-Backtest-Daten. Erst ./run_momentum_exit_pipeline.sh ausführen.{NC}\n")
        sys.exit(1)

    all_dts  = [t['entry_dt'] for r in all_results for t in r['trades']]
    min_date = min(all_dts)
    max_date = max(all_dts)
    n_weeks  = (max_date - min_date).days // 7
    print(f" {len(all_results)} Pairs | {min_date.strftime('%Y-%m-%d')} → "
          f"{max_date.strftime('%Y-%m-%d')} ({n_weeks} Wochen)")

    active_lookbacks = [w for w in LOOKBACK_WINDOWS if w + 1 <= n_weeks]
    skipped = [w for w in LOOKBACK_WINDOWS if w not in active_lookbacks]
    if skipped:
        print(f"  {Y}Übersprungen (zu wenig Daten): {skipped}W{NC}")
    if not active_lookbacks:
        print(f"  {R}Nicht genug Daten fuer irgendeinen Lookback.{NC}\n")
        sys.exit(1)

    max_lookback = max(active_lookbacks)
    oos_start    = next_monday(min_date + timedelta(weeks=max_lookback))
    oos_end      = next_monday(max_date)

    if args.oos_weeks:
        clipped_start = oos_end - timedelta(weeks=args.oos_weeks)
        if clipped_start > oos_start:
            oos_start = next_monday(clipped_start)

    week_starts = []
    w = oos_start
    while w < oos_end:
        week_starts.append(w)
        w += timedelta(weeks=1)

    if len(week_starts) < 2:
        print(f"  {R}Zu wenig OOS-Wochen ({len(week_starts)}).{NC}\n")
        sys.exit(1)

    print(f"  OOS-Zeitraum: {oos_start.strftime('%Y-%m-%d')} → "
          f"{oos_end.strftime('%Y-%m-%d')} ({len(week_starts)} Test-Wochen)"
          + (f" [--oos-weeks {args.oos_weeks}]" if args.oos_weeks else ""))
    print()

    results    = {}
    trade_logs = {}
    for weeks in active_lookbacks:
        print(f"  {C}Lookback {weeks:2d}W (min. {scaled_min_trades(weeks)} Trades) ...{NC}", end='', flush=True)
        curve, n_total, n_wins, empty_w, trade_log = run_walk_forward(
            all_results, weeks, risk_pct, week_starts, capital,
            leverage=leverage, max_dd_limit=args.max_dd, require_persistence=args.persistence,
            fee_pct=args.fee_pct
        )
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        wr = n_wins / n_total * 100 if n_total > 0 else 0.0
        results[weeks] = (curve, n_total, n_wins, empty_w)
        trade_logs[weeks] = trade_log

        col = G if pnl_pct > 0 else R
        print(f"  {col}PnL={pnl_pct:+.1f}% | DD={max_dd:.1f}% | Calmar={calmar:.1f} | "
              f"Trades={n_total} | WR={wr:.1f}% | Leerwochen={empty_w}{NC}")

    best_weeks = max(results, key=lambda w: compute_stats(results[w][0], capital)[0])
    bc, bp, bd = compute_stats(results[best_weeks][0], capital)

    print()
    print(f"  {'─' * 50}")
    print(f"  {G}★ Bester Lookback: {best_weeks} Wochen{NC}")
    print(f"  Calmar: {bc:.1f} | PnL: {bp:+.1f}% | MaxDD: {bd:.1f}%")
    print(f"  {'─' * 50}")

    best_log = sorted(trade_logs[best_weeks], key=lambda x: x['entry_dt'])
    coins_involved = sorted(set(t['market'] for t in best_log))
    if len(coins_involved) <= 2:
        print(f"    {R}⚠ Nur {len(coins_involved)} Coin(s) beteiligt: {coins_involved} "
              f"-- Ergebnis haengt an sehr wenigen Symbolen.{NC}")

    write_ok = False
    if not args.no_write:
        write_ok = write_lookback_to_settings(best_weeks)
        if write_ok:
            print()
            print(f"  {G}✓ settings.json aktualisiert -- optimization_settings.backtest_lookback_weeks = {best_weeks}{NC}")
    else:
        print()
        print(f"  {Y}--no-write: settings.json wurde NICHT geändert.{NC}")
        print(f"  Empfehlung: optimization_settings.backtest_lookback_weeks = {best_weeks}")

    print()
    chart_result = create_chart(results, week_starts, risk_pct, capital)
    if chart_result is None:
        sys.exit(0)

    chart_path, summary_data = chart_result
    print(f"  {G}✓ Chart gespeichert: {chart_path}{NC}")

    print()
    print(f"{'=' * 62}")
    print(f"  {'Lookback':<10} {'PnL%':>8} {'MaxDD%':>8} {'Calmar':>8} {'Trades':>7} {'WR':>7} {'LeerW':>6}")
    print(f"  {'─' * 56}")
    for d in sorted(summary_data, key=lambda x: x['calmar'], reverse=True):
        marker = '★' if d['weeks'] == best_weeks else ' '
        col = G if d['pnl_pct'] > 0 else R
        print(f"  {marker}{d['weeks']:2d}W       {col}{d['pnl_pct']:>+8.1f}%{NC} "
              f"{d['max_dd']:>7.1f}% {d['calmar']:>8.1f} {d['n_total']:>7} {d['wr']:>6.1f}% {d['empty_w']:>6}")
    print(f"{'=' * 62}")

    if not args.no_telegram:
        token, chat_id = get_telegram()
        if token and chat_id:
            caption_lines = [
                "dnabot momentum_exit Walk-Forward — Lookback-Analyse (Out-of-Sample)",
                f"Zeitraum: {oos_start.strftime('%Y-%m-%d')} → {oos_end.strftime('%Y-%m-%d')} ({len(week_starts)} Wochen)",
                f"Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT",
                "",
            ]
            for d in sorted(summary_data, key=lambda x: x['calmar'], reverse=True):
                marker = "★ " if d['weeks'] == best_weeks else "  "
                caption_lines.append(
                    f"{marker}{d['weeks']:2d}W: {d['pnl_pct']:+.1f}% | DD {d['max_dd']:.1f}% | "
                    f"Calmar {d['calmar']:.1f} | {d['n_total']} Trades | WR {d['wr']:.1f}%"
                )
            caption_lines.append("")
            caption_lines.append(f"★ Bester Lookback: {best_weeks} Wochen (Calmar {bc:.1f})")
            caption_lines.append(
                f"settings.json {'aktualisiert' if write_ok else 'NICHT geändert (--no-write)'}: "
                f"backtest_lookback_weeks = {best_weeks}"
            )
            print()
            print("  Sende Chart via Telegram...", end='', flush=True)
            send_photo(token, chat_id, chart_path, "\n".join(caption_lines))
            print(f" {G}✓{NC}")
        else:
            print(f"  {Y}Telegram nicht konfiguriert — nur lokaler Chart.{NC}")

    print(f"\n  {G}Walk-Forward Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
