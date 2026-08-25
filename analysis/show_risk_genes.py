#!/usr/bin/env python3
# analysis/show_risk_genes.py
# Report ueber die Risiko-Gen-Datenbank (momentum_exit-Strategie): zeigt pro
# Pair/Timeframe das aktive Gen (falls vorhanden) und die Top-Kandidaten.

import os
import sys

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from dnabot.genome.risk_genome_db import RiskGenomeDB

RISK_DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_genome.db')


def main():
    if not os.path.exists(RISK_DB_PATH):
        print("\nKeine Risiko-Gen-Datenbank gefunden -- erst risk_genome_discover.py laufen lassen.\n")
        return

    db = RiskGenomeDB(RISK_DB_PATH)
    pairs = db.get_all_market_pairs()

    print("\n" + "=" * 78)
    print("  RISIKO-GEN-DATENBANK (momentum_exit) — Report")
    print("=" * 78)

    if not pairs:
        print("  Keine Paare in der Datenbank.")
        db.close()
        return

    for market, timeframe in pairs:
        candidates = db.get_candidates(market, timeframe)
        active = db.get_active_gene(market, timeframe)
        print(f"\n  {market} ({timeframe}) — {len(candidates)} Kandidaten-Gene")
        print("  " + "-" * 74)
        if active:
            print(f"  ✔ AKTIV: seq_len={active['seq_len']:<3} rr={active['rr_ratio']:<4} "
                  f"trail={active['trailing_pct']}% risk={active['risk_pct']}% | "
                  f"Calmar={active['calmar']:.2f} PnL={active['total_pnl_pct']:+.1f}% "
                  f"MaxDD={active['max_dd_pct']:.1f}% n={active['total_trades']}")
        else:
            print("  ✘ Kein aktives Gen (kein Kandidat hat die OOS-Pruefung bestanden, "
                  "oder Discovery noch nicht gelaufen).")

        top5 = sorted(candidates, key=lambda c: c['calmar'], reverse=True)[:5]
        print(f"  Top 5 Kandidaten nach Calmar (Referenz, unabhaengig vom aktiven Status):")
        for c in top5:
            marker = " <- aktiv" if active and c['risk_gene_id'] == active['risk_gene_id'] else ""
            print(f"    seq={c['seq_len']:<3} rr={c['rr_ratio']:<4} trail={c['trailing_pct']}% "
                  f"risk={c['risk_pct']}% | Calmar={c['calmar']:>6.2f} "
                  f"PnL={c['total_pnl_pct']:>+7.1f}% MaxDD={c['max_dd_pct']:>5.1f}% "
                  f"n={c['total_trades']:>4}{marker}")

    print("\n" + "=" * 78 + "\n")
    db.close()


if __name__ == '__main__':
    main()
