#!/usr/bin/env python3
"""
analysis/param_optimizer.py — Parameter Walk-Forward Optimierung

Testet out-of-sample welcher Wert für einen Parameter optimal ist:
  a) RR-Ratio         (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0)
  b) Score Threshold  (0.01, 0.05, 0.08, 0.12, 0.15, 0.20, 0.30)
  c) Trailing Callback (0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0)

Methode: Identisch zu walk_forward_test.py — Walk-Forward ohne Lookahead.
Das optimale Ergebnis kann direkt in settings.json übernommen werden.

Ausführung:
  python3 analysis/param_optimizer.py --param rr
  python3 analysis/param_optimizer.py --param score
  python3 analysis/param_optimizer.py --param callback
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

MAX_NOTIONAL_USDT = 200_000.0
MIN_TRADES        = 2

PARAM_RANGES = {
    'rr':       [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
    'score':    [0.01, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30],
    'callback': [0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0],
}
PARAM_LABELS = {
    'rr':       'RR-Ratio',
    'score':    'Min. Score Threshold',
    'callback': 'Trailing Callback %',
}
SETTINGS_KEYS = {
    'rr':       ('risk_settings', 'rr_ratio'),
    'score':    ('genome_settings', 'min_score'),
    'callback': ('risk_settings', 'trailing_callback_rate_pct'),
}

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
NC = '\033[0m'


def _parse_dt(ts):
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def next_monday(dt):
    days = (7 - dt.weekday()) % 7
    return (dt + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def load_active_pairs():
    """Gibt die aktiven (market, timeframe) Paare aus settings.json zurück."""
    try:
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
        strats = s.get('live_trading_settings', {}).get('active_strategies', [])
        return {(st['symbol'], st['timeframe']) for st in strats if st.get('active', True)}
    except Exception:
        return set()


def load_all_trades():
    """Lädt Trades aus den Backtest-JSON-Dateien — gefiltert auf active_strategies."""
    active_pairs = load_active_pairs()

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

        if active_pairs and (data['market'], data['timeframe']) not in active_pairs:
            continue

        parsed = []
        for t in data.get('trades', []):
            dt = _parse_dt(t.get('entry_time', ''))
            if dt:
                t['entry_dt'] = dt
                parsed.append(t)
        if parsed:
            all_results.append({
                'market': data['market'], 'timeframe': data['timeframe'],
                'coin':   data['market'].split('/')[0].upper(), 'trades': parsed,
            })
    return all_results


def simulate_trades(trades, capital, risk_pct, rr_ratio=2.0, callback_pct=1.0, leverage=1):
    """Simuliert Trades mit variierbarem RR-Ratio, Callback und Leverage-Cap."""
    equity = capital
    peak   = equity
    max_dd = 0.0
    wins   = 0
    for t in sorted(trades, key=lambda x: x['entry_dt']):
        sl_pct      = max(t.get('sl_pct', 1.0), 0.01)
        leverage_cap = equity * max(leverage, 1) * (sl_pct / 100.0)
        risk_amount = min(equity * (risk_pct / 100.0),
                          leverage_cap,
                          MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome = t.get('outcome', 'LOSS')
        if outcome == 'WIN':
            # Callback reduziert den effektiven Gewinn leicht
            effective_rr = rr_ratio * (1.0 - callback_pct / 100.0)
            pnl = risk_amount * max(effective_rr, 0.1)
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


def select_portfolio(all_results, is_start, is_end, min_score, risk_pct, rr_ratio=2.0, leverage=1):
    candidates = []
    for r in all_results:
        is_trades = [t for t in r['trades'] if is_start <= t['entry_dt'] < is_end]
        if len(is_trades) < MIN_TRADES:
            continue
        # Score-Filter
        if min_score > 0:
            is_trades = [t for t in is_trades
                         if t.get('genome_score', 0) >= min_score]
        if len(is_trades) < MIN_TRADES:
            continue
        final_eq, max_dd, wins = simulate_trades(is_trades, 100.0, risk_pct, rr_ratio, leverage=leverage)
        pnl_pct = (final_eq - 100.0) / 100.0 * 100.0
        if pnl_pct <= 0:
            continue
        calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
        candidates.append({**r, '_calmar': calmar})
    coin_best = {}
    for c in candidates:
        if c['coin'] not in coin_best or c['_calmar'] > coin_best[c['coin']]['_calmar']:
            coin_best[c['coin']] = c
    return list(coin_best.values())


def run_walk_forward_param(all_results, param_value, param, risk_pct,
                            capital, week_starts, lookback_weeks=2, leverage=1):
    rr_ratio   = param_value if param == 'rr'       else 2.0
    min_score  = param_value if param == 'score'    else 0.08
    callback   = param_value if param == 'callback' else 1.0

    equity = capital
    curve  = []
    total_n = 0
    total_wins = 0

    for week_start in week_starts:
        is_start = week_start - timedelta(weeks=lookback_weeks)
        oos_end  = week_start + timedelta(weeks=1)

        portfolio = select_portfolio(all_results, is_start, week_start,
                                     min_score, risk_pct, rr_ratio, leverage=leverage)
        oos_trades = []
        for p in portfolio:
            oos_trades.extend(
                t for t in p['trades'] if week_start <= t['entry_dt'] < oos_end)

        if oos_trades:
            equity, _, wins = simulate_trades(oos_trades, equity, risk_pct,
                                               rr_ratio, callback, leverage=leverage)
            total_n    += len(oos_trades)
            total_wins += wins
        curve.append((oos_end, equity))

    pnl_pct = (equity - capital) / capital * 100.0
    eq_vals = [capital] + [e[1] for e in curve]
    peak = eq_vals[0]
    max_dd = 0.0
    for e in eq_vals:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
    wr = total_wins / total_n * 100 if total_n > 0 else 0.0
    return curve, pnl_pct, max_dd, calmar, total_n, wr


def get_telegram_credentials():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            s = json.load(f)
        acc = s.get('dnabot', [{}])[0]
        token = acc.get('telegram_bot_token', '') or s.get('telegram', {}).get('bot_token', '')
        chat_id = acc.get('telegram_chat_id', '') or s.get('telegram', {}).get('chat_id', '')
        return (token, chat_id) if token and chat_id else (None, None)
    except Exception:
        return None, None


def send_telegram_photo(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                          data={'chat_id': chat_id, 'caption': caption},
                          files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def create_chart(results, week_starts, param, capital, risk_pct):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706',
              '#7c3aed', '#0891b2', '#db2777', '#059669']
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))
    fig.patch.set_facecolor('#0f172a')

    for ax in (ax1, ax2):
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')

    ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5)

    bar_vals, bar_labels, bar_cols = [], [], []
    best_calmar = -999.0

    for i, (val, (curve, pnl, dd, calmar, n, wr)) in enumerate(results.items()):
        dates    = [week_starts[0]] + [e[0] for e in curve]
        equities = [capital]        + [e[1] for e in curve]
        color = COLORS[i % len(COLORS)]
        label = f"{val}: Calmar {calmar:.0f} | {pnl:+.0f}% | DD {dd:.1f}%"
        try:
            ax1.plot(dates, equities, color=color, linewidth=2, label=label, zorder=3)
        except Exception:
            pass
        bar_vals.append(calmar)
        bar_labels.append(str(val))
        bar_cols.append(color)
        if calmar > best_calmar:
            best_calmar = calmar

    ax1.set_title(f'dnabot {PARAM_LABELS[param]} Optimierung (Walk-Forward OOS)\n'
                  f'Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT',
                  color='white', fontsize=12)
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=9, loc='upper left', framealpha=0.3,
               facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')
    try:
        ax1.set_yscale('log')
    except Exception:
        pass

    ax2.set_title(f'Calmar Score pro {PARAM_LABELS[param]} (höher = besser)',
                  color='white', fontsize=11)
    bars = ax2.bar(bar_labels, bar_vals, color=bar_cols, alpha=0.8, edgecolor='#1e293b')
    for bar, score in zip(bars, bar_vals):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                 f'{score:.0f}', ha='center', va='bottom', color='white',
                 fontsize=11, fontweight='bold')
    if bar_vals:
        best_idx = bar_vals.index(max(bar_vals))
        bars[best_idx].set_edgecolor('#fbbf24')
        bars[best_idx].set_linewidth(3)
    ax2.set_xlabel(PARAM_LABELS[param], color='#94a3b8')
    ax2.set_ylabel('Calmar (OOS)', color='#94a3b8')
    ax2.axhline(y=0, color='#475569', linewidth=0.8)

    plt.tight_layout(pad=2.5)
    path = f'/tmp/dnabot_param_{param}.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', f'param_{param}_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='dnabot Parameter Walk-Forward Optimizer')
    parser.add_argument('--param',       type=str,   choices=['rr', 'score', 'callback'],
                        required=True)
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--lookback',    type=int,   default=2,
                        help='Lookback-Wochen für Portfolio-Selektion')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    label  = PARAM_LABELS[args.param]
    values = PARAM_RANGES[args.param]

    # Leverage aus settings.json
    try:
        with open(SETTINGS_PATH) as f:
            _s = json.load(f)
        leverage = int(_s.get('risk_settings', {}).get('leverage', 1))
    except Exception:
        leverage = 1

    print(f"\n{'=' * 62}")
    print(f"  dnabot — {label} Walk-Forward Optimierung")
    print(f"{'=' * 62}")
    print(f"  Testwerte: {values}")
    print(f"  Risk/Trade: {args.risk}% | Kapital: {args.capital} USDT | "
          f"Lookback: {args.lookback}W | Leverage: {leverage}x")
    print()

    print("  Lade Backtest-Daten...", end='', flush=True)
    all_results = load_all_trades()
    if not all_results:
        print(f"\n  {R}Keine Daten. Erst show_results.sh → Mode 1.{NC}\n")
        sys.exit(1)

    all_dts = [t['entry_dt'] for r in all_results for t in r['trades']]
    min_date = min(all_dts)
    max_date = max(all_dts)
    n_weeks  = (max_date - min_date).days // 7
    print(f" {len(all_results)} Pairs | {n_weeks} Wochen Daten")

    oos_start   = next_monday(min_date + timedelta(weeks=max(args.lookback, 2)))
    oos_end     = next_monday(max_date)
    week_starts = []
    w = oos_start
    while w < oos_end:
        week_starts.append(w)
        w += timedelta(weeks=1)

    if len(week_starts) < 2:
        print(f"  {R}Zu wenig OOS-Wochen.{NC}\n")
        sys.exit(1)

    print(f"  OOS: {oos_start.strftime('%Y-%m-%d')} → "
          f"{oos_end.strftime('%Y-%m-%d')} ({len(week_starts)} Wochen)")
    print()

    results = {}
    for val in values:
        print(f"  {C}{label} = {val} ...{NC}", end='', flush=True)
        curve, pnl, dd, calmar, n, wr = run_walk_forward_param(
            all_results, val, args.param, args.risk,
            args.capital, week_starts, args.lookback, leverage=leverage
        )
        results[val] = (curve, pnl, dd, calmar, n, wr)
        col = G if pnl > 0 else R
        print(f"  {col}PnL={pnl:+.1f}% | DD={dd:.1f}% | "
              f"Calmar={calmar:.1f} | Trades={n} | WR={wr:.1f}%{NC}")

    best_val    = max(results, key=lambda v: results[v][3])
    bc, bp, bdd = results[best_val][3], results[best_val][1], results[best_val][2]

    print()
    print(f"  {'─' * 50}")
    print(f"  {G}★ Optimaler Wert: {label} = {best_val}{NC}")
    print(f"  Calmar: {bc:.1f} | PnL: {bp:+.1f}% | MaxDD: {bdd:.1f}%")
    print(f"  {'─' * 50}")

    # Empfehlung für settings.json
    section, key = SETTINGS_KEYS[args.param]
    print()
    print(f"  {Y}Empfehlung für settings.json:{NC}")
    print(f"  {section}.{key} = {best_val}")

    # Optional: settings.json aktualisieren
    try:
        ans = input("\n  In settings.json übernehmen? (j/n): ").strip().lower()
        if ans in ('j', 'ja', 'y', 'yes'):
            with open(SETTINGS_PATH) as f:
                s = json.load(f)
            s.setdefault(section, {})[key] = best_val
            with open(SETTINGS_PATH, 'w') as f:
                json.dump(s, f, indent=2, ensure_ascii=False)
            print(f"  {G}✓ settings.json aktualisiert.{NC}")
    except (EOFError, KeyboardInterrupt):
        pass

    path = create_chart(results, week_starts, args.param, args.capital, args.risk)
    if path:
        print(f"\n  {G}✓ Chart gespeichert: {path}{NC}")
        if not args.no_telegram:
            token, chat_id = get_telegram_credentials()
            if token:
                lines = [f"dnabot {label} Optimierung (Walk-Forward OOS)",
                         f"Risk/Trade: {args.risk}% | {len(week_starts)} Test-Wochen\n"]
                for val, (_, pnl, dd, calmar, n, wr) in sorted(results.items()):
                    m = "★ " if val == best_val else "  "
                    lines.append(f"{m}{val}: Calmar {calmar:.0f} | "
                                  f"{pnl:+.0f}% | DD {dd:.1f}% | {n} Trades")
                lines.append(f"\n★ Optimal: {label} = {best_val}")
                send_telegram_photo(token, chat_id, path, "\n".join(lines))
                print(f"  {G}✓ Via Telegram gesendet.{NC}")

    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
