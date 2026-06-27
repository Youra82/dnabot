#!/usr/bin/env python3
"""
Patcht die Tracker-Dateien der laufenden Trades mit den korrekten Preisen
aus den Telegram-Benachrichtigungen.

Ausführen auf dem VPS:
  cd /path/to/dnabot
  python3 patch_tracker_prices.py
"""

import json
import os

TRACKER_DIR = os.path.join(os.path.dirname(__file__), 'artifacts', 'tracker')

LIVE_TRADES = [
    {
        "symbol": "AVAX/USDT:USDT",
        "timeframe": "2h",
        "direction": "SHORT",
        "entry_price": 6.528,
        "sl_price": 6.630,
        "tp_price": 6.324,
        "contracts": 7.2377,
    },
    {
        "symbol": "LINK/USDT:USDT",
        "timeframe": "2h",
        "direction": "LONG",
        "entry_price": 7.366,
        "sl_price": 7.286,
        "tp_price": 7.526,
        "contracts": 35.1110,
    },
    {
        "symbol": "FIL/USDT:USDT",
        "timeframe": "2h",
        "direction": "LONG",
        "entry_price": 0.751,
        "sl_price": 0.738,
        "tp_price": 0.777,
        "contracts": 402.9954,
    },
]


def get_tracker_path(symbol: str, timeframe: str) -> str:
    safe = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}.json"
    return os.path.join(TRACKER_DIR, safe)


def patch_tracker(trade: dict):
    path = get_tracker_path(trade["symbol"], trade["timeframe"])
    if not os.path.exists(path):
        print(f"[SKIP] {path} nicht gefunden — trade noch nicht im Tracker.")
        return

    with open(path, 'r') as f:
        tracker = json.load(f)

    active_genome = tracker.get('active_genome') or {}

    changed = []

    # Kein active_genome → anlegen mit bekannten Preisen
    if not tracker.get('active_genome'):
        tracker['active_genome'] = {
            "genome_id": "manual_patch",
            "sequence": "manual",
            "direction": trade["direction"],
            "seq_length": 4,
            "score": 0.0,
            "winrate": 0.0,
            "total_occurrences": 0,
            "entry_price": trade["entry_price"],
            "sl_price": trade["sl_price"],
            "tp_price": trade["tp_price"],
        }
        changed.append("active_genome erstellt")
    else:
        # Vorhandenes active_genome: nur fehlende/falsche Preise korrigieren
        for key in ("entry_price", "sl_price", "tp_price"):
            if not active_genome.get(key):
                tracker['active_genome'][key] = trade[key]
                changed.append(f"{key}={trade[key]}")

    # tsl_api_visible entfernen falls gesetzt (→ Repair läuft beim nächsten Bot-Lauf)
    if 'tsl_api_visible' in tracker:
        del tracker['tsl_api_visible']
        changed.append("tsl_api_visible entfernt (→ Repair)")

    if changed:
        with open(path, 'w') as f:
            json.dump(tracker, f, indent=4)
        print(f"[PATCHED] {trade['symbol']} ({trade['timeframe']}): {', '.join(changed)}")
    else:
        print(f"[OK]      {trade['symbol']} ({trade['timeframe']}): active_genome bereits korrekt, kein Patch nötig.")


if __name__ == "__main__":
    print(f"Tracker-Dir: {TRACKER_DIR}")
    for trade in LIVE_TRADES:
        patch_tracker(trade)
    print("\nFertig. Nächster Bot-Lauf repariert die fehlenden Trailing Stops automatisch.")
