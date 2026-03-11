#!/usr/bin/env python3
"""
auto_optimizer_scheduler.py

Prüft bei jedem Aufruf ob eine Optimierung fällig ist und führt
die dnabot-Pipeline aus (scan_and_learn → portfolio_optimizer).
Sendet Telegram-Benachrichtigungen bei Start und Ende.

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

CACHE_DIR        = os.path.join(PROJECT_ROOT, 'artifacts', 'cache')
LOG_DIR          = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE    = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE      = os.path.join(PROJECT_ROOT, 'secret.json')
LAST_RUN_FILE    = os.path.join(CACHE_DIR, '.last_optimization_run')
IN_PROGRESS_FILE = os.path.join(CACHE_DIR, '.optimization_in_progress')
TRIGGER_LOG      = os.path.join(LOG_DIR, 'auto_optimizer_trigger.log')

SCAN_SCRIPT      = os.path.join(PROJECT_ROOT, 'scan_and_learn.py')
PORTFOLIO_SCRIPT = os.path.join(PROJECT_ROOT, 'run_portfolio_optimizer.py')
PYTHON_EXE       = os.path.join(PROJECT_ROOT, '.venv', 'bin', 'python3')


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
        if accounts:
            acc = accounts[0]
            return acc.get('telegram_bot_token'), acc.get('telegram_chat_id')
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
        requests.post(api_url, data={'chat_id': chat_id, 'text': message}, timeout=10)
        _log("TELEGRAM sent")
    except Exception as e:
        _log(f"TELEGRAM ERROR {e}")


# ---------------------------------------------------------------------------
# Pipeline-Ausführung
# ---------------------------------------------------------------------------

def _run_scan(opt_settings: dict) -> int:
    """Führt scan_and_learn.py aus."""
    cmd = [PYTHON_EXE, SCAN_SCRIPT]
    _log(f"SCAN_START cmd={' '.join(cmd)}")
    result = subprocess.run(cmd)
    _log(f"SCAN_EXIT rc={result.returncode}")
    return result.returncode


def _run_portfolio_optimizer(opt_settings: dict) -> int:
    """Führt run_portfolio_optimizer.py mit --auto-write aus."""
    capital = str(opt_settings.get('start_capital', 1000))
    risk    = str(opt_settings.get('risk_pct', 1.0))
    max_dd  = str(opt_settings.get('max_drawdown_pct', 30))

    cmd = [
        PYTHON_EXE, PORTFOLIO_SCRIPT,
        '--capital', capital,
        '--risk',    risk,
        '--max-dd',  max_dd,
        '--auto-write',
    ]
    _log(f"PORTFOLIO_START capital={capital} risk={risk} max_dd={max_dd}")
    result = subprocess.run(cmd)
    _log(f"PORTFOLIO_EXIT rc={result.returncode}")
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
            f"🚀 dnabot Auto-Optimizer GESTARTET\n"
            f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Schritt 1: Genome Discovery (scan_and_learn)\n"
            f"Schritt 2: Portfolio-Optimierung"
        )

    start_perf = time.time()
    success    = False

    try:
        rc_scan = _run_scan(opt_settings)
        if rc_scan != 0:
            _log(f"SCAN_FAILED rc={rc_scan}")
        else:
            rc_opt = _run_portfolio_optimizer(opt_settings)
            success = (rc_opt == 0)
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
                f"✅ dnabot Auto-Optimizer abgeschlossen\n"
                f"Dauer: {_format_elapsed(elapsed)}\n"
                f"Genome gescannt + Portfolio optimiert.\n"
                f"Neue active_strategies in settings.json eingetragen."
            )
    else:
        _log(f"FINISH result=failed elapsed_s={elapsed}")
        if send_tg:
            _send_telegram(
                f"❌ dnabot Auto-Optimizer FEHLGESCHLAGEN\n"
                f"Dauer: {_format_elapsed(elapsed)}\n"
                f"Logs prüfen: {TRIGGER_LOG}"
            )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='dnabot Auto-Optimizer Scheduler')
    parser.add_argument('--force', action='store_true',
                        help='Optimierung sofort erzwingen (ignoriert Zeitplan)')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    opt_settings = settings.get('optimization_settings', {})

    if not opt_settings.get('enabled', False) and not args.force:
        print("Auto-Optimierung deaktiviert (optimization_settings.enabled=false).")
        return

    schedule = opt_settings.get('schedule', {
        'day_of_week': 0, 'hour': 3, 'minute': 0,
        'interval': {'value': 7, 'unit': 'days'},
    })

    if args.force:
        reason = 'forced'
    else:
        due, reason = _is_due(schedule)
        if not due:
            print("Optimierung noch nicht fällig.")
            return

    run_optimization(schedule, opt_settings, reason)


if __name__ == '__main__':
    main()
