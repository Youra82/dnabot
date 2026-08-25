#!/usr/bin/env python3
"""
analysis/regime_analysis_momentum_exit.py — Regime Performance Analysis (momentum_exit)

Zeigt, in welchen Marktregimen (TREND/RANGE/HIGH_VOL/NEUTRAL) momentum_exit
pro Coin gut/schlecht performt.

WICHTIG, anders als beim fruaheren Genome-System (analysis/regime_analysis.py,
beim Cleanup 2026-08-24 entfernt): momentum_exit hat KEINEN Regime-Filter und
speichert kein Regime pro Trade (siehe momentum_exit_logic.py-Docstring --
bewusste Design-Entscheidung, der validierte Research-Code hatte auch keinen
Regime-Filter). Diese Analyse berechnet das Regime deshalb UNABHAENGIG per
ADX/ATR aus frisch geladenen OHLCV-Daten (dieselbe Formel wie das fruahere
genome/regime.py) und matcht es nachtraeglich gegen die Entry-Zeit jedes
gespeicherten Trades -- eine rein deskriptive Auswertung ("performt der Bot
in bestimmten Regimen anders, obwohl er nicht danach filtert?"), kein Live-
Verhalten.

Ausfuehrung:
  python3 analysis/regime_analysis_momentum_exit.py
  python3 analysis/regime_analysis_momentum_exit.py --min-samples 10
"""
import os
import sys
import argparse
from collections import defaultdict

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))
from momentum_exit_utils import (
    load_trades, style_axes, save_send, PROJECT_ROOT, G, Y, R, NC,
)

REGIMES = ['TREND', 'RANGE', 'HIGH_VOL', 'NEUTRAL']

ADX_PERIOD          = 14
ATR_PERIOD           = 14
ATR_MA_PERIOD        = 50
ADX_TREND_THRESHOLD  = 25.0
ADX_RANGE_THRESHOLD  = 20.0
ATR_SPIKE_FACTOR     = 1.5


def classify_regimes(df: pd.DataFrame) -> pd.Series:
    """Vektorisierte Regime-Klassifikation fuer JEDE Kerze (nicht nur die
    letzte) -- gleiche Formel/Schwellenwerte wie das fruahere genome/
    regime.py::detect_regime(), hier auf die ganze Historie angewendet
    statt nur den aktuellen Wert."""
    import ta
    atr_series = ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'],
        window=ATR_PERIOD, fillna=True
    ).average_true_range()
    atr_ma = atr_series.rolling(window=ATR_MA_PERIOD, min_periods=10).mean()
    adx_series = ta.trend.ADXIndicator(
        high=df['high'], low=df['low'], close=df['close'],
        window=ADX_PERIOD, fillna=True
    ).adx()

    atr_ratio = (atr_series / atr_ma.replace(0, pd.NA)).fillna(1.0)

    regime = pd.Series('NEUTRAL', index=df.index)
    regime[atr_ratio >= ATR_SPIKE_FACTOR] = 'HIGH_VOL'
    trend_mask = (adx_series >= ADX_TREND_THRESHOLD) & (atr_ratio < ATR_SPIKE_FACTOR)
    range_mask = (adx_series <= ADX_RANGE_THRESHOLD) & (atr_ratio < ATR_SPIKE_FACTOR)
    regime[trend_mask] = 'TREND'
    regime[range_mask] = 'RANGE'
    return regime


def main():
    parser = argparse.ArgumentParser(description='dnabot Regime Performance Analysis (momentum_exit)')
    parser.add_argument('--min-samples', type=int, default=10)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    print(f"\n{'=' * 60}\n  dnabot — Regime Performance Analysis (momentum_exit)\n{'=' * 60}")

    pair_results = load_trades()
    if not pair_results:
        print(f"  {R}Keine momentum_exit-Backtest-Daten. Erst ./run_momentum_exit_pipeline.sh ausführen.{NC}")
        sys.exit(1)

    from dnabot.utils.exchange import Exchange
    from dnabot.utils.config_loader import HISTORY_DAYS_MAP
    import json
    from datetime import datetime, timezone, timedelta

    with open(os.path.join(PROJECT_ROOT, 'secret.json'), encoding='utf-8') as f:
        secrets = json.load(f)
    accounts = secrets.get('dnabot', [])
    if not accounts:
        print(f"  {R}Kein 'dnabot'-Account in secret.json.{NC}")
        sys.exit(1)
    exchange = Exchange(accounts[0])

    market_regime = defaultdict(lambda: {r: {'wins': 0, 'occ': 0, 'pnl': 0.0} for r in REGIMES})

    for pr in pair_results:
        market, timeframe = pr['market'], pr['timeframe']
        print(f"  {market} ({timeframe}): lade OHLCV + klassifiziere Regime...", end='', flush=True)
        history_days = HISTORY_DAYS_MAP.get(timeframe, 730)
        fetch_end   = datetime.now(timezone.utc)
        fetch_start = fetch_end - timedelta(days=history_days)
        df = exchange.fetch_historical_ohlcv(
            market, timeframe, fetch_start.strftime('%Y-%m-%d'), fetch_end.strftime('%Y-%m-%d'),
        )
        if df is None or df.empty:
            print(f" {Y}keine Daten, übersprungen.{NC}")
            continue
        try:
            regime_series = classify_regimes(df)
        except ImportError:
            print(f"\n  {R}'ta' nicht installiert -- pip install ta{NC}")
            sys.exit(1)

        matched = 0
        key = f"{market.split('/')[0]}/{timeframe}"
        for t in pr['trades']:
            try:
                reg = regime_series.asof(pd.Timestamp(t['entry_dt']))
            except Exception:
                continue
            if pd.isna(reg):
                continue
            matched += 1
            d = market_regime[key][reg]
            d['occ']  += 1
            d['pnl']  += t.get('pnl_pct', 0.0)
            if t.get('outcome') == 'WIN':
                d['wins'] += 1
        print(f" {matched}/{len(pr['trades'])} Trades zugeordnet.")

    if not market_regime:
        print(f"  {R}Keine Regime-Daten ermittelt.{NC}")
        sys.exit(1)

    markets = sorted(market_regime.keys())
    wr_matrix = []
    for m in markets:
        row = []
        for reg in REGIMES:
            d = market_regime[m][reg]
            row.append(d['wins'] / d['occ'] if d['occ'] >= args.min_samples else float('nan'))
        wr_matrix.append(row)

    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, max(6, len(markets) * 0.4 + 2)))
    fig.patch.set_facecolor('#0f172a')
    style_axes(ax1, ax2)

    mat = np.array(wr_matrix, dtype=float)
    masked = np.ma.masked_invalid(mat)
    im = ax1.imshow(masked, cmap='RdYlGn', vmin=0.15, vmax=0.45, aspect='auto')
    ax1.set_xticks(range(len(REGIMES))); ax1.set_xticklabels(REGIMES, color='white')
    ax1.set_yticks(range(len(markets))); ax1.set_yticklabels(markets, fontsize=8, color='white')
    plt.colorbar(im, ax=ax1, label='Win-Rate')
    for i in range(len(markets)):
        for j in range(len(REGIMES)):
            if not np.isnan(mat[i, j]):
                ax1.text(j, i, f'{mat[i,j]:.0%}', ha='center', va='center',
                         color='black' if 0.15 < mat[i, j] < 0.45 else 'white', fontsize=7)
    ax1.set_title(f'Win-Rate Heatmap: Coin × Regime (min. {args.min_samples} Trades)\n'
                  '(grau=zu wenig Daten -- momentum_exit filtert NICHT nach Regime)')

    avg_by_regime = {}
    for j, reg in enumerate(REGIMES):
        vals = [mat[i, j] for i in range(len(markets)) if not np.isnan(mat[i, j])]
        avg_by_regime[reg] = sum(vals) / len(vals) if vals else 0

    ax2.bar(REGIMES, [avg_by_regime[r] for r in REGIMES],
            color=['#16a34a', '#f59e0b', '#ef4444', '#2563eb'], alpha=0.8)
    for i, (reg, val) in enumerate(avg_by_regime.items()):
        ax2.text(i, val + 0.005, f'{val:.1%}', ha='center', va='bottom', color='white', fontsize=11)
    ax2.set_xlabel('Markt-Regime')
    ax2.set_ylabel('Durchschnittliche Win-Rate')
    ax2.set_title('Ø Win-Rate pro Regime (alle Coins)')
    ax2.set_ylim(0, max(avg_by_regime.values(), default=0.1) * 1.3 or 0.1)

    fig.suptitle(f'dnabot momentum_exit Regime Performance | {len(markets)} Pairs | '
                 f'min_samples={args.min_samples}', color='white', fontsize=11)
    plt.tight_layout()

    best_regime = max(avg_by_regime, key=avg_by_regime.get)
    caption = (f"dnabot momentum_exit Regime Performance Analysis\n"
               f"{len(markets)} Pairs | min_samples={args.min_samples}\n\n"
               f"Ø Win-Rate pro Regime:\n"
               + "\n".join(f"  {reg}: {avg_by_regime[reg]:.1%}" for reg in REGIMES)
               + f"\n\nBestes Regime: {best_regime} ({avg_by_regime[best_regime]:.1%})"
               + f"\n\nHinweis: momentum_exit filtert NICHT nach Regime (bewusste Design-"
                 f"Entscheidung) -- diese Auswertung ist rein deskriptiv, kein Live-Filter.")
    save_send(fig, 'regime_analysis', caption, args.no_telegram)
    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
