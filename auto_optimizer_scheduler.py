#!/usr/bin/env python3
"""
auto_optimizer_scheduler.py

Prüft bei jedem Aufruf ob eine Optimierung faellig ist und fuehrt dann die
volle momentum_exit-Pipeline aus:
  1. risk_genome_discover.py       -- Risiko-Gene fuer den VOLLEN Pool
                                       (POOL_COINS x POOL_TIMEFRAMES = 6h/
                                       4h/2h/1h, 1d ausgeschlossen) neu
                                       bewerten (IS/OOS-gated, aktuelles
                                       26-Wochen-Fenster) -- nicht nur die
                                       aktuell in active_strategies
                                       konfigurierten Paare
  2. backtest_momentum_exit.py     -- JEDES Paar mit einem aktiven Risiko-
                                       Gen in der DB backtesten (nicht nur
                                       die aktuell aktiven -- der Optimizer
                                       braucht den vollen entdeckten Pool,
                                       um ueberhaupt eine bessere Teilmenge
                                       finden zu koennen)
  3. run_portfolio_optimizer_momentum_exit.py --auto-write
                                    -- waehlt per Greedy-Calmar-Suche (Max-
                                       Drawdown-limitiert) die beste Teil-
                                       menge, schreibt active_strategies NUR
                                       bei echter Verbesserung neu, schickt
                                       Excel + HTML-Equity-Chart per Telegram

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

CACHE_DIR         = os.path.join(PROJECT_ROOT, 'artifacts', 'cache')
LOG_DIR           = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE     = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE       = os.path.join(PROJECT_ROOT, 'secret.json')
LAST_RUN_FILE     = os.path.join(CACHE_DIR, '.last_optimization_run')
IN_PROGRESS_FILE  = os.path.join(CACHE_DIR, '.optimization_in_progress')
TRIGGER_LOG       = os.path.join(LOG_DIR, 'auto_optimizer_trigger.log')

RISK_GENOME_SCRIPT      = os.path.join(PROJECT_ROOT, 'risk_genome_discover.py')
BACKTEST_SCRIPT         = os.path.join(PROJECT_ROOT, 'backtest_momentum_exit.py')
PORTFOLIO_OPTIMIZER_SCRIPT = os.path.join(PROJECT_ROOT, 'run_portfolio_optimizer_momentum_exit.py')

# Voller Coin/Timeframe-Pool fuer die automatisierte Pipeline. 1d bewusst
# ausgeschlossen (User-Entscheidung 2026-08-25: zu wenige Kerzen im 26-Wochen-
# Rolling-Fenster fuer eine belastbare Calmar-Schaetzung -- hatte den frueheren
# Auto-Write-Vorfall ausgeloest, der das etablierte 7x6h-Portfolio durch ein
# duennes 2x1d-Portfolio ersetzte). 6h/4h/2h/1h duerfen dagegen frei gegen-
# einander antreten -- der User will bewusst dem aktuellen 26-Wochen-Trend
# folgen statt einer zusaetzlichen Zwei-Fenster-Bestaetigungs-Huerde.
POOL_COINS       = ['BTC', 'XRP', 'ETH', 'SOL', 'ADA', 'AAVE', 'DOGE']
POOL_TIMEFRAMES  = ['6h', '4h', '2h', '1h']

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
    calibration.md) fuer den VOLLEN Pool (POOL_COINS x POOL_TIMEFRAMES, 1d
    ausgeschlossen) -- nicht nur die aktuell in active_strategies konfigurierten
    Paare. Jedes Paar bekommt so bei jedem Lauf ein frisches, auf dem aktuellen
    26-Wochen-Fenster (backtest_lookback_weeks) bewertetes Risiko-Gen, damit
    der Portfolio-Optimizer wirklich aus dem aktuellen Trend ueber alle vier
    Timeframes waehlen kann statt aus veralteten/nie neu bewerteten Genen.
    """
    failures = 0
    total = len(POOL_COINS) * len(POOL_TIMEFRAMES)
    _log(f"RISK_GENOME_START n_pairs={total}")
    for coin in POOL_COINS:
        for timeframe in POOL_TIMEFRAMES:
            market = f"{coin}/USDT:USDT"
            cmd = [PYTHON_EXE, RISK_GENOME_SCRIPT, '--symbol', market, '--timeframe', timeframe]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                failures += 1
                _log(f"RISK_GENOME_PAIR_FAILED {market} ({timeframe}) rc={result.returncode}")
    _log(f"RISK_GENOME_EXIT n_pairs={total} failures={failures}")
    return 0 if failures < total else 1


def _discovered_pairs_with_active_gene():
    """Alle (market, timeframe)-Paare in risk_genome.db, die gerade ein
    aktives Risiko-Gen haben -- der volle Pool, aus dem der Portfolio-
    Optimizer waehlen kann (nicht nur die aktuell in active_strategies
    konfigurierten). 1d wird hier defensiv rausgefiltert, auch falls die DB
    noch aeltere 1d-Eintraege enthaelt (siehe POOL_TIMEFRAMES-Kommentar) --
    ohne dieses Filter koennte ein einzelner, duenner 1d-Ausreisser wieder
    das ganze Portfolio kippen."""
    from dnabot.genome.risk_genome_db import RiskGenomeDB
    db_path = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_genome.db')
    if not os.path.exists(db_path):
        return []
    db = RiskGenomeDB(db_path)
    try:
        pairs = []
        for market, timeframe in db.get_all_market_pairs():
            if timeframe == '1d':
                continue
            gene = db.get_active_gene(market, timeframe)
            if gene:
                pairs.append((market, timeframe, gene))
        return pairs
    finally:
        db.close()


def _run_backtest_all_discovered(opt_settings: dict) -> int:
    """
    Backtestet JEDES Paar mit einem aktiven Risiko-Gen (nicht nur die aktuell
    in active_strategies konfigurierten) ueber die ECHTE Live-Signalfunktion,
    jeweils mit dessen EIGENEN Gen-Parametern. run_portfolio_optimizer_
    momentum_exit.py liest nur vorhandene backtest_*_momentum_exit.json --
    ohne diesen Schritt haette die Discovery keinerlei Einfluss auf die
    Portfolio-Auswahl, die wuerde nur mit zufaellig noch vorhandenen, evtl.
    laengst veralteten Dateien weiterarbeiten.
    """
    pairs = _discovered_pairs_with_active_gene()
    if not pairs:
        _log("BACKTEST_ALL_SKIP keine Paare mit aktivem Gen")
        return 0

    capital = str(opt_settings.get('start_capital', 1000))
    _log(f"BACKTEST_ALL_START n_pairs={len(pairs)} capital={capital}")
    failures = 0
    for market, timeframe, gene in pairs:
        cmd = [
            PYTHON_EXE, BACKTEST_SCRIPT,
            '--symbol', market, '--timeframe', timeframe,
            '--capital', capital,
            '--risk', str(gene['risk_pct']),
            '--rr-ratio', str(gene['rr_ratio']),
            '--trailing-callback-pct', str(gene['trailing_pct']),
            '--seq-len', str(gene['seq_len']),
            '--oos-weeks', '26',
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            failures += 1
            _log(f"BACKTEST_ALL_PAIR_FAILED {market} ({timeframe}) rc={result.returncode}")
    _log(f"BACKTEST_ALL_EXIT n_pairs={len(pairs)} failures={failures}")
    return 0 if failures < len(pairs) else 1


def _run_portfolio_optimizer(opt_settings: dict) -> int:
    """
    Waehlt per Greedy-Calmar-Suche (Max-Drawdown-limitiert) die beste
    Teilmenge aus allen gerade frisch gebacktesteten Paaren -- pooled NICHT
    blind alles, sondern stoppt sobald ein weiterer Coin das gemeinsame
    Ergebnis nicht mehr verbessert oder das Max-Drawdown-Limit reissen wuerde.
    --auto-write schreibt active_strategies NUR neu, wenn das Ergebnis
    nachweislich besser ist als das aktuell konfigurierte Portfolio, und
    erstellt danach automatisch Excel-Trade-Log + HTML-Equity-Chart
    (per Telegram verschickt, siehe generate_portfolio_equity_chart()/
    generate_trades_excel() dort).
    """
    capital = str(opt_settings.get('start_capital', 1000))
    max_dd  = str(opt_settings.get('max_drawdown_pct', 30))
    cmd = [
        PYTHON_EXE, PORTFOLIO_OPTIMIZER_SCRIPT,
        '--capital', capital,
        '--max-dd', max_dd,
        '--auto-write',
    ]
    _log(f"PORTFOLIO_OPTIMIZER_START capital={capital} max_dd={max_dd}")
    result = subprocess.run(cmd)
    _log(f"PORTFOLIO_OPTIMIZER_EXIT rc={result.returncode}")
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
            f"1. Risiko-Gen-Discovery\n"
            f"2. Backtest aller discovered Paare\n"
            f"3. Portfolio-Optimierung (Max-Drawdown-limitiert)"
        )

    start_perf = time.time()
    success    = False

    try:
        rc_discover = _run_risk_genome_discovery()
        if rc_discover != 0:
            _log(f"RISK_GENOME_FAILED rc={rc_discover} -- fahre trotzdem mit Backtest/Optimizer fort")

        rc_backtest = _run_backtest_all_discovered(opt_settings)
        if rc_backtest != 0:
            _log(f"BACKTEST_ALL_FAILED rc={rc_backtest} -- Portfolio-Optimierung nutzt evtl. unvollstaendige Daten")

        rc_optimize = _run_portfolio_optimizer(opt_settings)
        success = (rc_optimize == 0)
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
                f"Risiko-Gene aktualisiert, Portfolio geprueft -- "
                f"settings.json nur bei echter Verbesserung geaendert."
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
