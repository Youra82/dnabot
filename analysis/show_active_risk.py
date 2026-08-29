#!/usr/bin/env python3
# analysis/show_active_risk.py
# Kompakter Report NUR fuer die aktuell aktiven Paare in settings.json::
# active_strategies -- im Unterschied zu show_risk_genes.py (zeigt ALLE
# Paare in der DB inkl. Kandidaten). Trennt bewusst zwei Risiko-Quellen:
#   Live-Risiko% : tatsaechlich verwendete Positionsgroesse (zentral, aus
#                  risk_overrides/risk_settings -- siehe run_portfolio_
#                  optimizer_momentum_exit.py, gilt einheitlich fuer alle
#                  Strategien)
#   Gen RR/Trail : SL/TP-Mechanik, kommt weiterhin aus dem aktiven Risiko-
#                  Gen und ist pro Paar individuell (siehe trade_manager.py::
#                  full_trade_cycle())

import os
import sys
import json

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from dnabot.genome.risk_genome_db import RiskGenomeDB
from dnabot.utils.strategy_overrides import find_strategy_overrides

SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
RISK_DB_PATH  = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_genome.db')


def main():
    with open(SETTINGS_PATH, encoding='utf-8') as f:
        settings = json.load(f)
    strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    if not strategies:
        print("\nKeine active_strategies in settings.json.\n")
        return

    global_risk_pct = settings.get('risk_settings', {}).get('risk_per_entry_pct', 1.0)

    db = None
    if os.path.exists(RISK_DB_PATH):
        db = RiskGenomeDB(RISK_DB_PATH)

    print(f"\n  {'Symbol':<20} {'TF':<5} {'Live-Risk%':>10} {'Gen-RR':>7} {'Gen-Trail%':>10} {'Gen-SeqLen':>10}  {'Aktiv':>6}")
    print("  " + "-" * 78)
    for s in strategies:
        sym, tf = s.get('symbol', ''), s.get('timeframe', '')
        active_flag = 'ja' if s.get('active') else 'NEIN'

        overrides = find_strategy_overrides(sym, tf, settings)
        live_risk_pct = overrides['risk'].get('risk_per_entry_pct', global_risk_pct)

        gene = db.get_active_gene(sym, tf) if db else None
        if gene:
            print(f"  {sym:<20} {tf:<5} {live_risk_pct:>9.2f}% {gene['rr_ratio']:>7} "
                  f"{gene['trailing_pct']:>9}% {gene['seq_len']:>10}  {active_flag:>6}")
        else:
            print(f"  {sym:<20} {tf:<5} {live_risk_pct:>9.2f}% {'kein aktives Gen':>30}  {active_flag:>6}")

    if db:
        db.close()
    print(f"\n  Live-Risk% gilt zentral fuer alle Strategien (settings.json::risk_settings."
          f"risk_per_entry_pct = {global_risk_pct}%, sofern kein Pair-eigenes risk_overrides gesetzt ist).\n")


if __name__ == '__main__':
    main()
