#!/usr/bin/env python3
"""
analysis/fee_impact_momentum_exit.py — Gebuehren-Impact-Analyse fuer die
momentum_exit-Strategie (Fund AQ/AR). Frueher wiederverwendete dieses Skript
simulate_with_fees()/create_chart() aus analysis/fee_impact.py -- das wurde
beim Genome-System-Cleanup (2026-08-24) zusammen mit dem restlichen Genome-
Analyse-Tooling entfernt (mischte alte Genom-Trades mit momentum_exit-Trades
in einer Kurve, was die gute momentum_exit-Performance irrefuehrend
ertraenkte). Die noch benoetigten Funktionen sind hier direkt uebernommen.
"""
import os
import sys
import json
import argparse

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
NC = '\033[0m'


def load_momentum_exit_trades():
    trades = []
    if not os.path.isdir(RESULTS_DIR):
        return trades
    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('_momentum_exit.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname), encoding='utf-8') as f:
                data = json.load(f)
            for t in data.get('trades', []):
                t['market'] = data['market']
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

        position_size = risk_amount / (sl_pct / 100.0)
        fee_cost = position_size * (fee_per_side_pct / 100.0) * 2.0

        if outcome == 'WIN':
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct) - fee_cost
            wins += 1
        elif outcome == 'LOSS':
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
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), encoding='utf-8') as f:
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
    fee_vals    = [r['fee'] for r in results_fee]
    pnl_vals    = [r['pnl_pct'] for r in results_fee]
    calmar_vals = [r['calmar'] for r in results_fee]

    ax1.bar([f"{f:.2f}%" for f in fee_vals], pnl_vals,
            color=['#16a34a' if p > 0 else '#ef4444' for p in pnl_vals], alpha=0.8)
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
        f'dnabot momentum_exit Fee & Slippage Impact | {n} Trades | WR {wr_base:.1f}% | '
        f'Risk/Trade: {risk_pct}% | Startkapital: {capital:.0f} USDT',
        color='white', fontsize=11
    )
    plt.tight_layout()
    tmp_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, 'dnabot_fee_impact_momentum_exit.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'fee_impact_momentum_exit_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='dnabot Fee Impact -- NUR momentum_exit')
    parser.add_argument('--capital', type=float, default=1000.0)
    parser.add_argument('--risk', type=float, default=1.0)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print("  dnabot -- Gebuehren-Impact NUR momentum_exit (Fund AQ/AR)")
    print(f"{'=' * 60}")
    print(f"  Startkapital: {args.capital} USDT | Risk/Trade: {args.risk}%")
    print("  Bitget Taker-Gebühr: 0.06%/Seite (Round-Trip: 0.12%)\n")

    print("  Lade momentum_exit-Trades...", end='', flush=True)
    trades = load_momentum_exit_trades()
    if not trades:
        print(f"\n  {R}Keine momentum_exit-Backtest-Daten gefunden.{NC}\n")
        sys.exit(1)
    print(f" {len(trades)} Trades geladen (nur momentum_exit).\n")

    by_market = {}
    for t in trades:
        key = f"{t['market']} ({t['timeframe']})"
        by_market[key] = by_market.get(key, 0) + 1
    print("  Enthaltene Strategien:")
    for k, v in by_market.items():
        print(f"    {k}: {v} Trades")
    print()

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

    break_even = None
    for i in range(len(results_fee) - 1):
        if results_fee[i]['pnl_pct'] > 0 and results_fee[i + 1]['pnl_pct'] <= 0:
            break_even = (FEE_LEVELS[i] + FEE_LEVELS[i + 1]) / 2
            break
    print()
    if break_even:
        print(f"  {Y}Break-Even Gebühr: ~{break_even:.2f}%/Seite ({break_even*2:.2f}% Round-Trip){NC}")
    else:
        r0 = results_fee[0]
        print(f"  {G}Profitabel bei allen getesteten Gebühren.{NC}" if r0['pnl_pct'] > 0
              else f"  {R}Nicht profitabel — auch ohne Gebühren.{NC}")
    print()

    print("  Slippage-Impact (Gebühr fix 0.06%/Seite):")
    print(f"  {'Slippage':>10}  {'PnL%':>10}  {'MaxDD%':>8}  {'Calmar':>8}")
    print(f"  {'-' * 44}")
    results_slip = []
    for slip in SLIPPAGE_LEVELS:
        r = simulate_with_fees(trades, args.capital, args.risk, 0.06, slip)
        results_slip.append({**r, 'slip': slip})
        col = G if r['pnl_pct'] > 0 else R
        print(f"  {slip:>8.2f}%  {col}{r['pnl_pct']:>+9.1f}%{NC}  "
              f"{r['max_dd']:>7.1f}%  {r['calmar']:>8.1f}")

    print()
    path = create_chart(results_fee, results_slip, trades, args.capital, args.risk)
    if path:
        print(f"  {G}✓ Chart gespeichert: {path}{NC}")
        if not args.no_telegram:
            token, chat_id = get_telegram_credentials()
            if token:
                bitget_result = next((r for r in results_fee if abs(r['fee'] - 0.06) < 0.001), results_fee[0])
                caption = (
                    f"dnabot Fee Impact -- NUR momentum_exit\n"
                    f"{len(trades)} Trades | WR {results_fee[0]['wr']:.1f}% | "
                    f"Bei Bitget-Gebühr: {bitget_result['pnl_pct']:+.1f}%"
                )
                send_telegram_photo(token, chat_id, path, caption)
                print(f"  {G}✓ Via Telegram gesendet.{NC}")

    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
