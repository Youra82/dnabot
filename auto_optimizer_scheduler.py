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
LAST_SCAN_FILE   = os.path.join(CACHE_DIR, '.last_scan_run')
IN_PROGRESS_FILE = os.path.join(CACHE_DIR, '.optimization_in_progress')
TRIGGER_LOG      = os.path.join(LOG_DIR, 'auto_optimizer_trigger.log')

SCAN_SCRIPT      = os.path.join(PROJECT_ROOT, 'scan_and_learn.py')
PORTFOLIO_SCRIPT = os.path.join(PROJECT_ROOT, 'run_portfolio_optimizer.py')
ALPHABET_SCRIPT  = os.path.join(PROJECT_ROOT, 'analysis', 'alphabet_optimizer.py')
BACKTEST_SCRIPT  = os.path.join(PROJECT_ROOT, 'run_backtest.py')
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


def _get_last_scan_run() -> datetime | None:
    if not os.path.exists(LAST_SCAN_FILE):
        return None
    with open(LAST_SCAN_FILE, 'r') as f:
        s = f.read().strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _set_last_scan_run():
    os.makedirs(CACHE_DIR, exist_ok=True)
    now_str = datetime.now().isoformat()
    with open(LAST_SCAN_FILE, 'w') as f:
        f.write(now_str)
    _log(f"LAST_SCAN updated={now_str}")


def _is_scan_due(opt_settings: dict) -> bool:
    if os.path.exists(IN_PROGRESS_FILE):
        _log("SCAN_SKIP already_in_progress")
        return False
    last_scan = _get_last_scan_run()
    if last_scan is None:
        return True
    interval_h = float(opt_settings.get('scan_interval_hours', 24))
    return (datetime.now() - last_scan).total_seconds() / 3600 >= interval_h


RUN_COUNTER_FILE = os.path.join(CACHE_DIR, '.db_reset_counter')

def _get_run_counter() -> int:
    if not os.path.exists(RUN_COUNTER_FILE):
        return 0
    try:
        with open(RUN_COUNTER_FILE, 'r') as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return 0

def _set_run_counter(value: int):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(RUN_COUNTER_FILE, 'w') as f:
        f.write(str(value))


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

def _run_alphabet_optimizer(opt_settings: dict) -> int:
    """
    Führt analysis/alphabet_optimizer.py --all-scan-pairs --auto-apply aus,
    VOR dem eigentlichen Scan (jeder Optuna-Trial macht seinen eigenen
    vollstaendigen Discovery+Backtest-Durchlauf -- ein vorheriger Scan mit
    dem Default-Alphabet waere sonst verschwendete Arbeit, siehe
    scan_and_learn.py's Alphabet-Wechsel-Erkennung).

    Bestaetigte Pairs werden automatisch in settings.json::genome_settings.
    alphabet_by_pair uebernommen -- der direkt danach laufende _run_scan()
    nutzt das dann sofort. Pairs mit bereits bestaetigtem Alphabet werden
    von alphabet_optimizer.py standardmaessig uebersprungen (kein
    wiederholtes Neu-Optimieren bereits abgeschlossener Pairs).

    Steuerbar via optimization_settings.alphabet_optimizer_enabled (Default
    false -- explizit aktivieren, da das den Optimierungslauf deutlich
    verlaengert) und .alphabet_optimizer_trials (Default 20).
    """
    trials  = int(opt_settings.get('alphabet_optimizer_trials', 20))
    # --risk/--capital: dieselben Werte, die _run_portfolio_optimizer() weiter
    # unten fuer denselben Lauf verwendet (optimization_settings.risk_pct/
    # start_capital) -- ohne das bewertet der Optimizer Drawdown/PnL still mit
    # risk_settings aus settings.json (den LIVE-Werten, z.B. 5%/5x aus einer
    # aggressiven Portfolio-Optimierung), was die MAX_DD_PCT-Grenze faktisch
    # viel haerter treffen laesst als hier beabsichtigt (siehe run_pipeline.sh,
    # derselbe Fix -- dort ueber $CAPITAL/$RISK vom Nutzer-Prompt).
    capital = str(opt_settings.get('start_capital', 1000))
    risk    = str(opt_settings.get('risk_pct', 1.0))
    cmd = [PYTHON_EXE, ALPHABET_SCRIPT, '--all-scan-pairs', '--n-trials', str(trials),
           '--capital', capital, '--risk', risk, '--auto-apply']
    _log(f"ALPHABET_START trials={trials} capital={capital} risk={risk}")
    result = subprocess.run(cmd)
    _log(f"ALPHABET_EXIT rc={result.returncode}")
    return result.returncode


def _run_scan(opt_settings: dict) -> int:
    """Führt scan_and_learn.py aus."""
    cmd = [PYTHON_EXE, SCAN_SCRIPT]
    _log(f"SCAN_START cmd={' '.join(cmd)}")
    result = subprocess.run(cmd)
    _log(f"SCAN_EXIT rc={result.returncode}")
    return result.returncode


def _run_backtest_all(opt_settings: dict) -> int:
    """
    Backtestet alle (market, timeframe)-Paare aus der Genome-DB frisch
    (run_backtest.py --all-from-db), BEVOR der Portfolio-Optimizer laeuft.

    run_portfolio_optimizer.py generiert selbst KEINE Backtests -- es liest
    ausschliesslich vorhandene artifacts/results/backtest_*.json (siehe
    load_all_results()). Ohne diesen Schritt haette die vorangegangene
    Alphabet-Optimierung + Discovery + Evolver (oft stundenlange Arbeit)
    ueberhaupt keinen Einfluss auf die Portfolio-Auswahl -- die wuerde
    einfach mit welchen backtest_*.json-Dateien auch immer zufaellig noch
    von frueheren, moeglicherweise laengst veralteten Laeufen auf der Platte
    liegen weiterarbeiten.
    """
    capital = str(opt_settings.get('start_capital', 1000))
    risk    = str(opt_settings.get('risk_pct', 1.0))
    cmd = [PYTHON_EXE, BACKTEST_SCRIPT, '--all-from-db', '--capital', capital, '--risk', risk]
    _log(f"BACKTEST_START cmd={' '.join(cmd)}")
    result = subprocess.run(cmd)
    _log(f"BACKTEST_EXIT rc={result.returncode}")
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
    if opt_settings.get('require_persistence', False):
        cmd.append('--persistence')
    _log(f"PORTFOLIO_START capital={capital} risk={risk} max_dd={max_dd} "
         f"persistence={opt_settings.get('require_persistence', False)}")
    result = subprocess.run(cmd)
    _log(f"PORTFOLIO_EXIT rc={result.returncode}")
    return result.returncode


def _run_scan_standalone(opt_settings: dict):
    """Nur Genome-Scan ohne Portfolio-Optimierung (wenn enabled=false)."""
    send_tg    = opt_settings.get('send_telegram_on_completion', False)
    start_time = datetime.now()
    _log("SCAN_ONLY_START")

    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(start_time.isoformat())

    start_perf = time.time()
    success    = False
    try:
        rc      = _run_scan(opt_settings)
        success = (rc == 0)
    except Exception as e:
        _log(f"SCAN_ONLY_ERROR {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)

    elapsed = round(time.time() - start_perf, 1)

    if success:
        _set_last_scan_run()
        _log(f"SCAN_ONLY_FINISH elapsed_s={elapsed}")
        if send_tg:
            _send_telegram(
                f"🧬 dnabot Genome-Scan abgeschlossen\n"
                f"Dauer: {_format_elapsed(elapsed)}\n"
                f"Nur neue, noch nicht gesehene Kerzen wurden verarbeitet.\n"
                f"Portfolio-Optimierung: deaktiviert (enabled=false)"
            )
    else:
        _log(f"SCAN_ONLY_FAILED elapsed_s={elapsed}")
        if send_tg:
            _send_telegram(
                f"❌ dnabot Genome-Scan FEHLGESCHLAGEN\n"
                f"Dauer: {_format_elapsed(elapsed)}\n"
                f"Logs prüfen: {TRIGGER_LOG}"
            )


def run_optimization(schedule: dict, opt_settings: dict, reason: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    start_time = datetime.now()
    send_tg    = opt_settings.get('send_telegram_on_completion', False)

    _log(f"START reason={reason}")

    # DB und alte Backtest-Ergebnisse zurücksetzen (steuerbar via settings.json)
    # reset_db_before_optimize: true  → Reset aktiv
    # reset_db_every_n_runs: N        → Reset nur alle N Läufe (0 = jeder Lauf)
    reset_info = "deaktiviert"
    if opt_settings.get('reset_db_before_optimize', False):
        import glob
        every_n   = int(opt_settings.get('reset_db_every_n_runs', 0))
        counter   = _get_run_counter()
        counter  += 1
        do_reset  = (every_n <= 0) or (counter >= every_n)
        if do_reset:
            counter = 0
            db_path = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
            if os.path.exists(db_path):
                os.remove(db_path)
                _log("DB_RESET genome.db geloescht")
            results_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
            if os.path.isdir(results_dir):
                for f in glob.glob(os.path.join(results_dir, 'backtest_*.json')):
                    os.remove(f)
            _log(f"DB_RESET backtest_*.json geloescht (every_n={every_n})")
            reset_info = f"✅ zurückgesetzt (alle {every_n} Läufe)" if every_n > 0 else "✅ zurückgesetzt"
        else:
            _log(f"DB_RESET uebersprungen — Lauf {counter}/{every_n}")
            reset_info = f"⏭ übersprungen — Lauf {counter}/{every_n}"
        _set_run_counter(counter)

    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(start_time.isoformat())

    alphabet_enabled = opt_settings.get('alphabet_optimizer_enabled', False)
    steps_desc = (
        "Schritt 1: Alphabet-Optimierung pro Pair\n"
        "Schritt 2: Genome Discovery (scan_and_learn)\n"
        "Schritt 3: Backtest aller Pairs\n"
        "Schritt 4: Portfolio-Optimierung"
    ) if alphabet_enabled else (
        "Schritt 1: Genome Discovery (scan_and_learn)\n"
        "Schritt 2: Backtest aller Pairs\n"
        "Schritt 3: Portfolio-Optimierung"
    )
    if send_tg:
        _send_telegram(
            f"🚀 dnabot Auto-Optimizer GESTARTET\n"
            f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"DB-Reset: {reset_info}\n"
            f"{steps_desc}"
        )

    start_perf = time.time()
    success    = False

    try:
        # Alphabet-Optimierung VOR der Discovery -- jeder Optuna-Trial macht
        # seinen eigenen vollstaendigen Discovery+Backtest-Durchlauf, ein
        # vorheriger Scan mit dem Default-Alphabet waere sonst verschwendete
        # Arbeit. Bereits bestaetigte Pairs werden automatisch uebersprungen.
        if alphabet_enabled:
            rc_alpha = _run_alphabet_optimizer(opt_settings)
            if rc_alpha != 0:
                _log(f"ALPHABET_FAILED rc={rc_alpha} -- fahre trotzdem mit Discovery fort")

        rc_scan = _run_scan(opt_settings)
        if rc_scan != 0:
            _log(f"SCAN_FAILED rc={rc_scan}")
        else:
            # Backtest ALLER DB-Pairs frisch generieren -- run_portfolio_
            # optimizer.py liest nur vorhandene backtest_*.json, generiert
            # selbst keine (siehe _run_backtest_all()-Docstring). Ohne
            # diesen Schritt haette Alphabet-Optimierung+Discovery keinerlei
            # Einfluss auf die Portfolio-Auswahl.
            rc_bt = _run_backtest_all(opt_settings)
            if rc_bt != 0:
                _log(f"BACKTEST_FAILED rc={rc_bt} -- Portfolio-Optimierung nutzt evtl. veraltete Daten")
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
    enabled      = opt_settings.get('enabled', False)

    # Wenn Portfolio-Optimierung deaktiviert: nur Genome-Scan (inkrementell)
    if not enabled and not args.force:
        if _is_scan_due(opt_settings):
            _run_scan_standalone(opt_settings)
        else:
            print("Genome-Scan noch nicht fällig (optimization_settings.enabled=false).")
        return

    # Portfolio-Optimierung aktiv (enabled=true oder --force)
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
