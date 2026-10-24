"""Teamhistorie: welche Strategien waren zu welchem Zeitpunkt live aktiv.

master_runner.py ruft record_team() bei jedem Lauf mit dem Team auf, das er tatsaechlich
handelt. Ein neuer Eintrag entsteht nur, wenn sich Team oder Config-Inhalt geaendert hat --
egal ob durch die Wochenauswahl, update.sh oder eine manuelle Aenderung.
Ein Eintrag gilt ab valid_from bis zum valid_from des naechsten Eintrags.

Datei: artifacts/db/team_history.jsonl (gitignored, eine JSON-Zeile je Teamwechsel).
Config-Pruefsumme = Inhalt des Strategie-Eintrags in settings.json (Overrides).
Fehlt sie, wird sie einmalig angelegt: aktuelles Team aus settings.json, gueltig ab der letzten
Wochenauswahl (artifacts/cache/.last_optimization_run, Ortszeit des Rechners) -- ohne Config-Pruefsumme.
"""
import hashlib
import json
import os
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
HISTORY_FILE = os.path.join(ROOT, 'artifacts', 'db', 'team_history.jsonl')
SETTINGS_FILE = os.path.join(ROOT, 'settings.json')
LAST_SELECTION_FILE = os.path.join(ROOT, 'artifacts', 'cache', '.last_optimization_run')


def strategy_key(symbol: str, timeframe: str) -> str:
    """'DOGE/USDT:USDT', '6h' -> 'DOGEUSDTUSDT_6h'."""
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"


def _config_hash(key: str) -> str | None:
    """dnabot hat keine Config-Datei je Strategie -- die Parameter (risk_/momentum_exit_overrides)
    stehen im Strategie-Eintrag in settings.json. Dessen Inhalt bildet die Pruefsumme."""
    if not os.path.exists(SETTINGS_FILE):
        return None
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        active = json.load(f).get('live_trading_settings', {}).get('active_strategies', [])
    for s in active:
        if s.get('symbol') and s.get('timeframe') and strategy_key(s['symbol'], s['timeframe']) == key:
            return hashlib.sha1(json.dumps(s, sort_keys=True).encode()).hexdigest()[:12]
    return None


def _ensure_seeded(path: str) -> None:
    """Echte Historie fehlt noch -> einmalig mit dem aktuellen Team ab der letzten Wochenauswahl anlegen."""
    if path != HISTORY_FILE or os.path.exists(path) or not os.path.exists(SETTINGS_FILE):
        return
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        active = json.load(f).get('live_trading_settings', {}).get('active_strategies', [])
    keys = sorted({strategy_key(s['symbol'], s['timeframe']) for s in active
                   if s.get('active') and not s.get('manage_only') and s.get('symbol') and s.get('timeframe')})
    if not keys or not os.path.exists(LAST_SELECTION_FILE):
        return  # ohne bekannten Zeitpunkt nichts erfinden -- Aufzeichnung beginnt mit master_runner
    with open(LAST_SELECTION_FILE) as f:
        since = datetime.fromisoformat(f.read().strip())
    since = since.astimezone() if since.tzinfo is None else since  # Datei: Ortszeit ohne Zone
    entry = {'valid_from': since.astimezone(timezone.utc).isoformat(timespec='seconds'),
             'source': 'Wochenauswahl (letzter Lauf, nachgetragen)', 'team': {k: None for k in keys}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def load_history(path: str = HISTORY_FILE) -> list:
    _ensure_seeded(path)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def record_team(keys: list, source: str, path: str = HISTORY_FILE, now: datetime | None = None,
                force: bool = False) -> bool:
    """Haengt einen Eintrag an, wenn Team oder Config-Inhalt vom letzten Eintrag abweicht.
    force=True schreibt auch ein unveraendertes Team (Wochenauswahl hat bestaetigt)."""
    team = {k: _config_hash(k) for k in sorted(set(keys))}
    history = load_history(path)
    if history and history[-1]['team'] == team and not force:
        return False
    entry = {'valid_from': (now or datetime.now(timezone.utc)).isoformat(timespec='seconds'),
             'source': source, 'team': team}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')
    return True


def team_at(ts, path: str = HISTORY_FILE) -> dict | None:
    """Team (Key -> Config-Hash), das zum Zeitpunkt ts aktiv war; None vor der ersten Aufzeichnung."""
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    active = None
    for entry in load_history(path):
        if pd.Timestamp(entry['valid_from']) <= ts:
            active = entry['team']
        else:
            break
    return active
