#!/usr/bin/env python3
# analysis/resolve_scan_pairs.py
#
# Loest DNABOT_OVERRIDE_COINS/DNABOT_OVERRIDE_TFS (siehe run_pipeline.sh) zu
# einer (Symbol, Timeframe)-Paarliste auf und gibt sie zeilenweise als
# "SYMBOL TIMEFRAME" aus.
#
# Ersetzt einen zuvor inline in run_pipeline.sh eingebetteten Python-Heredoc
# (python3 - <<'PYEOF' ... PYEOF) -- der war auf einem Live-System
# reproduzierbar leer (0 Paare statt der erwarteten 18), obwohl dieselbe
# Logik hier lokal und als eigenstaendige Funktion in analysis/
# alphabet_optimizer.py::_env_override_pairs() immer korrekt lief. Ursache
# nie sicher gefunden (vermutlich Heredoc-Terminator-/Zeilenenden-
# Empfindlichkeit auf dem Zielsystem) -- als echte .py-Datei entfaellt diese
# ganze Fehlerklasse (kein Heredoc, keine Terminator-Erkennung, normale
# Python-Dateiverarbeitung mit universal newlines).
#
# Ausfuehrung:
#   DNABOT_OVERRIDE_COINS="BTC DOGE" DNABOT_OVERRIDE_TFS="4h" python3 analysis/resolve_scan_pairs.py
#   python3 analysis/resolve_scan_pairs.py   (keine Overrides -> voll aus active_strategies)

import os
import sys
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def to_symbol(coin: str) -> str:
    coin = coin.strip().upper()
    return coin if '/' in coin else f"{coin}/USDT:USDT"


def main():
    coins_raw = os.environ.get('DNABOT_OVERRIDE_COINS', '').strip()
    tfs_raw = os.environ.get('DNABOT_OVERRIDE_TFS', '').strip()

    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json'), encoding='utf-8') as f:
            settings = json.load(f)
        active = settings.get('live_trading_settings', {}).get('active_strategies', [])
        auto_coins = list(dict.fromkeys(s['symbol'] for s in active if s.get('symbol')))
        auto_tfs = list(dict.fromkeys(s['timeframe'] for s in active if s.get('timeframe')))
    except Exception as e:
        print(f"WARNUNG: settings.json nicht lesbar ({e}) -- Fallback BTC/USDT:USDT 4h.", file=sys.stderr)
        auto_coins = []
        auto_tfs = []

    if not auto_coins:
        auto_coins = ['BTC/USDT:USDT']
    if not auto_tfs:
        auto_tfs = ['4h']

    coins = [to_symbol(c) for c in coins_raw.split()] if coins_raw else auto_coins
    tfs = [t.strip() for t in tfs_raw.split()] if tfs_raw else auto_tfs

    if not coins or not tfs:
        print("FEHLER: keine Coins/Timeframes aufloesbar.", file=sys.stderr)
        sys.exit(1)

    for sym in coins:
        for tf in tfs:
            print(f"{sym} {tf}")


if __name__ == '__main__':
    main()
