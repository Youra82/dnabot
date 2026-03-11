#!/usr/bin/env python3
# run_portfolio_optimizer.py
# Automatische Portfolio-Optimierung (jaegerbot-Style):
#
# Kapital-Modell:
#   - EIN gemeinsamer Kapital-Pool für alle Pairs
#   - Alle Trades laufen chronologisch auf demselben Kapital
#   - Jeder Trade riskiert risk_pct% des AKTUELLEN Equity
#   - Mehr profitable Trades = mehr Kompoundierung = höherer PnL
#   - Nebenläufige Trades werden nacheinander auf das gleiche Equity angewandt
#
# Optimizer:
#   - Exhaustive Suche: testet alle möglichen Pair-Kombinationen
#   - Constraint: max. 1 TF pro Coin (Bitget: 1 Position pro Symbol)
#   - Stoppt wenn keine weitere Verbesserung durch mehr Coins möglich ist

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from itertools import combinations

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

G   = '\033[0;32m'
Y   = '\033[1;33m'
R   = '\033[0;31m'
C   = '\033[0;36m'
B   = '\033[1;37m'
NC  = '\033[0m'

RR_RATIO = 2.0


def coin_from_symbol(symbol: str) -> str:
    return symbol.split('/')[0].upper()


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
            'coin':      coin_from_symbol(data['market']),
            'trades':    trades,
            'stats':     data.get('stats', {}),
        })

    return results


def simulate_portfolio(pair_results: list, capital: float, risk_pct: float) -> dict:
    """
    Simuliert ein Portfolio mit GEMEINSAMEM Kapital-Pool.

    Alle Trades aller Pairs werden chronologisch zusammengeführt.
    Jeder Trade riskiert risk_pct% des aktuellen Equity (Kompoundierung).
    Das ermöglicht höhere PnL als Einzel-Pairs durch mehr Trades.
    """
    if not pair_results:
        return {
            'total_pnl_pct': 0.0, 'final_equity': capital,
            'max_dd': 0.0, 'n_trades': 0, 'win_rate': 0.0,
        }

    # Alle Trades zusammenführen und chronologisch sortieren
    all_trades = []
    for pr in pair_results:
        for t in pr['trades']:
            all_trades.append({
                'market':    pr['market'],
                'timeframe': pr['timeframe'],
                'outcome':   t.get('outcome', 'LOSS'),
                'pnl_pct':   t.get('pnl_pct', 0.0),
                'sl_pct':    t.get('sl_pct', 1.0),
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
        else:  # TIMEOUT
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


def compute_filtered_stats(trades: list, capital: float, risk_pct: float) -> dict:
    """Berechnet Einzel-Statistiken aus gefilterten Trades (shared-capital Modell)."""
    return simulate_portfolio(
        [{'market': '', 'timeframe': '', 'trades': trades}],
        capital, risk_pct
    )


def best_portfolio_for_size(candidates: list, team_size: int, capital: float,
                             risk_pct: float, max_dd_limit: float,
                             force_coins: set = None) -> tuple:
    """
    Findet das beste Portfolio mit genau team_size Pairs (exhaustive).
    Constraint: max. 1 TF pro Coin.
    Returns: (best_metrics, best_combo) or (None, None)
    """
    # Nur 1 TF pro Coin zulassen: besten TF pro Coin vorausfiltern
    coin_map = {}
    for pair in candidates:
        coin = pair['coin']
        pnl  = pair['filtered_stats']['total_pnl_pct']
        if pnl <= 0:
            continue
        if coin not in coin_map or pnl > coin_map[coin]['filtered_stats']['total_pnl_pct']:
            coin_map[coin] = pair

    eligible = list(coin_map.values())
    if len(eligible) < team_size:
        return None, None

    best_metrics = None
    best_combo   = None

    for combo in combinations(eligible, team_size):
        metrics = simulate_portfolio(list(combo), capital, risk_pct)
        if metrics['max_dd'] > max_dd_limit:
            continue
        if best_metrics is None or metrics['total_pnl_pct'] > best_metrics['total_pnl_pct']:
            best_metrics = metrics
            best_combo   = list(combo)

    return best_metrics, best_combo


def optimize_portfolio(candidates: list, capital: float, risk_pct: float,
                        max_dd_limit: float) -> tuple:
    """
    Findet das optimale Portfolio durch schrittweise Team-Größen-Suche.
    Stoppt wenn keine weitere Verbesserung mehr möglich ist.
    """
    # Gefilterte Einzel-Stats berechnen
    for r in candidates:
        r['filtered_stats'] = compute_filtered_stats(r['trades'], capital, risk_pct)

    best_overall_metrics = None
    best_overall_combo   = None
    max_possible_size    = len(set(r['coin'] for r in candidates
                                   if r['filtered_stats']['total_pnl_pct'] > 0))

    for team_size in range(1, max_possible_size + 1):
        n_combos = 1
        # Berechne Anzahl Kombinationen für Progress-Anzeige
        eligible_count = len([r for r in candidates
                               if r['filtered_stats']['total_pnl_pct'] > 0])
        from math import comb as math_comb
        n_combos = math_comb(min(eligible_count, 7), team_size)

        print(f"  Teste Teams mit {team_size} Coin(s) ({n_combos} Kombinationen) ...",
              end='', flush=True)

        metrics, combo = best_portfolio_for_size(
            candidates, team_size, capital, risk_pct, max_dd_limit
        )

        if combo is None:
            print(f" {Y}kein gültiges Team (MaxDD-Limit zu eng){NC}")
            break

        print(f" Bestes PnL: {G}+{metrics['total_pnl_pct']:.1f}%{NC}  MaxDD: {metrics['max_dd']:.1f}%")

        if best_overall_metrics is None or metrics['total_pnl_pct'] > best_overall_metrics['total_pnl_pct']:
            best_overall_metrics = metrics
            best_overall_combo   = combo
        else:
            print(f"\n  {Y}Keine weitere Verbesserung durch mehr Coins. Optimierung beendet.{NC}")
            break

    return best_overall_metrics, best_overall_combo


def print_result(selected: list, pm: dict, capital: float, risk_pct: float,
                 max_dd_limit: float):
    w = 72
    print(f"\n{'=' * w}")
    print(f"{B}  dnabot — Automatische Portfolio-Optimierung{NC}")
    print(f"  Ziel: Maximaler Profit bei maximal {max_dd_limit:.1f}% Drawdown.")
    print(f"{'=' * w}")

    if not selected:
        print(f"\n{R}  Kein Portfolio gefunden, das die Bedingungen erfüllt.{NC}")
        print(f"  → Erhöhe das Max-Drawdown-Limit oder führe zuerst Mode 1 aus.\n")
        return

    print(f"\n  {G}Optimales Portfolio — {len(selected)} Coin(s){NC}")
    print(f"  Kapital: {capital:.0f} USDT | Risiko/Trade: {risk_pct}% (gemeinsamer Pool)")
    print(f"\n  {'Markt':<24} {'TF':<6} {'Trades':>7} {'WR':>7} {'PnL%':>9} {'MaxDD':>8}")
    print(f"  {'-' * (w - 2)}")

    for pr in sorted(selected, key=lambda x: x['filtered_stats']['total_pnl_pct'], reverse=True):
        st  = pr['filtered_stats']
        pnl_col = G if st['total_pnl_pct'] > 0 else R
        wr_col  = G if st['win_rate'] >= 0.50 else (Y if st['win_rate'] >= 0.43 else R)
        sign = '+' if st['total_pnl_pct'] >= 0 else ''
        print(
            f"  {pr['market']:<24} {pr['timeframe']:<6} {st['n_trades']:>7} "
            f"{wr_col}{st['win_rate']:>6.1%}{NC} "
            f"{pnl_col}{sign}{st['total_pnl_pct']:>7.1f}%{NC} "
            f"{st['max_dd']:>7.1f}%"
        )

    pnl_col = G if pm['total_pnl_pct'] > 0 else R
    print(f"\n  {'─' * (w - 2)}")
    print(f"  {B}Portfolio gesamt (gemeinsamer Kapital-Pool, alle Trades kompoundiert):{NC}")
    print(f"  Trades total:  {pm['n_trades']}")
    print(f"  Win-Rate:      {pm['win_rate']:.1%}")
    print(f"  PnL:           {pnl_col}{'+' if pm['total_pnl_pct'] >= 0 else ''}{pm['total_pnl_pct']:.1f}%{NC}")
    print(f"  Final Equity:  {pm['final_equity']:.2f} USDT")
    print(f"  Max Drawdown:  {pm['max_dd']:.1f}%")
    print(f"{'=' * w}\n")


def generate_charts_for_portfolio(selected: list, start_date: str, end_date: str,
                                   capital: float, risk_pct: float):
    """Erstellt interaktive Charts für alle Pairs im Portfolio und sendet sie via Telegram."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    try:
        import pandas as pd
    except ImportError:
        print(f"{R}  pandas nicht installiert — Charts übersprungen.{NC}")
        return

    # Settings + Secrets laden
    try:
        with open(SETTINGS_PATH) as f:
            settings = _json.load(f)
    except Exception as e:
        print(f"{R}  settings.json nicht lesbar: {e}{NC}")
        return

    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    try:
        with open(secret_path) as f:
            secrets = _json.load(f)
    except Exception as e:
        print(f"{R}  secret.json nicht lesbar: {e}{NC}")
        return

    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))
    try:
        from dnabot.utils.exchange import Exchange
        from dnabot.genome.database import GenomeDB
        from dnabot.analysis.backtester import run_backtest
        from dnabot.analysis.interactive_chart import create_chart
        from scan_and_learn import resolve_history_days
    except Exception as e:
        print(f"{R}  Import-Fehler: {e}{NC}")
        return

    accounts = secrets.get('dnabot', [])
    if not accounts:
        print(f"{R}  Kein 'dnabot'-Account in secret.json.{NC}")
        return
    exchange = Exchange(accounts[0])

    scan_cfg   = settings.get('scan_settings', {})
    genome_cfg = settings.get('genome_settings', {})
    risk_cfg   = settings.get('risk_settings', {})
    db_path    = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')

    params = {
        'genome': {
            'min_score':        genome_cfg.get('min_score', 0.08),
            'min_winrate':      genome_cfg.get('min_winrate', 0.45),
            'sequence_lengths': genome_cfg.get('sequence_lengths', [4, 5, 6]),
        },
        'risk': {'rr_ratio': risk_cfg.get('rr_ratio', 2.0)},
    }

    # Telegram-Credentials
    tg          = accounts[0] if accounts else {}
    bot_token   = tg.get('telegram_bot_token', '') or secrets.get('telegram', {}).get('bot_token', '')
    chat_id     = tg.get('telegram_chat_id', '')   or secrets.get('telegram', {}).get('chat_id', '')
    send_tg     = bool(bot_token and chat_id)

    generated = []
    print()

    for pr in selected:
        symbol    = pr['market']
        timeframe = pr['timeframe']
        print(f"  Erstelle Chart: {symbol} ({timeframe}) ...", end='', flush=True)

        history_days = resolve_history_days(timeframe, scan_cfg.get('history_days'))
        fetch_end    = datetime.now(timezone.utc)
        fetch_start  = fetch_end - timedelta(days=history_days)

        df = exchange.fetch_historical_ohlcv(
            symbol, timeframe,
            fetch_start.strftime('%Y-%m-%d'),
            fetch_end.strftime('%Y-%m-%d'),
        )
        if df is None or df.empty:
            print(f" {Y}keine Daten.{NC}")
            continue

        db      = GenomeDB(db_path)
        results = run_backtest(
            df=df, market=symbol, timeframe=timeframe, db=db,
            params=params, start_capital=capital,
            risk_per_trade_pct=risk_pct,
        )
        db.close()

        trades      = results.get('trades', [])
        stats       = results.get('stats', {})
        trades_filt = trades

        if start_date:
            trades_filt = [t for t in trades_filt
                           if str(t.get('entry_time', '')) >= start_date]
        if end_date:
            trades_filt = [t for t in trades_filt
                           if str(t.get('entry_time', '')) <= end_date + ' 23:59:59']

        df_chart = df.copy()
        if start_date:
            try:
                df_chart = df_chart[df_chart.index >= pd.Timestamp(start_date, tz='UTC')]
            except Exception:
                pass
        if end_date:
            try:
                df_chart = df_chart[df_chart.index <= pd.Timestamp(end_date + ' 23:59:59', tz='UTC')]
            except Exception:
                pass

        fig = create_chart(
            symbol, timeframe, df_chart, trades_filt, stats, capital,
            risk_pct=risk_pct,
            rr_ratio=risk_cfg.get('rr_ratio', 2.0),
        )
        if fig is None:
            print(f" {R}Fehler beim Erstellen.{NC}")
            continue

        safe_name   = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
        output_file = f"/tmp/dnabot_{safe_name}.html"
        fig.write_html(output_file)
        generated.append((symbol, timeframe, output_file))
        print(f" {G}✓{NC}")

    print(f"\n  {G}{len(generated)} Chart(s) erstellt.{NC}")

    if send_tg and generated:
        from dnabot.utils.telegram import send_document
        print(f"  Sende via Telegram ...")
        for sym, tf, path in generated:
            send_document(bot_token, chat_id, path, caption=f"dnabot Portfolio-Chart: {sym} {tf}")
            print(f"  {G}✓ {sym} {tf} gesendet.{NC}")
    elif generated:
        print(f"  {Y}Telegram nicht konfiguriert — Charts nur lokal in /tmp/ gespeichert.{NC}")


def write_to_settings(selected: list):
    try:
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"{R}Fehler beim Lesen von settings.json: {e}{NC}")
        return False

    new_strategies = [
        {"symbol": pr['market'], "timeframe": pr['timeframe'], "active": True}
        for pr in selected
    ]
    settings.setdefault('live_trading_settings', {})['active_strategies'] = new_strategies

    try:
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(settings, f, indent=2)
        print(f"\n{G}✓ settings.json aktualisiert — {len(new_strategies)} Strategie(n) eingetragen.{NC}\n")
        return True
    except Exception as e:
        print(f"{R}Fehler beim Schreiben von settings.json: {e}{NC}")
        return False


def main():
    parser = argparse.ArgumentParser(description="dnabot Portfolio Optimizer")
    parser.add_argument('--capital',    type=float, default=1000.0)
    parser.add_argument('--risk',       type=float, default=1.0)
    parser.add_argument('--max-dd',     type=float, default=30.0)
    parser.add_argument('--start-date', type=str,   default=None)
    parser.add_argument('--end-date',   type=str,   default=None)
    parser.add_argument('--auto-write', action='store_true')
    args = parser.parse_args()

    date_range = ""
    if args.start_date or args.end_date:
        date_range = f" | {args.start_date or '...'} → {args.end_date or 'heute'}"

    print(f"\n{'─' * 72}")
    print(f"{B}  dnabot Automatische Portfolio-Optimierung{NC}")
    print(f"  Ziel: Maximaler Profit bei maximal {args.max_dd:.1f}% Drawdown.{date_range}")
    print(f"  Modell: Gemeinsamer Kapital-Pool — alle Trades kompoundieren zusammen")
    print(f"  Constraint: max. 1 Timeframe pro Coin (Bitget-Regel)")
    print(f"{'─' * 72}\n")

    print("  Lade Backtest-Ergebnisse ...", end='', flush=True)
    all_results = load_all_results(args.start_date, args.end_date)
    if not all_results:
        print(f"\n{R}  Keine Backtest-Ergebnisse gefunden.{NC}")
        print("  Zuerst Mode 1 (Einzel-Backtest) ausführen!\n")
        sys.exit(1)

    with_trades = [r for r in all_results if len(r['trades']) > 0]
    coins = sorted(set(r['coin'] for r in with_trades))
    print(f" {len(all_results)} Dateien, {len(with_trades)} mit Trades, {len(coins)} Coins.")
    print(f"\n  Optimiere Portfolio...\n")

    best_metrics, best_combo = optimize_portfolio(
        with_trades, args.capital, args.risk, args.max_dd
    )

    if not best_combo:
        best_metrics = {'total_pnl_pct': 0, 'final_equity': args.capital,
                        'max_dd': 0, 'n_trades': 0, 'win_rate': 0}
        best_combo = []

    print_result(best_combo, best_metrics, args.capital, args.risk, args.max_dd)

    if not best_combo:
        sys.exit(0)

    if args.auto_write:
        write_to_settings(best_combo)
    else:
        try:
            ans = input("  Sollen die optimalen Ergebnisse automatisch in settings.json eingetragen werden? (j/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans in ('j', 'ja', 'y', 'yes'):
            write_to_settings(best_combo)
        else:
            print(f"\n{Y}  settings.json wurde NICHT geändert.{NC}\n")

    # Charts für das Portfolio anbieten
    try:
        chart_ans = input("  Interaktive Charts für diese Zusammenstellung erstellen & via Telegram senden? (j/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        chart_ans = 'n'
    if chart_ans in ('j', 'ja', 'y', 'yes'):
        generate_charts_for_portfolio(
            best_combo, args.start_date, args.end_date, args.capital, args.risk
        )


if __name__ == '__main__':
    main()
