#!/usr/bin/env python3
"""
analysis/fee_impact_momentum_exit.py — Gebuehren-Impact-Analyse NUR fuer die
momentum_exit-Strategie (Fund AQ/AR), nicht den gesamten Backtest-Ergebnis-
Pool wie analysis/fee_impact.py (der mischt alte Genom-Trades mit den neuen
momentum_exit-Trades in einer Kurve -- irrefuehrend, da die alten,
ueberwiegend verlustreichen Genom-Trades die gute momentum_exit-Performance
in der gepoolten Kurve ertraenken). Wiederverwendet simulate_with_fees()/
create_chart() aus fee_impact.py 1:1, filtert beim Laden nur auf
'*_momentum_exit.json'-Ergebnisdateien.
"""
import os
import sys
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fee_impact import (
    simulate_with_fees, create_chart, get_telegram_credentials,
    send_telegram_photo, RESULTS_DIR, FEE_LEVELS, SLIPPAGE_LEVELS, G, Y, R, NC,
)


def load_momentum_exit_trades():
    trades = []
    if not os.path.isdir(RESULTS_DIR):
        return trades
    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('_momentum_exit.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                data = json.load(f)
            for t in data.get('trades', []):
                t['market'] = data['market']
                t['timeframe'] = data['timeframe']
                trades.append(t)
        except Exception:
            continue
    trades.sort(key=lambda t: str(t.get('entry_time', '')))
    return trades


def main():
    import argparse
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
    print(f" {len(trades)} Trades geladen (nur momentum_exit, kein Pool mit alten Genom-Trades).\n")

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
