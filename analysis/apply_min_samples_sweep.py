#!/usr/bin/env python3
# analysis/apply_min_samples_sweep.py
#
# Uebernimmt artifacts/results/min_samples_sweep.json in
# settings.json::scan_settings.min_samples_by_timeframe.
#
# Vorher inline als Bash-Heredoc in run_pipeline.sh -- ausgelagert in eine
# echte .py-Datei zusammen mit resolve_scan_pairs.py, aus demselben Grund
# (Heredocs mit eingebettetem Python-Code sind auf manchen Zielsystemen
# reproduzierbar fehlgeschlagen, vermutlich Terminator-/Zeilenenden-
# Empfindlichkeit -- siehe resolve_scan_pairs.py-Docstring).

import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
SWEEP_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'min_samples_sweep.json')


def main():
    if not os.path.exists(SWEEP_PATH):
        print("  Keine Sweep-Ergebnisse gefunden -- ueberspringe Uebernahme.")
        return

    with open(SWEEP_PATH, encoding='utf-8') as f:
        sweep_results = json.load(f)
    with open(SETTINGS_PATH, encoding='utf-8') as f:
        settings = json.load(f)

    scan_settings = settings.setdefault('scan_settings', {})
    by_tf = scan_settings.setdefault('min_samples_by_timeframe', {})
    for tf, r in sweep_results.items():
        if 'min_samples' in r:
            by_tf[tf] = r['min_samples']
            print(f"  {tf}: min_samples={r['min_samples']} (PnL {r.get('pnl_usdt', 0):+.2f}, "
                  f"Trades {r.get('trades', '?')}, MaxDD {r.get('max_drawdown_pct', '?')}%)")

    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print("  settings.json aktualisiert.")


if __name__ == '__main__':
    main()
