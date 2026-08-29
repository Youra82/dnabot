#!/usr/bin/env python3
# analysis/show_active_risk.py
# Kompakter Report: Risiko-Gen-Parameter (v.a. risk_pct) NUR fuer die aktuell
# aktiven Paare in settings.json::active_strategies -- im Unterschied zu
# show_risk_genes.py, das ALLE Paare in der DB inkl. Kandidaten zeigt.

import os
import sys
import json

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from dnabot.genome.risk_genome_db import RiskGenomeDB

SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
RISK_DB_PATH  = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_genome.db')


def main():
    with open(SETTINGS_PATH, encoding='utf-8') as f:
        settings = json.load(f)
    strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    if not strategies:
        print("\nKeine active_strategies in settings.json.\n")
        return

    if not os.path.exists(RISK_DB_PATH):
        print("\nKeine Risiko-Gen-Datenbank gefunden -- erst risk_genome_discover.py laufen lassen.\n")
        return

    db = RiskGenomeDB(RISK_DB_PATH)
    print(f"\n  {'Symbol':<20} {'TF':<5} {'Risk%':>7} {'RR':>6} {'Trail%':>7} {'SeqLen':>7}  {'Aktiv':>6}")
    print("  " + "-" * 66)
    for s in strategies:
        sym, tf = s.get('symbol', ''), s.get('timeframe', '')
        active_flag = 'ja' if s.get('active') else 'NEIN'
        gene = db.get_active_gene(sym, tf)
        if gene:
            print(f"  {sym:<20} {tf:<5} {gene['risk_pct']:>6.2f}% {gene['rr_ratio']:>6} "
                  f"{gene['trailing_pct']:>6}% {gene['seq_len']:>7}  {active_flag:>6}")
        else:
            print(f"  {sym:<20} {tf:<5} {'kein aktives Gen':>28}  {active_flag:>6}")
    db.close()
    print()


if __name__ == '__main__':
    main()
