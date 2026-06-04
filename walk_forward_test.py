#!/usr/bin/env python3
"""
walk_forward_test.py — Walk-Forward Analyse: Optimaler Lookback-Zeitraum

Untersucht welcher Lookback-Zeitraum (Wochen zurück) für den wöchentlichen
Auto-Optimizer am besten Out-of-Sample performt.

Methode: Rolling Walk-Forward (kein Lookahead)
  Für jeden Lookback (1, 2, 4, 8, 12, 26 Wochen):
    Pro Test-Woche:
      In-Sample  → letzte N Wochen: Portfolio auswählen
      Out-of-Sample → nächste Woche: Portfolio anwenden, Equity akkumulieren

  Alle Lookbacks laufen auf demselben OOS-Zeitraum (fairer Vergleich).

Ausführung:
    ./run_walkforward.sh
    python3 walk_forward_test.py
    python3 walk_forward_test.py --risk 1.5 --min-trades 2 --no-telegram
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
OUTPUT_PATH   = '/tmp/dnabot_walkforward.png'

LOOKBACK_WINDOWS  = [1, 2, 4, 8, 12, 26]   # Wochen
RR_RATIO          = 2.0
MAX_NOTIONAL_USDT = 200_000.0

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
B  = '\033[1;37m'
NC = '\033[0m'


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def get_telegram_credentials():
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    try:
        with open(secret_path) as f:
            secrets = json.load(f)
        accounts  = secrets.get('dnabot', [])
        acc       = accounts[0] if accounts else {}
        token     = acc.get('telegram_bot_token', '') or secrets.get('telegram', {}).get('bot_token', '')
        chat_id   = acc.get('telegram_chat_id', '')   or secrets.get('telegram', {}).get('chat_id', '')
        return (token, chat_id) if token and chat_id else (None, None)
    except Exception:
        return None, None


def send_telegram_photo(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(
                f'https://api.telegram.org/bot{token}/sendPhoto',
                data={'chat_id': chat_id, 'caption': caption},
                files={'photo': f},
                timeout=30,
            )
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def send_telegram_message(token, chat_id, text):
    try:
        import requests
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def _parse_dt(ts_str):
    try:
        dt = datetime.fromisoformat(str(ts_str))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def next_monday(dt):
    """Rundet auf den nächsten Montag (oder bleibt beim Montag)."""
    days = (7 - dt.weekday()) % 7
    return (dt + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0,
                                               tzinfo=timezone.utc)


# ─── Daten laden ──────────────────────────────────────────────────────────────

def load_all_trades():
    """Lädt alle Trades aus den Backtest-JSON-Dateien mit geparsten Timestamps."""
    all_results = []
    if not os.path.isdir(RESULTS_DIR):
        return all_results
    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        parsed = []
        for t in data.get('trades', []):
            dt = _parse_dt(t.get('entry_time', ''))
            if dt:
                t['entry_dt'] = dt
                parsed.append(t)
        if parsed:
            all_results.append({
                'market':    data['market'],
                'timeframe': data['timeframe'],
                'coin':      data['market'].split('/')[0].upper(),
                'trades':    parsed,
            })
    return all_results


# ─── Simulation ───────────────────────────────────────────────────────────────

def simulate_trades(trades, equity, risk_pct):
    """
    Simuliert eine Liste von Trades auf einem gemeinsamen Kapital-Pool.
    Gibt (final_equity, max_dd, n_wins) zurück.
    """
    peak   = equity
    max_dd = 0.0
    wins   = 0
    for t in sorted(trades, key=lambda x: x['entry_dt']):
        sl_pct      = max(t.get('sl_pct', 1.0), 0.01)
        risk_amount = min(equity * (risk_pct / 100.0), MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome     = t.get('outcome', 'LOSS')
        if outcome == 'WIN':
            pnl = risk_amount * RR_RATIO
            wins += 1
        elif outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return equity, max_dd, wins


# ─── Portfolio-Auswahl ────────────────────────────────────────────────────────

def select_portfolio(all_results, is_start, is_end, min_trades, risk_pct):
    """
    Wählt das Portfolio für das In-Sample Fenster [is_start, is_end).
    Kriterium: Alle Pairs mit >= min_trades und positivem Calmar.
    Constraint: Max 1 Timeframe pro Coin (Bitget-Regel).
    """
    candidates = []
    for r in all_results:
        is_trades = [t for t in r['trades'] if is_start <= t['entry_dt'] < is_end]
        if len(is_trades) < min_trades:
            continue
        # Calmar auf In-Sample berechnen (mit Startkapital = 100 für Vergleichbarkeit)
        final_eq, max_dd, wins = simulate_trades(is_trades, 100.0, risk_pct)
        pnl_pct = (final_eq - 100.0) / 100.0 * 100.0
        if pnl_pct <= 0:
            continue
        calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
        candidates.append({**r, '_calmar': calmar, '_pnl': pnl_pct, '_n': len(is_trades)})

    # Max 1 TF pro Coin: bester Calmar gewinnt
    coin_best = {}
    for c in candidates:
        if c['coin'] not in coin_best or c['_calmar'] > coin_best[c['coin']]['_calmar']:
            coin_best[c['coin']] = c

    return list(coin_best.values())


# ─── Walk-Forward ─────────────────────────────────────────────────────────────

def run_walk_forward(all_results, lookback_weeks, risk_pct, min_trades, week_starts, capital):
    """
    Walk-Forward für einen Lookback-Zeitraum.
    Gibt (equity_curve, total_trades, total_wins, empty_weeks) zurück.
    equity_curve: Liste von (week_end_date, equity, n_portfolio_pairs, n_oos_trades)
    """
    equity      = capital
    curve       = []
    total_n     = 0
    total_wins  = 0
    empty_weeks = 0

    for week_start in week_starts:
        is_start  = week_start - timedelta(weeks=lookback_weeks)
        oos_end   = week_start + timedelta(weeks=1)

        portfolio = select_portfolio(all_results, is_start, week_start, min_trades, risk_pct)

        oos_trades = []
        for p in portfolio:
            oos_trades.extend(
                t for t in p['trades'] if week_start <= t['entry_dt'] < oos_end
            )

        if not oos_trades:
            empty_weeks += 1
            curve.append((oos_end, equity, len(portfolio), 0))
            continue

        equity, _, wins = simulate_trades(oos_trades, equity, risk_pct)
        total_n    += len(oos_trades)
        total_wins += wins
        curve.append((oos_end, equity, len(portfolio), len(oos_trades)))

    return curve, total_n, total_wins, empty_weeks


# ─── Statistiken ──────────────────────────────────────────────────────────────

def compute_stats(curve, capital):
    """Berechnet PnL%, MaxDD% und Calmar aus einer Equity-Kurve."""
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


# ─── Chart ────────────────────────────────────────────────────────────────────

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

    # ─ Panel 1: Equity-Kurven ─
    ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5, linewidth=0.8)
    ax1.text(week_starts[0], capital * 1.02, f'Start {capital:.0f} USDT',
             color='#475569', fontsize=8)

    summary_data = []
    bar_labels, bar_calmar, bar_colors = [], [], []
    best_calmar = -999.0

    for i, (weeks, (curve, n_total, n_wins, empty_w)) in enumerate(
        sorted(results.items())
    ):
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

        summary_data.append({
            'weeks': weeks, 'calmar': calmar, 'pnl_pct': pnl_pct,
            'max_dd': max_dd, 'n_total': n_total, 'wr': wr, 'empty_w': empty_w,
        })
        if calmar > best_calmar:
            best_calmar = calmar

    ax1.set_title(
        f'dnabot Walk-Forward — Lookback-Vergleich (Out-of-Sample)\n'
        f'Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT | '
        f'Test-Wochen: {len(week_starts)}',
        color='white', fontsize=12, pad=10
    )
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=9, loc='upper left', framealpha=0.3,
               facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')

    try:
        all_eq = [capital] + [e[1] for c, _ in results.items() for e in _[0]]
        min_eq = min(e for e in all_eq if e > 0)
        ax1.set_yscale('log')
        ax1.set_ylim(bottom=max(1, min_eq * 0.5))
    except Exception:
        pass

    # ─ Panel 2: Calmar-Balken ─
    ax2.set_title('Calmar Score pro Lookback (Out-of-Sample, höher = besser)',
                  color='white', fontsize=11, pad=8)

    bars = ax2.bar(bar_labels, bar_calmar, color=bar_colors, alpha=0.8, edgecolor='#1e293b',
                   linewidth=1.2)

    for bar, score in zip(bars, bar_calmar):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + abs(max(bar_calmar, default=1)) * 0.01,
                 f'{score:.1f}', ha='center', va='bottom',
                 color='white', fontsize=11, fontweight='bold')

    # Bestes hervorheben
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
    plt.savefig(OUTPUT_PATH, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()

    return OUTPUT_PATH, summary_data


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='dnabot Walk-Forward Lookback Analyse')
    parser.add_argument('--risk',        type=float, default=None,
                        help='Risiko pro Trade in %% (Standard: aus settings.json)')
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--min-trades',  type=int,   default=2,
                        help='Min. Trades pro Pair im Lookback-Fenster')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    settings = load_settings()
    risk_pct = args.risk or settings.get('optimization_settings', {}).get('risk_pct', 1.5)
    capital  = args.capital

    print(f"\n{'=' * 62}")
    print(f"  {B}dnabot — Walk-Forward Lookback Analyse{NC}")
    print(f"{'=' * 62}")
    print(f"  Risk/Trade:   {risk_pct}%")
    print(f"  Startkapital: {capital} USDT")
    print(f"  Min. Trades:  {args.min_trades} (pro Pair im Lookback-Fenster)")
    print(f"  Lookbacks:    {LOOKBACK_WINDOWS} Wochen")
    print()

    # ── Daten laden
    print("  Lade Backtest-Daten...", end='', flush=True)
    all_results = load_all_trades()
    if not all_results:
        print(f"\n  {R}Keine Backtest-Daten. Erst ./show_results.sh → Mode 1 ausführen!{NC}\n")
        sys.exit(1)

    all_dts   = [t['entry_dt'] for r in all_results for t in r['trades']]
    min_date  = min(all_dts)
    max_date  = max(all_dts)
    n_weeks   = (max_date - min_date).days // 7
    print(f" {len(all_results)} Pairs | {min_date.strftime('%Y-%m-%d')} → "
          f"{max_date.strftime('%Y-%m-%d')} ({n_weeks} Wochen)")

    # ── Lookback-Fenster filtern
    active_lookbacks = [w for w in LOOKBACK_WINDOWS if w + 1 <= n_weeks]
    skipped = [w for w in LOOKBACK_WINDOWS if w not in active_lookbacks]
    if skipped:
        print(f"  {Y}Übersprungen (zu wenig Daten): {skipped}W{NC}")
    if not active_lookbacks:
        print(f"  {R}Nicht genug Daten. Tipp: ./show_results.sh → Mode 1 mit früherem Startdatum.{NC}\n")
        sys.exit(1)

    # ── Gemeinsamer OOS-Zeitraum (alle Lookbacks auf identischer Basis)
    max_lookback  = max(active_lookbacks)
    oos_start     = next_monday(min_date + timedelta(weeks=max_lookback))
    oos_end       = next_monday(max_date)

    week_starts = []
    w = oos_start
    while w < oos_end:
        week_starts.append(w)
        w += timedelta(weeks=1)

    if len(week_starts) < 2:
        print(f"  {R}Zu wenig OOS-Wochen ({len(week_starts)}). "
              f"Tipp: Backtest-Daten mit früherem Startdatum generieren.{NC}\n")
        sys.exit(1)

    print(f"  OOS-Zeitraum: {oos_start.strftime('%Y-%m-%d')} → "
          f"{oos_end.strftime('%Y-%m-%d')} ({len(week_starts)} Test-Wochen)")
    print()

    # ── Walk-Forward für jeden Lookback
    results = {}
    for weeks in active_lookbacks:
        print(f"  {C}Lookback {weeks:2d}W ...{NC}", end='', flush=True)
        curve, n_total, n_wins, empty_w = run_walk_forward(
            all_results, weeks, risk_pct, args.min_trades, week_starts, capital
        )
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        wr = n_wins / n_total * 100 if n_total > 0 else 0.0
        results[weeks] = (curve, n_total, n_wins, empty_w)

        col = G if pnl_pct > 0 else R
        print(f"  {col}PnL={pnl_pct:+.1f}% | DD={max_dd:.1f}% | "
              f"Calmar={calmar:.1f} | Trades={n_total} | WR={wr:.1f}% | "
              f"Leerwochen={empty_w}{NC}")

    # ── Bester Lookback
    best_weeks   = max(results, key=lambda w: compute_stats(results[w][0], capital)[0])
    bc, bp, bd   = compute_stats(results[best_weeks][0], capital)

    print()
    print(f"  {'─' * 50}")
    print(f"  {G}★ Bester Lookback: {best_weeks} Wochen{NC}")
    print(f"  Calmar: {bc:.1f} | PnL: {bp:+.1f}% | MaxDD: {bd:.1f}%")
    print(f"  {'─' * 50}")

    # ── Empfehlung für settings.json
    rec_date = (datetime.now(timezone.utc) - timedelta(weeks=best_weeks)).strftime('%Y-%m-%d')
    print()
    print(f"  {Y}Empfehlung für settings.json:{NC}")
    print(f"  optimization_settings.backtest_start_date = \"{rec_date}\"")
    print(f"  (= {best_weeks} Wochen rollierender Lookback)")

    # ── Chart erstellen
    print()
    chart_result = create_chart(results, week_starts, risk_pct, capital)
    if chart_result is None:
        sys.exit(0)

    chart_path, summary_data = chart_result
    print(f"  {G}✓ Chart gespeichert: {chart_path}{NC}")

    # ── Zusammenfassung
    print()
    print(f"{'=' * 62}")
    print(f"  {'Lookback':<10} {'PnL%':>8} {'MaxDD%':>8} {'Calmar':>8} {'Trades':>7} {'WR':>7} {'LeerW':>6}")
    print(f"  {'─' * 56}")
    for d in sorted(summary_data, key=lambda x: x['calmar'], reverse=True):
        marker = '★' if d['weeks'] == best_weeks else ' '
        col = G if d['pnl_pct'] > 0 else R
        print(
            f"  {marker}{d['weeks']:2d}W       "
            f"{col}{d['pnl_pct']:>+8.1f}%{NC} "
            f"{d['max_dd']:>7.1f}% "
            f"{d['calmar']:>8.1f} "
            f"{d['n_total']:>7} "
            f"{d['wr']:>6.1f}% "
            f"{d['empty_w']:>6}"
        )
    print(f"{'=' * 62}")

    # ── Telegram
    if not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption_lines = [
                "dnabot Walk-Forward — Lookback-Analyse (Out-of-Sample)",
                f"Zeitraum: {oos_start.strftime('%Y-%m-%d')} → {oos_end.strftime('%Y-%m-%d')} "
                f"({len(week_starts)} Wochen)",
                f"Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT",
                "",
            ]
            for d in sorted(summary_data, key=lambda x: x['calmar'], reverse=True):
                marker = "★ " if d['weeks'] == best_weeks else "  "
                caption_lines.append(
                    f"{marker}{d['weeks']:2d}W: {d['pnl_pct']:+.1f}% | "
                    f"DD {d['max_dd']:.1f}% | Calmar {d['calmar']:.1f} | "
                    f"{d['n_total']} Trades | WR {d['wr']:.1f}%"
                )
            caption_lines.append("")
            caption_lines.append(f"★ Bester Lookback: {best_weeks} Wochen (Calmar {bc:.1f})")
            caption_lines.append(f"Empfehlung: backtest_start_date = \"{rec_date}\"")

            caption = "\n".join(caption_lines)
            print()
            print("  Sende Chart via Telegram...", end='', flush=True)
            send_telegram_photo(token, chat_id, chart_path, caption)
            print(f" {G}✓{NC}")
        else:
            print(f"  {Y}Telegram nicht konfiguriert — nur lokaler Chart.{NC}")

    print(f"\n  {G}Walk-Forward Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
