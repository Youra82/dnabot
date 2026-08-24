#!/usr/bin/env python3
"""
auto_optimizer_scheduler.py

Prüft bei jedem Aufruf ob eine Risiko-Gen-Discovery faellig ist (siehe
risk_genome_discover.py + genome/risk_genome_db.py, momentum_exit-Strategie)
und fuehrt sie aus. Sendet Telegram-Benachrichtigungen bei Start und Ende.

Aufruf:
  python3 auto_optimizer_scheduler.py           # normale Prüfung
  python3 auto_optimizer_scheduler.py --force   # sofort erzwingen
"""

import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime

PROJECT_ROOT     = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

CACHE_DIR         = os.path.join(PROJECT_ROOT, 'artifacts', 'cache')
LOG_DIR           = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE     = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE       = os.path.join(PROJECT_ROOT, 'secret.json')
LAST_RUN_FILE     = os.path.join(CACHE_DIR, '.last_optimization_run')
IN_PROGRESS_FILE  = os.path.join(CACHE_DIR, '.optimization_in_progress')
TRIGGER_LOG       = os.path.join(LOG_DIR, 'auto_optimizer_trigger.log')

RISK_GENOME_SCRIPT = os.path.join(PROJECT_ROOT, 'risk_genome_discover.py')

# Plattformuebergreifend wie run_momentum_exit_pipeline.sh: Unix-Layout zuerst
# pruefen (unveraendertes Verhalten auf dem Linux-VPS), Windows-Fallback fuer
# lokale Entwicklung/Tests.
_unix_python = os.path.join(PROJECT_ROOT, '.venv', 'bin', 'python3')
_win_python = os.path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe')
PYTHON_EXE = _unix_python if os.path.exists(_unix_python) else _win_python


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().isoformat()} AUTO-OPTIMIZER {msg}"
    with open(TRIGGER_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _get_last_run() -> datetime | None:
    if not os.path.exists(LAST_RUN_FILE):
        return None
    with open(LAST_RUN_FILE, 'r') as f:
        s = f.read().strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _set_last_run():
    os.makedirs(CACHE_DIR, exist_ok=True)
    now_str = datetime.now().isoformat()
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(now_str)
    _log(f"LAST_RUN updated={now_str}")


def _is_due(schedule: dict) -> tuple[bool, str]:
    if os.path.exists(IN_PROGRESS_FILE):
        _log("SKIP already_in_progress")
        return False, None

    last_run = _get_last_run()
    if last_run is None:
        return True, 'first_run'

    interval_cfg     = schedule.get('interval', {})
    value            = int(interval_cfg.get('value', 7))
    unit             = interval_cfg.get('unit', 'days')
    multipliers      = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
    interval_seconds = value * multipliers.get(unit, 86400)

    if (datetime.now() - last_run).total_seconds() >= interval_seconds:
        return True, 'interval'

    now    = datetime.now()
    dow    = int(schedule.get('day_of_week', 0))
    hour   = int(schedule.get('hour', 3))
    minute = int(schedule.get('minute', 0))
    if now.weekday() == dow and now.hour == hour and minute <= now.minute < minute + 15:
        if last_run.date() < now.date():
            return True, 'scheduled'

    return False, None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _get_telegram_credentials():
    try:
        with open(SECRET_FILE, 'r') as f:
            secrets = json.load(f)
        accounts = secrets.get('dnabot', [])
        acc = accounts[0] if accounts else {}
        bot_token = acc.get('telegram_bot_token', '') or secrets.get('telegram', {}).get('bot_token', '')
        chat_id   = acc.get('telegram_chat_id', '')   or secrets.get('telegram', {}).get('chat_id', '')
        if bot_token and chat_id:
            return bot_token, chat_id
    except Exception:
        pass
    return None, None


def _send_telegram(message: str):
    bot_token, chat_id = _get_telegram_credentials()
    if not bot_token or not chat_id:
        _log("TELEGRAM SKIP kein token/chat_id")
        return
    try:
        import requests
        api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(api_url, data={'chat_id': chat_id, 'text': message}, timeout=10)
        if resp.ok:
            _log("TELEGRAM sent")
        else:
            _log(f"TELEGRAM ERROR HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        _log(f"TELEGRAM ERROR {e}")


# ---------------------------------------------------------------------------
# Pipeline-Ausführung
# ---------------------------------------------------------------------------

def _run_risk_genome_discovery() -> int:
    """
    Aktualisiert die Risiko-Gen-Datenbank (momentum_exit-Strategie, siehe
    genome/risk_genome_db.py + Fund AQ/AR in research_dnabot_direction_
    calibration.md): baut Kandidaten-Risiko-Gene, bewertet sie per Calmar auf
    einem In-Sample-Fenster, aktiviert das beste per risk_evolver.py und
    prueft es einmalig auf einem Out-of-Sample-Fenster. Ohne --symbol/
    --timeframe verarbeitet das Skript automatisch alle momentum_exit-
    Eintraege aus active_strategies. Kein Fehler wenn es keine gibt.
    """
    cmd = [PYTHON_EXE, RISK_GENOME_SCRIPT]
    _log(f"RISK_GENOME_START cmd={' '.join(cmd)}")
    result = subprocess.run(cmd)
    _log(f"RISK_GENOME_EXIT rc={result.returncode}")
    return result.returncode


def run_optimization(schedule: dict, opt_settings: dict, reason: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    start_time = datetime.now()
    send_tg    = opt_settings.get('send_telegram_on_completion', False)

    _log(f"START reason={reason}")

    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(start_time.isoformat())

    if send_tg:
        _send_telegram(
            f"🚀 dnabot Risiko-Gen-Discovery GESTARTET\n"
            f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    start_perf = time.time()
    success    = False

    try:
        rc = _run_risk_genome_discovery()
        success = (rc == 0)
    except Exception as e:
        _log(f"ERROR {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)

    elapsed = round(time.time() - start_perf, 1)

    if success:
        _set_last_run()
        _log(f"FINISH result=success elapsed_s={elapsed}")
        if send_tg:
            _send_telegram(
                f"✅ dnabot Risiko-Gen-Discovery abgeschlossen\n"
                f"Dauer: {_format_elapsed(elapsed)}"
            )
    else:
        _log(f"FINISH result=failed elapsed_s={elapsed}")
        if send_tg:
            _send_telegram(
                f"❌ dnabot Risiko-Gen-Discovery FEHLGESCHLAGEN\n"
                f"Dauer: {_format_elapsed(elapsed)}\n"
                f"Logs prüfen: {TRIGGER_LOG}"
            )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='dnabot Auto-Optimizer Scheduler')
    parser.add_argument('--force', action='store_true',
                        help='Discovery sofort erzwingen (ignoriert Zeitplan)')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    opt_settings = settings.get('optimization_settings', {})
    schedule = opt_settings.get('schedule', {
        'day_of_week': 0, 'hour': 3, 'minute': 0,
        'interval': {'value': 7, 'unit': 'days'},
    })

    if args.force:
        reason = 'forced'
    else:
        due, reason = _is_due(schedule)
        if not due:
            print("Risiko-Gen-Discovery noch nicht fällig.")
            return

    run_optimization(schedule, opt_settings, reason)


if __name__ == '__main__':
    main()
