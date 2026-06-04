#!/usr/bin/env python3
"""
analysis/monte_carlo.py — Monte Carlo Simulation

Simulates 10.000 zufällige Trade-Reihenfolgen auf Basis der echten
Win/Loss/Timeout-Verteilung aus den Backtest-Daten.

Beantwortet:
  - Was ist das schlechteste realistisch mögliche Ergebnis?
  - Mit welcher Wahrscheinlichkeit verliert man mehr als X%?
  - Wie hoch ist die Ruin-Wahrscheinlichkeit (Equity < 50% Start)?

Ausführung:
  python3 analysis/monte_carlo.py
  python3 analysis/monte_carlo.py --simulations 10000 --capital 100 --risk 2.5
"""

import os
import sys
import json
import random
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR  = os.path.join(PROJECT_ROOT, 'artifacts', 'results')

RR_RATIO          = 2.0
MAX_NOTIONAL_USDT = 200_000.0

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
            trades.extend(data.get('trades', []))
        except Exception:
            continue
    return trades


def simulate_path(trade_outcomes, capital, risk_pct):
    """Simuliert einen einzelnen Pfad mit zufälliger Trade-Reihenfolge."""
    shuffled = list(trade_outcomes)
    random.shuffle(shuffled)
    equity = capital
    peak   = equity
    max_dd = 0.0
    for outcome, sl_pct, pnl_pct in shuffled:
        sl_pct      = max(sl_pct, 0.01)
        risk_amount = min(equity * (risk_pct / 100.0),
                          MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        if outcome == 'WIN':
            pnl = risk_amount * RR_RATIO
        elif outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (pnl_pct / sl_pct)
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return equity, max_dd


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


def create_chart(final_equities, max_dds, capital, risk_pct, n_sims, n_trades):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor('#0f172a')

    for ax in (ax1, ax2):
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')

    pnl_pcts = [(e - capital) / capital * 100 for e in final_equities]

    # Panel 1: Final Equity Verteilung
    ax1.hist(pnl_pcts, bins=80, color='#2563eb', alpha=0.7, edgecolor='none')
    p5  = float(np.percentile(pnl_pcts, 5))
    p50 = float(np.percentile(pnl_pcts, 50))
    p95 = float(np.percentile(pnl_pcts, 95))
    ax1.axvline(p5,  color='#ef4444', linewidth=2, linestyle='--',
                label=f'5. Perzentil: {p5:+.0f}%')
    ax1.axvline(p50, color='#fbbf24', linewidth=2, linestyle='-',
                label=f'Median: {p50:+.0f}%')
    ax1.axvline(p95, color='#16a34a', linewidth=2, linestyle='--',
                label=f'95. Perzentil: {p95:+.0f}%')
    ax1.axvline(0, color='white', linewidth=1, alpha=0.4)
    ax1.set_xlabel('PnL% nach allen Trades')
    ax1.set_ylabel('Häufigkeit')
    ax1.set_title('Verteilung der Endkapitale', color='white')
    ax1.legend(fontsize=9, facecolor='#1e293b', labelcolor='white', framealpha=0.5)

    # Panel 2: Max Drawdown Verteilung
    ax2.hist(max_dds, bins=60, color='#dc2626', alpha=0.7, edgecolor='none')
    dd50 = float(np.percentile(max_dds, 50))
    dd95 = float(np.percentile(max_dds, 95))
    ax2.axvline(dd50, color='#fbbf24', linewidth=2, linestyle='-',
                label=f'Median MaxDD: {dd50:.1f}%')
    ax2.axvline(dd95, color='#ef4444', linewidth=2, linestyle='--',
                label=f'95. Perzentil MaxDD: {dd95:.1f}%')
    ax2.set_xlabel('Maximaler Drawdown (%)')
    ax2.set_ylabel('Häufigkeit')
    ax2.set_title('Verteilung der Max Drawdowns', color='white')
    ax2.legend(fontsize=9, facecolor='#1e293b', labelcolor='white', framealpha=0.5)

    ruin = sum(1 for e in final_equities if e < capital * 0.5) / n_sims * 100
    fig.suptitle(
        f'dnabot Monte Carlo | {n_sims:,} Simulationen | {n_trades} Trades | '
        f'Risk/Trade: {risk_pct}% | Ruin-Wahrsch. (<50%): {ruin:.1f}%',
        color='white', fontsize=11
    )
    plt.tight_layout()
    path = '/tmp/dnabot_monte_carlo.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'monte_carlo_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='dnabot Monte Carlo Simulation')
    parser.add_argument('--simulations', type=int,   default=10000)
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--risk',        type=float, default=2.5)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print(f"  dnabot — Monte Carlo Simulation")
    print(f"{'=' * 60}")
    print(f"  Simulationen: {args.simulations:,}")
    print(f"  Startkapital: {args.capital} USDT | Risk/Trade: {args.risk}%")
    print()

    print("  Lade Trades...", end='', flush=True)
    trades = load_trades()
    if not trades:
        print(f"\n  {R}Keine Backtest-Daten. Erst show_results.sh → Mode 1.{NC}\n")
        sys.exit(1)

    # Trade-Outcomes extrahieren
    trade_outcomes = [
        (t.get('outcome', 'LOSS'), t.get('sl_pct', 1.0), t.get('pnl_pct', 0.0))
        for t in trades
    ]
    wins     = sum(1 for t in trade_outcomes if t[0] == 'WIN')
    losses   = sum(1 for t in trade_outcomes if t[0] == 'LOSS')
    timeouts = sum(1 for t in trade_outcomes if t[0] == 'TIMEOUT')
    n = len(trade_outcomes)
    print(f" {n} Trades | WR {wins/n*100:.1f}% | "
          f"SL {losses/n*100:.1f}% | Timeout {timeouts/n*100:.1f}%")

    # Simulation
    print(f"  Simuliere {args.simulations:,} Pfade...", end='', flush=True)
    random.seed(42)
    final_equities = []
    max_dds        = []
    for _ in range(args.simulations):
        eq, dd = simulate_path(trade_outcomes, args.capital, args.risk)
        final_equities.append(eq)
        max_dds.append(dd)
    print(" fertig.")
    print()

    # Statistiken
    import statistics
    pnl_pcts = [(e - args.capital) / args.capital * 100 for e in final_equities]
    pnl_pcts.sort()
    max_dds.sort()

    p5  = pnl_pcts[int(0.05 * len(pnl_pcts))]
    p25 = pnl_pcts[int(0.25 * len(pnl_pcts))]
    p50 = pnl_pcts[int(0.50 * len(pnl_pcts))]
    p75 = pnl_pcts[int(0.75 * len(pnl_pcts))]
    p95 = pnl_pcts[int(0.95 * len(pnl_pcts))]

    dd50 = max_dds[int(0.50 * len(max_dds))]
    dd95 = max_dds[int(0.95 * len(max_dds))]

    ruin       = sum(1 for e in final_equities if e < args.capital * 0.5) / args.simulations * 100
    profitable = sum(1 for p in pnl_pcts if p > 0) / args.simulations * 100

    print(f"  {'─' * 50}")
    print(f"  PnL% Verteilung ({args.simulations:,} Simulationen):")
    print(f"  {'5. Perzentil (schlechteste 5%):':<36} {p5:>+8.1f}%")
    print(f"  {'25. Perzentil:':<36} {p25:>+8.1f}%")
    print(f"  {'Median (50. Perzentil):':<36} {p50:>+8.1f}%")
    print(f"  {'75. Perzentil:':<36} {p75:>+8.1f}%")
    print(f"  {'95. Perzentil (beste 5%):':<36} {p95:>+8.1f}%")
    print(f"  {'─' * 50}")
    print(f"  {'Max Drawdown Median:':<36} {dd50:>8.1f}%")
    print(f"  {'Max Drawdown 95. Perzentil:':<36} {dd95:>8.1f}%")
    print(f"  {'─' * 50}")
    col_ruin = R if ruin > 10 else (Y if ruin > 2 else G)
    col_prof = G if profitable > 70 else (Y if profitable > 50 else R)
    print(f"  {'Ruin-Wahrscheinlichkeit (<50%):':36} {col_ruin}{ruin:>8.1f}%{NC}")
    print(f"  {'Profitabel-Wahrscheinlichkeit:':<36} {col_prof}{profitable:>8.1f}%{NC}")
    print(f"  {'─' * 50}")

    # Interpretation
    print()
    if ruin < 1:
        print(f"  {G}✓ Sehr geringes Ruin-Risiko (<1%). Strategie ist robust.{NC}")
    elif ruin < 5:
        print(f"  {Y}⚠ Moderates Ruin-Risiko ({ruin:.1f}%). Risk% prüfen.{NC}")
    else:
        print(f"  {R}✗ Hohes Ruin-Risiko ({ruin:.1f}%). Risk% reduzieren!{NC}")

    # Chart
    print()
    path = create_chart(final_equities, max_dds, args.capital, args.risk,
                        args.simulations, n)
    if path:
        print(f"  {G}✓ Chart gespeichert: {path}{NC}")
        if not args.no_telegram:
            token, chat_id = get_telegram_credentials()
            if token:
                caption = (
                    f"dnabot Monte Carlo | {args.simulations:,} Simulationen\n"
                    f"{n} Trades | WR {wins/n*100:.1f}% | Risk/Trade: {args.risk}%\n\n"
                    f"5. Perz.:  {p5:+.0f}%\n"
                    f"Median:    {p50:+.0f}%\n"
                    f"95. Perz.: {p95:+.0f}%\n"
                    f"MaxDD 95%: {dd95:.1f}%\n"
                    f"Ruin-Wahrsch.: {ruin:.1f}%"
                )
                send_telegram_photo(token, chat_id, path, caption)
                print(f"  {G}✓ Via Telegram gesendet.{NC}")

    print(f"\n  {G}Monte Carlo abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
