#!/usr/bin/env python3
# run_manual_portfolio.py
# Manuelle Portfolio-Simulation: Nutzer wählt Pairs, Bot simuliert kombinierten Kapital-Pool.

import os
import sys
import json
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

G   = '\033[0;32m'
Y   = '\033[1;33m'
R   = '\033[0;31m'
C   = '\033[0;36m'
B   = '\033[1;37m'
NC  = '\033[0m'

RR_RATIO = 2.0


def load_all_results(start_date=None, end_date=None):
    results = []
    if not os.path.isdir(RESULTS_DIR):
        return results

    sd = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc) if start_date else None
    ed = datetime.fromisoformat(end_date + 'T23:59:59').replace(tzinfo=timezone.utc) if end_date else None

    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        path = os.path.join(RESULTS_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue

        trades = data.get('trades', [])
        if sd or ed:
            filtered = []
            for t in trades:
                ts = t.get('entry_time', '')
                if not ts:
                    continue
                try:
                    t_dt = datetime.fromisoformat(str(ts))
                    if t_dt.tzinfo is None:
                        t_dt = t_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if sd and t_dt < sd:
                    continue
                if ed and t_dt > ed:
                    continue
                filtered.append(t)
            trades = filtered

        results.append({
            'market':    data['market'],
            'timeframe': data['timeframe'],
            'trades':    trades,
        })

    return results


def simulate_portfolio(pair_results, capital, risk_pct):
    if not pair_results:
        return {'total_pnl_pct': 0.0, 'final_equity': capital,
                'max_dd': 0.0, 'n_trades': 0, 'win_rate': 0.0}

    all_trades = []
    for pr in pair_results:
        for t in pr['trades']:
            all_trades.append({
                'market':     pr['market'],
                'timeframe':  pr['timeframe'],
                'outcome':    t.get('outcome', 'LOSS'),
                'pnl_pct':    t.get('pnl_pct', 0.0),
                'sl_pct':     t.get('sl_pct', 1.0),
                'entry_time': str(t.get('entry_time', '')),
            })

    all_trades.sort(key=lambda t: t['entry_time'])

    equity = capital
    peak   = equity
    max_dd = 0.0
    wins   = 0

    for t in all_trades:
        risk_amount = equity * (risk_pct / 100.0)
        outcome     = t['outcome']
        sl_pct      = max(t['sl_pct'], 0.01)

        if outcome == 'WIN':
            pnl = risk_amount * RR_RATIO
            wins += 1
        elif outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (t['pnl_pct'] / sl_pct)

        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    n = len(all_trades)
    total_pnl_pct = (equity - capital) / capital * 100.0 if capital > 0 else 0.0

    return {
        'total_pnl_pct': total_pnl_pct,
        'final_equity':  equity,
        'max_dd':        max_dd,
        'n_trades':      n,
        'win_rate':      wins / n if n > 0 else 0.0,
    }


def compute_single_stats(trades, capital, risk_pct):
    return simulate_portfolio(
        [{'market': '', 'timeframe': '', 'trades': trades}],
        capital, risk_pct
    )


def select_pairs(all_results, capital, risk_pct):
    """Zeigt alle Pairs mit PnL% und lässt Nutzer auswählen."""
    # Einzel-Stats berechnen
    pairs_with_stats = []
    for r in all_results:
        st = compute_single_stats(r['trades'], capital, risk_pct)
        pairs_with_stats.append({**r, 'stats': st})

    # Nach PnL% sortieren
    pairs_with_stats.sort(key=lambda x: x['stats']['total_pnl_pct'], reverse=True)

    w = 72
    print(f"\n{'=' * w}")
    print(f"{B}  Verfügbare Pairs{NC}")
    print(f"  {'Nr':<4} {'Markt':<24} {'TF':<6} {'Trades':>7} {'WR':>7} {'PnL%':>9} {'MaxDD':>8}")
    print(f"  {'-' * (w - 2)}")

    for i, pr in enumerate(pairs_with_stats, 1):
        st = pr['stats']
        if st['n_trades'] == 0:
            continue
        pnl_col = G if st['total_pnl_pct'] > 0 else R
        wr_col  = G if st['win_rate'] >= 0.50 else (Y if st['win_rate'] >= 0.43 else R)
        sign    = '+' if st['total_pnl_pct'] >= 0 else ''
        print(
            f"  {i:<4} {pr['market']:<24} {pr['timeframe']:<6} {st['n_trades']:>7} "
            f"{wr_col}{st['win_rate']:>6.1%}{NC} "
            f"{pnl_col}{sign}{st['total_pnl_pct']:>7.1f}%{NC} "
            f"{st['max_dd']:>7.1f}%"
        )

    print(f"{'=' * w}")
    print(f"\n  Eingabe: Nummern kommagetrennt (z.B. {Y}1,3,5{NC}) oder {Y}alle{NC}")

    try:
        sel = input("  Auswahl: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if not sel or sel.lower() in ('alle', 'all', 'a'):
        return [p for p in pairs_with_stats if p['stats']['n_trades'] > 0]

    selected = []
    valid_pairs = [p for p in pairs_with_stats if p['stats']['n_trades'] > 0]
    try:
        indices = [int(x.strip()) - 1 for x in sel.split(',')]
        for idx in indices:
            if 0 <= idx < len(valid_pairs):
                selected.append(valid_pairs[idx])
    except ValueError:
        print(f"  {R}Ungültige Eingabe.{NC}")
        return []

    return selected


def build_telegram_report(selected, pm, capital, risk_pct, start_date, end_date):
    date_range = f"{start_date or '...'} → {end_date or 'heute'}"
    sign = '+' if pm['total_pnl_pct'] >= 0 else ''
    lines = [
        "dnabot — Manuelle Portfolio-Simulation",
        f"Zeitraum: {date_range}",
        f"Kapital: {capital:.0f} USDT | Risiko: {risk_pct}%",
        "",
        f"Ausgewählte Pairs ({len(selected)}):",
    ]
    for pr in selected:
        st = pr['stats']
        s = '+' if st['total_pnl_pct'] >= 0 else ''
        lines.append(
            f"  {pr['market']} {pr['timeframe']} | "
            f"{st['n_trades']} Trades | WR {st['win_rate']:.1%} | PnL {s}{st['total_pnl_pct']:.1f}%"
        )
    lines += [
        "",
        f"Portfolio gesamt:",
        f"  Trades:       {pm['n_trades']}",
        f"  Win-Rate:     {pm['win_rate']:.1%}",
        f"  PnL:          {sign}{pm['total_pnl_pct']:.1f}%",
        f"  Final Equity: {pm['final_equity']:.2f} USDT",
        f"  Max Drawdown: {pm['max_dd']:.1f}%",
    ]
    return "\n".join(lines)


def print_result(selected, pm, capital, risk_pct, start_date, end_date):
    w = 72
    date_range = f" | {start_date or '...'} → {end_date or 'heute'}"
    print(f"\n{'=' * w}")
    print(f"{B}  dnabot — Manuelle Portfolio-Simulation{NC}")
    print(f"  Zeitraum:{date_range}")
    print(f"  Kapital: {capital:.0f} USDT | Risiko/Trade: {risk_pct}% (gemeinsamer Pool)")
    print(f"{'=' * w}")

    print(f"\n  {G}Ausgewählte Pairs — {len(selected)} Pair(s){NC}")
    print(f"  {'Markt':<24} {'TF':<6} {'Trades':>7} {'WR':>7} {'PnL%':>9} {'MaxDD':>8}")
    print(f"  {'-' * (w - 2)}")

    for pr in selected:
        st      = pr['stats']
        pnl_col = G if st['total_pnl_pct'] > 0 else R
        wr_col  = G if st['win_rate'] >= 0.50 else (Y if st['win_rate'] >= 0.43 else R)
        sign    = '+' if st['total_pnl_pct'] >= 0 else ''
        print(
            f"  {pr['market']:<24} {pr['timeframe']:<6} {st['n_trades']:>7} "
            f"{wr_col}{st['win_rate']:>6.1%}{NC} "
            f"{pnl_col}{sign}{st['total_pnl_pct']:>7.1f}%{NC} "
            f"{st['max_dd']:>7.1f}%"
        )

    pnl_col = G if pm['total_pnl_pct'] > 0 else R
    sign    = '+' if pm['total_pnl_pct'] >= 0 else ''
    print(f"\n  {'─' * (w - 2)}")
    print(f"  {B}Portfolio gesamt (gemeinsamer Kapital-Pool, alle Trades kompoundiert):{NC}")
    print(f"  Trades total:  {pm['n_trades']}")
    print(f"  Win-Rate:      {pm['win_rate']:.1%}")
    print(f"  PnL:           {pnl_col}{sign}{pm['total_pnl_pct']:.1f}%{NC}")
    print(f"  Final Equity:  {pm['final_equity']:.2f} USDT")
    print(f"  Max Drawdown:  {pm['max_dd']:.1f}%")
    print(f"{'=' * w}\n")


def send_telegram(report_text):
    # Telegram-Credentials aus secret.json lesen
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    bot_token = ''
    chat_id   = ''
    try:
        with open(secret_path) as f:
            secrets = json.load(f)
        tg = secrets.get('telegram', {})
        bot_token = tg.get('bot_token', '')
        chat_id   = tg.get('chat_id', '')
    except Exception:
        pass

    # Fallback: settings.json (legacy)
    if not bot_token or not chat_id:
        try:
            with open(SETTINGS_PATH) as f:
                settings = json.load(f)
            cfg       = settings.get('dnabot', settings)
            bot_token = bot_token or cfg.get('telegram_bot_token') or settings.get('telegram_bot_token', '')
            chat_id   = chat_id   or cfg.get('telegram_chat_id')   or settings.get('telegram_chat_id', '')
        except Exception:
            pass

    if not bot_token or not chat_id:
        print(f"  {Y}Kein Telegram-Token/Chat-ID in secret.json — übersprungen.{NC}")
        return

    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))
    from dnabot.utils.telegram import send_message
    send_message(bot_token, chat_id, report_text)
    print(f"  {G}✓ Telegram-Nachricht gesendet.{NC}")


def main():
    parser = argparse.ArgumentParser(description="dnabot Manuelle Portfolio-Simulation")
    parser.add_argument('--capital',    type=float, default=1000.0)
    parser.add_argument('--risk',       type=float, default=1.0)
    parser.add_argument('--start-date', type=str,   default=None)
    parser.add_argument('--end-date',   type=str,   default=None)
    parser.add_argument('--telegram',   action='store_true')
    args = parser.parse_args()

    w = 72
    date_range = f" | {args.start_date or '...'} → {args.end_date or 'heute'}"
    print(f"\n{'─' * w}")
    print(f"{B}  dnabot Manuelle Portfolio-Simulation{NC}")
    print(f"  Kapital: {args.capital:.0f} USDT | Risiko/Trade: {args.risk}%{date_range}")
    print(f"  Modell: Gemeinsamer Kapital-Pool — alle Trades kompoundieren zusammen")
    print(f"{'─' * w}\n")

    print("  Lade Backtest-Ergebnisse ...", end='', flush=True)
    all_results = load_all_results(args.start_date, args.end_date)
    with_trades = [r for r in all_results if len(r['trades']) > 0]

    if not with_trades:
        print(f"\n  {R}Keine Backtest-Ergebnisse gefunden.{NC}")
        print("  Zuerst Mode 1 (Einzel-Backtest) ausführen!\n")
        sys.exit(1)

    print(f" {len(all_results)} Dateien, {len(with_trades)} mit Trades.")

    selected = select_pairs(with_trades, args.capital, args.risk)

    if not selected:
        print(f"\n  {Y}Keine Pairs ausgewählt. Abbruch.{NC}\n")
        sys.exit(0)

    pm = simulate_portfolio(selected, args.capital, args.risk)
    print_result(selected, pm, args.capital, args.risk, args.start_date, args.end_date)

    send_tg = args.telegram
    if not send_tg:
        try:
            ans = input("  Ergebnisse an Telegram senden? (j/n): ").strip().lower()
            send_tg = ans in ('j', 'ja', 'y', 'yes')
        except (EOFError, KeyboardInterrupt):
            pass

    if send_tg:
        report = build_telegram_report(selected, pm, args.capital, args.risk,
                                       args.start_date, args.end_date)
        send_telegram(report)


if __name__ == '__main__':
    main()
