#!/usr/bin/env python3
"""
analysis/fee_impact.py — Slippage & Fee Impact Analyse

Zeigt wie verschiedene Gebühren-/Slippage-Niveaus die Performance beeinflussen.
Berechnet den Break-Even-Punkt: ab welchen Gebühren wird der Bot unrentabel?

Gebühren werden auf den NOTIONAL angewendet:
  fee_per_trade = position_size × fee_pct × 2  (Einstieg + Ausstieg)
  position_size = risk_amount / sl_pct

Ausführung:
  python3 analysis/fee_impact.py
  python3 analysis/fee_impact.py --capital 100 --risk 2.5
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')

MAX_NOTIONAL_USDT = 200_000.0

# Getestete Gebühren pro Seite (0.06% = Bitget Taker)
FEE_LEVELS = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
# Slippage auf SL-Execution (% des Entry-Preises, zusätzlich zu Gebühren)
SLIPPAGE_LEVELS = [0.0, 0.05, 0.10, 0.15, 0.20]

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
NC = '\033[0m'


def load_trades():
    trades = []
    if not os.path.isdir(RESULTS_DIR):
        return trades
    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                data = json.load(f)
            for t in data.get('trades', []):
                t['market']    = data['market']
                t['timeframe'] = data['timeframe']
                trades.append(t)
        except Exception:
            continue
    trades.sort(key=lambda t: str(t.get('entry_time', '')))
    return trades


def simulate_with_fees(trades, capital, risk_pct, fee_per_side_pct, slippage_pct):
    """
    Simuliert alle Trades mit Gebühren und Slippage.
    fee_per_side_pct: Gebühr pro Seite in % des Notionals (z.B. 0.06)
    slippage_pct: Zusätzlicher Verlust bei SL-Execution in % des Entry-Preises
    """
    equity = capital
    peak   = equity
    max_dd = 0.0
    wins   = 0

    for t in trades:
        sl_pct      = max(t.get('sl_pct', 1.0), 0.01)
        risk_amount = min(equity * (risk_pct / 100.0), MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome     = t.get('outcome', 'LOSS')

        # Positionsgröße (Notional)
        position_size = risk_amount / (sl_pct / 100.0)

        # Gebühren: Einstieg + Ausstieg (beide Seiten)
        fee_cost = position_size * (fee_per_side_pct / 100.0) * 2.0

        # WIN nutzt wie TIMEOUT die tatsaechlich simulierte Bewegung statt
        # einer pauschalen RR-Konstante (siehe analysis/utils.py::simulate()).
        if outcome == 'WIN':
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct) - fee_cost
            wins += 1
        elif outcome == 'LOSS':
            # Slippage verschlechtert den SL-Exit zusätzlich
            slip_cost = position_size * (slippage_pct / 100.0)
            pnl = -risk_amount - fee_cost - slip_cost
        else:  # TIMEOUT
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct) - fee_cost

        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    n = len(trades)
    pnl_pct = (equity - capital) / capital * 100.0
    wr = wins / n * 100.0 if n > 0 else 0.0
    calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
    return {'equity': equity, 'pnl_pct': pnl_pct, 'max_dd': max_dd,
            'calmar': calmar, 'wr': wr, 'n': n}


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


def create_chart(results_fee, results_slip, trades, capital, risk_pct):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor('#0f172a')
    COLORS = ['#22d3ee', '#16a34a', '#f59e0b', '#f97316', '#ef4444',
              '#dc2626', '#991b1b', '#7f1d1d']

    for ax in axes:
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')

    # Panel 1: Gebühren
    ax1 = axes[0]
    ax1.set_title('Gebühren-Impact (Slippage=0%)', color='white', fontsize=11)
    fee_vals   = [r['fee'] for r in results_fee]
    pnl_vals   = [r['pnl_pct'] for r in results_fee]
    calmar_vals = [r['calmar'] for r in results_fee]

    ax1.bar([f"{f:.2f}%" for f in fee_vals], pnl_vals,
            color=[G if p > 0 else R for G, R, p in
                   zip(['#16a34a']*8, ['#ef4444']*8, pnl_vals)], alpha=0.8)
    ax1.axhline(y=0, color='#ef4444', linewidth=1, linestyle='--')
    ax1.set_xlabel('Gebühr pro Seite')
    ax1.set_ylabel('PnL%')

    for i, (p, c) in enumerate(zip(pnl_vals, calmar_vals)):
        col = 'white' if p > 0 else '#ef4444'
        ax1.text(i, p + (abs(max(pnl_vals, default=1)) * 0.02),
                 f'{p:+.0f}%\nCalmar:{c:.0f}',
                 ha='center', va='bottom', color=col, fontsize=8)

    # Panel 2: Slippage
    ax2 = axes[1]
    ax2.set_title('Slippage-Impact (Gebühr=0.06%/Seite)', color='white', fontsize=11)
    slip_vals = [r['slip'] for r in results_slip]
    pnl_slip  = [r['pnl_pct'] for r in results_slip]

    ax2.bar([f"{s:.2f}%" for s in slip_vals], pnl_slip,
            color=['#16a34a' if p > 0 else '#ef4444' for p in pnl_slip], alpha=0.8)
    ax2.axhline(y=0, color='#ef4444', linewidth=1, linestyle='--')
    ax2.set_xlabel('Slippage bei SL-Execution')
    ax2.set_ylabel('PnL%')

    for i, p in enumerate(pnl_slip):
        col = 'white' if p > 0 else '#ef4444'
        ax2.text(i, p + (abs(max(pnl_slip, default=1)) * 0.02),
                 f'{p:+.0f}%', ha='center', va='bottom', color=col, fontsize=9)

    n = len(trades)
    wr_base = results_fee[0]['wr']
    fig.suptitle(
        f'dnabot Fee & Slippage Impact | {n} Trades | WR {wr_base:.1f}% | '
        f'Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT',
        color='white', fontsize=11
    )
    plt.tight_layout()
    path = '/tmp/dnabot_fee_impact.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    # Auch ins Repo
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'fee_impact_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='dnabot Fee & Slippage Impact')
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print(f"  dnabot — Slippage & Fee Impact Analyse")
    print(f"{'=' * 60}")
    print(f"  Startkapital: {args.capital} USDT | Risk/Trade: {args.risk}%")
    print(f"  Bitget Taker-Gebühr: 0.06%/Seite (Round-Trip: 0.12%)")
    print()

    print("  Lade Trades...", end='', flush=True)
    trades = load_trades()
    if not trades:
        print(f"\n  {R}Keine Backtest-Daten. Erst show_results.sh → Mode 1.{NC}\n")
        sys.exit(1)
    print(f" {len(trades)} Trades geladen.")
    print()

    # Gebühren-Sweep (Slippage = 0)
    print(f"  {'Gebühr/Seite':>12}  {'PnL%':>10}  {'MaxDD%':>8}  {'Calmar':>8}  {'WR':>6}")
    print(f"  {'-' * 52}")
    results_fee = []
    for fee in FEE_LEVELS:
        r = simulate_with_fees(trades, args.capital, args.risk, fee, 0.0)
        results_fee.append({**r, 'fee': fee})
        col = G if r['pnl_pct'] > 0 else R
        marker = ' ← Bitget' if abs(fee - 0.06) < 0.001 else ''
        print(f"  {fee:>10.2f}%  {col}{r['pnl_pct']:>+9.1f}%{NC}  "
              f"{r['max_dd']:>7.1f}%  {r['calmar']:>8.1f}  {r['wr']:>5.1f}%{marker}")

    # Break-Even
    break_even = None
    for i in range(len(results_fee) - 1):
        if results_fee[i]['pnl_pct'] > 0 and results_fee[i+1]['pnl_pct'] <= 0:
            break_even = (FEE_LEVELS[i] + FEE_LEVELS[i+1]) / 2
            break
    print()
    if break_even:
        print(f"  {Y}Break-Even Gebühr: ~{break_even:.2f}%/Seite ({break_even*2:.2f}% Round-Trip){NC}")
    else:
        r0 = results_fee[0]
        if r0['pnl_pct'] > 0:
            print(f"  {G}Profitabel bei allen getesteten Gebühren.{NC}")
        else:
            print(f"  {R}Nicht profitabel — auch ohne Gebühren.{NC}")

    print()

    # Slippage-Sweep (Gebühr = 0.06%)
    print(f"  Slippage-Impact (Gebühr fix 0.06%/Seite):")
    print(f"  {'Slippage':>10}  {'PnL%':>10}  {'MaxDD%':>8}  {'Calmar':>8}")
    print(f"  {'-' * 44}")
    results_slip = []
    for slip in SLIPPAGE_LEVELS:
        r = simulate_with_fees(trades, args.capital, args.risk, 0.06, slip)
        results_slip.append({**r, 'slip': slip})
        col = G if r['pnl_pct'] > 0 else R
        print(f"  {slip:>8.2f}%  {col}{r['pnl_pct']:>+9.1f}%{NC}  "
              f"{r['max_dd']:>7.1f}%  {r['calmar']:>8.1f}")

    # Chart
    print()
    path = create_chart(results_fee, results_slip, trades, args.capital, args.risk)
    if path:
        print(f"  {G}✓ Chart gespeichert: {path}{NC}")
        if not args.no_telegram:
            token, chat_id = get_telegram_credentials()
            if token:
                bitget_result = next((r for r in results_fee
                                      if abs(r['fee'] - 0.06) < 0.001), results_fee[0])
                caption = (
                    f"dnabot Fee & Slippage Impact\n"
                    f"{len(trades)} Trades | WR {results_fee[0]['wr']:.1f}% | "
                    f"Risk/Trade: {args.risk}%\n\n"
                    f"Ohne Gebühren: {results_fee[0]['pnl_pct']:+.1f}%\n"
                    f"Mit 0.06%/Seite (Bitget): {bitget_result['pnl_pct']:+.1f}%\n"
                    + (f"Break-Even: ~{break_even:.2f}%/Seite" if break_even else "")
                )
                send_telegram_photo(token, chat_id, path, caption)
                print(f"  {G}✓ Via Telegram gesendet.{NC}")

    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
