#!/usr/bin/env python3
# analysis/min_samples_sweep.py
#
# Findet automatisch, GETRENNT PRO TIMEFRAME, den min_samples_to_activate-Wert
# mit der besten gepoolten PnL ueber alle Coins dieses Timeframes in
# genome.db -- per Optuna (TPE-Sampler) statt starrem Grid. Nebenbedingung:
# maximaler Drawdown ueber alle Coins <= MAX_DRAWDOWN_PCT (Standard 30%) --
# Trials, die das verletzen, werden verworfen, egal wie gut ihr PnL sonst ist.
#
# Hintergrund: Der Regime-Fix vom 2026-08-13 (discovery.py) hat dafuer
# gesorgt, dass Genome-Vorkommen erstmals korrekt in TREND/RANGE/NEUTRAL
# aufgeteilt werden, statt (Bug) immer in einen gemeinsamen NEUTRAL-Topf zu
# fallen. Das macht die Stichprobe PRO REGIME-TOPF pro Genom viel duenner
# als vorher -- min_samples_to_activate=2 wurde faktisch fuer die alte,
# gepoolte Statistik kalibriert. Ein Multi-Pair-Backtest direkt danach zeigte
# schwache Performance (GESAMT-WR 31.7% auf 6 Pairs, unter der Breakeven-
# Schwelle). Dieses Skript sucht systematisch pro Timeframe den Wert, der
# das PnL-mässig am besten macht.
#
# Design-Entscheidungen:
#   - Optimiert wird PRO TIMEFRAME (nicht pro Coin!) -- die Zielfunktion
#     summiert PnL ueber ALLE Coins dieses Timeframes. Pro-Coin-Optimierung
#     wuerde bei Pairs mit nur 1-10 historischen Trades reines Rauschen
#     fitten statt einen echten Edge zu finden.
#   - Trial-Ergebnisse mit zu wenigen Gesamt-Trades (MIN_TRADES_BY_TIMEFRAME,
#     je kleiner der Timeframe desto hoeher die Anforderung -- mehr Kerzen
#     verfuegbar) werden hart bestraft (-1e6) -- sonst konvergiert Optuna
#     trivial auf "so hoch, dass nie gehandelt wird" (PnL=0 sieht sonst besser
#     aus als jeder verlustreiche, aber echte Wert). Referenzpunkt: der volle
#     22-Coin x 5-Timeframe-Pool (110 Strategien) soll zusammen > 200
#     Trades/Jahr ergeben -- print_summary() zeigt die erreichte Summe an.
#   - fine_df wird bewusst NICHT genutzt (keine Intrabar-Feindaten-Simulation)
#     -- das dominiert sonst die Laufzeit durch viele einzelne Tages-API-Calls.
#     Fuer den relativen Vergleich zwischen min_samples-Werten reicht die
#     Grob-Kerzen-Approximation in simulate_trade().
#   - Jedes (market, timeframe)-Paar wird nur EINMAL von der Exchange geladen
#     und fuer alle Optuna-Trials wiederverwendet (kein Re-Download pro Trial).
#   - Optuna-Studien werden in einer SQLite-Datei persistiert (load_if_exists) --
#     ein Absturz mitten in der Nacht verliert dadurch nur laufende Trials,
#     nicht die bisherigen Ergebnisse. Neustart macht einfach weiter.
#
# Ausfuehrung (gedacht fuer einen laengeren Lauf, z.B. ueber Nacht):
#   nohup python3 analysis/min_samples_sweep.py > logs/min_samples_sweep.log 2>&1 &
#
# Nur einen Timeframe testen (z.B. zum Verifizieren):
#   python3 analysis/min_samples_sweep.py --timeframe 6h --n-trials 15
#
# Nur die bisherigen Ergebnisse zusammenfassen, ohne weiterzusuchen:
#   python3 analysis/min_samples_sweep.py --analyze-only

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import optuna
from tqdm import tqdm

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
sys.path.append(PROJECT_ROOT)

from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.analysis.backtester import run_backtest
from dnabot.genome.scoring import breakeven_winrate
from dnabot.genome.alphabet_store import resolve_alphabet, resolve_rr_ratio
from scan_and_learn import load_settings, load_secrets, resolve_history_days

# force=True: scan_and_learn.py setzt beim Import bereits sein eigenes
# basicConfig(INFO) -- ohne force wuerde unser WARNING-Level ignoriert
# (Pythons logging.basicConfig wirkt nur beim ERSTEN Aufruf) und jeder
# einzelne Coin-Download/Backtest-Log von backtester.py/exchange.py wuerde
# den Fortschrittsbalken zuspammen.
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s: %(message)s', force=True)
logger = logging.getLogger('min_samples_sweep')
logger.setLevel(logging.INFO)
optuna.logging.set_verbosity(optuna.logging.WARNING)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
STORAGE_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'min_samples_optuna.db')
RESULTS_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'min_samples_sweep.json')

MIN_SAMPLES_RANGE = (1, 10)  # weiter verengt (2026-08-14, VPS-Lauf 1h+2h/22 Coins):
                             # beide bisher abgeschlossenen Timeframes fanden ihr
                             # Optimum bei min_samples=3, deutlich innerhalb 1-10 --
                             # ein Bereich bis 15 verschwendet Trial-Budget in der
                             # toten Zone, ohne die Aussage zu verbessern.
N_TRIALS_DEFAULT = 20  # weiter reduziert -- 30m zeigte bei 30/40 Trials bereits ein
                       # stabiles Optimum (min_samples=1), 20 Trials bei 10 moeglichen
                       # Werten (~2x Abdeckung je Wert) reichen fuer die grossen,
                       # zeitfressenden Timeframes (30m/1h) mit vielen Kerzen pro Coin
MAX_DRAWDOWN_PCT = 30.0  # harte Nebenbedingung -- Trials darueber werden bestraft,
                         # unabhaengig davon wie gut ihr PnL sonst waere

# Mindest-Trade-Anzahl (gepoolt ueber alle Coins eines Timeframes), unter der
# ein Trial als statistisch nicht belastbar verworfen wird -- je kleiner der
# Timeframe, desto mehr Kerzen/Gelegenheiten stehen zur Verfuegung, also auch
# eine hoehere Anforderung. Referenzpunkt: 22 Coins x 5 Timeframes (110
# Strategien) sollen zusammen > 200 Trades/Jahr ergeben -- diese Floors sind
# bewusst niedriger als der Zielwert (sie sollen nur eindeutigen Unsinn
# rausfiltern, nicht das Optimum vorwegnehmen).
MIN_TRADES_BY_TIMEFRAME = {
    '15m': 40,
    '30m': 30,
    '1h':  20,
    '2h':  15,
    '4h':  10,
    '6h':  8,
    '8h':  6,
    '12h': 5,
    '1d':  4,
}
DEFAULT_MIN_TRADES = 10


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _date_range(history_days: int):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=history_days)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


def make_objective(dfs_by_market: dict, timeframe: str, db: GenomeDB,
                    base_genome_cfg: dict, settings: dict):
    def objective(trial: optuna.Trial) -> float:
        min_samples = trial.suggest_int('min_samples', *MIN_SAMPLES_RANGE)
        total_trades, total_pnl, total_wins = 0, 0.0, 0
        worst_dd = 0.0
        for market, df in dfs_by_market.items():
            # RR-Ratio ebenfalls PRO PAIR (analysis/alphabet_optimizer.py sucht
            # sie gemeinsam mit dem Alphabet) -- min_winrate muss konsistent
            # aus DERSELBEN rr_ratio abgeleitet werden, nicht aus dem globalen
            # Default, sonst passt die Aktivierungsschwelle nicht zur
            # tatsaechlich verwendeten TP-Distanz.
            pair_rr_ratio = resolve_rr_ratio(market, timeframe, settings)
            pair_genome_cfg = dict(
                base_genome_cfg,
                min_samples=min_samples,
                alphabet=resolve_alphabet(market, timeframe, settings),
                min_winrate=base_genome_cfg.get('min_winrate_explicit') or breakeven_winrate(pair_rr_ratio),
            )
            params = {
                # Alphabet-Override pro Pair (analysis/alphabet_optimizer.py) --
                # muss zum Alphabet passen, mit dem die Genome-DB fuer dieses
                # Pair befuellt wurde, sonst matcht hier nichts.
                'genome': pair_genome_cfg,
                'risk': {'rr_ratio': pair_rr_ratio},
            }
            try:
                result = run_backtest(df, market, timeframe, db, params,
                                       start_capital=1000, risk_per_trade_pct=1.0, fine_df=None)
            except Exception as e:
                logger.warning(f"Backtest-Fehler {market} ({timeframe}) min_samples={min_samples}: {e}")
                continue
            trades = result.get('trades', [])
            stats = result.get('stats', {})
            total_trades += len(trades)
            total_wins += sum(1 for t in trades if t.get('pnl_usdt', 0) > 0)
            total_pnl += sum(t.get('pnl_usdt', 0) for t in trades)
            worst_dd = max(worst_dd, stats.get('max_drawdown_pct', 0.0))

        trial.set_user_attr('total_trades', total_trades)
        trial.set_user_attr('total_wins', total_wins)
        trial.set_user_attr('total_pnl', round(total_pnl, 2))
        trial.set_user_attr('worst_drawdown_pct', round(worst_dd, 2))

        min_trades_required = MIN_TRADES_BY_TIMEFRAME.get(timeframe, DEFAULT_MIN_TRADES)
        if total_trades < min_trades_required:
            # Zu wenig Daten, um PnL sinnvoll zu bewerten -- klar unattraktiv
            # machen, statt Optuna auf "nie handeln" (PnL=0) konvergieren zu lassen.
            return -1e6 + total_trades  # leichte Praeferenz fuer "naeher an auswertbar"

        if worst_dd > MAX_DRAWDOWN_PCT:
            # Harte DD-Nebenbedingung verletzt -- klar unattraktiver als jede
            # zulaessige Loesung, aber mit Gradient (je naeher an der Grenze,
            # desto weniger schlecht), damit Optuna zurueck in Richtung
            # zulaessiger Werte lernen kann statt einer flachen Klippe.
            return -1e5 - (worst_dd - MAX_DRAWDOWN_PCT) * 100

        return total_pnl
    return objective


def run_sweep(timeframe_filter: str = None, n_trials: int = N_TRIALS_DEFAULT):
    settings = load_settings()
    secrets = load_secrets()
    accounts = secrets.get('dnabot', [])
    if not accounts:
        logger.critical("Kein 'dnabot'-Account in secret.json gefunden.")
        sys.exit(1)
    exchange = Exchange(accounts[0])
    db = GenomeDB(DB_PATH)

    pairs = db.get_all_market_pairs()
    if timeframe_filter:
        pairs = [p for p in pairs if p[1] == timeframe_filter]
    if not pairs:
        logger.critical("Keine passenden Paare in genome.db gefunden -- erst scannen.")
        sys.exit(1)

    genome_cfg = settings.get('genome_settings', {})
    # min_winrate_explicit: nur gesetzt wenn User es vorgibt -- dann gilt das
    # ueberall unveraendert. Sonst wird es PRO PAIR aus dessen eigener
    # rr_ratio abgeleitet (siehe make_objective()), nicht einmalig hier.
    min_winrate_explicit = genome_cfg.get('min_winrate')
    base_genome_cfg = {
        'min_score': genome_cfg.get('min_score', 0.08),
        'min_winrate_explicit': min_winrate_explicit,
        'sequence_lengths': genome_cfg.get('sequence_lengths', [4, 5, 6]),
        'half_life_days': genome_cfg.get('half_life_days', 180.0),
        'allowed_regimes': genome_cfg.get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL']),
    }

    by_tf = defaultdict(list)
    for market, tf in pairs:
        by_tf[tf].append(market)

    os.makedirs(os.path.dirname(STORAGE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"  min_samples-Sweep: {len(by_tf)} Timeframe(s), {len(pairs)} Coin/TF-Paare gesamt")
    print(f"{'=' * 60}")

    sweep_start = time.time()
    tf_durations = []
    timeframes_sorted = sorted(by_tf.items())

    for tf_idx, (tf, markets) in enumerate(timeframes_sorted):
        tf_start = time.time()
        elapsed_total = tf_start - sweep_start
        remaining_tf = len(timeframes_sorted) - tf_idx
        eta_str = ""
        if tf_durations:
            avg = sum(tf_durations) / len(tf_durations)
            eta_str = f" | ETA restliche {remaining_tf} Timeframes: ~{_fmt_duration(avg * remaining_tf)}"
        print(
            f"\n--- [{tf_idx + 1}/{len(timeframes_sorted)}] Timeframe {tf} "
            f"| bisher gelaufen: {_fmt_duration(elapsed_total)}{eta_str} ---"
        )

        dfs = {}
        history_days = resolve_history_days(tf, None)
        for market in tqdm(markets, desc=f"{tf}: Marktdaten laden", unit="coin"):
            try:
                df = exchange.fetch_historical_ohlcv(market, tf, *_date_range(history_days))
            except Exception as e:
                logger.error(f"Download fehlgeschlagen fuer {market} ({tf}): {e}")
                continue
            if df is not None and not df.empty and len(df) >= 100:
                dfs[market] = df
            else:
                logger.warning(f"Zu wenig Daten fuer {market} ({tf}) -- ueberspringe.")

        if not dfs:
            logger.warning(f"Keine verwertbaren Daten fuer Timeframe {tf} -- ueberspringe.")
            continue

        study = optuna.create_study(
            study_name=f"min_samples_{tf}",
            storage=f"sqlite:///{STORAGE_PATH}",
            load_if_exists=True,
            direction='maximize',
        )
        remaining = max(0, n_trials - len(study.trials))
        if remaining > 0:
            with tqdm(total=remaining, desc=f"{tf}: Optuna-Trials ({len(dfs)} Coins)", unit="trial") as pbar:
                def _progress(study: optuna.Study, trial: optuna.trial.FrozenTrial):
                    pbar.update(1)
                    try:
                        best = study.best_trial
                        if best.value is not None and best.value > -1e4:
                            pbar.set_postfix(
                                min_samples=best.params.get('min_samples'),
                                pnl=f"{best.user_attrs.get('total_pnl', best.value):+.1f}",
                            )
                    except ValueError:
                        pass  # noch kein abgeschlossener Trial
                study.optimize(
                    make_objective(dfs, tf, db, base_genome_cfg, settings),
                    n_trials=remaining,
                    callbacks=[_progress],
                )
        else:
            print(f"{tf}: bereits {len(study.trials)} Trials vorhanden -- ueberspringe.")

        min_trades_required = MIN_TRADES_BY_TIMEFRAME.get(tf, DEFAULT_MIN_TRADES)
        feasible = [
            t for t in study.trials
            if t.value is not None
            and t.user_attrs.get('total_trades', 0) >= min_trades_required
            and t.user_attrs.get('worst_drawdown_pct', 999) <= MAX_DRAWDOWN_PCT
        ]
        if feasible:
            best = max(feasible, key=lambda t: t.value)
            logger.info(
                f"  -> Bester min_samples fuer {tf}: {best.params['min_samples']} | "
                f"PnL {best.user_attrs.get('total_pnl', best.value):+.2f} USDT | "
                f"Trades {best.user_attrs.get('total_trades', '?')} | "
                f"MaxDD {best.user_attrs.get('worst_drawdown_pct', '?')}% | "
                f"({len(study.trials)} Trials, {len(feasible)} zulaessig, {len(dfs)} Coins)"
            )
        else:
            logger.warning(
                f"  -> {tf}: keine zulaessigen Trials in {len(study.trials)} Versuchen "
                f"(zu wenig Trades oder MaxDD > {MAX_DRAWDOWN_PCT}% ueberall, {len(dfs)} Coins)"
            )

        tf_durations.append(time.time() - tf_start)

    print(f"\n{'=' * 60}")
    print(f"  Sweep komplett abgeschlossen in {_fmt_duration(time.time() - sweep_start)}")
    print(f"{'=' * 60}")

    print_summary()


def print_summary():
    if not os.path.exists(STORAGE_PATH):
        print("Keine Optuna-Ergebnisse gefunden -- erst den Sweep laufen lassen.")
        return

    study_summaries = optuna.study.get_all_study_summaries(storage=f"sqlite:///{STORAGE_PATH}")
    if not study_summaries:
        print("Keine Studien in der Optuna-Storage gefunden.")
        return

    print(f"\n{'=' * 78}")
    print("  MIN_SAMPLES SWEEP (Optuna) -- ZUSAMMENFASSUNG (pro Timeframe)")
    print(f"{'=' * 78}")

    results = {}
    for summary in sorted(study_summaries, key=lambda s: s.study_name):
        tf = summary.study_name.replace('min_samples_', '')
        study = optuna.load_study(study_name=summary.study_name, storage=f"sqlite:///{STORAGE_PATH}")
        # "Gueltig" = beide Nebenbedingungen erfuellt (genug Trades, DD <= Limit) --
        # nicht ueber den Penalty-Wert selbst filtern, da die DD-Strafzone
        # (~-1e5) und die Zu-wenig-Trades-Strafzone (~-1e6) unterschiedlich
        # tief liegen und ein einzelner Schwellenwert das nicht sauber trennt.
        min_trades_required = MIN_TRADES_BY_TIMEFRAME.get(tf, DEFAULT_MIN_TRADES)
        valid_trials = [
            t for t in study.trials
            if t.value is not None
            and t.user_attrs.get('total_trades', 0) >= min_trades_required
            and t.user_attrs.get('worst_drawdown_pct', 999) <= MAX_DRAWDOWN_PCT
        ]
        if not valid_trials:
            print(f"\n  --- {tf} --- : keine zulaessigen Trials (zu wenig Trades oder MaxDD > {MAX_DRAWDOWN_PCT}% ueberall)")
            continue
        best = max(valid_trials, key=lambda t: t.value)
        print(f"\n  --- {tf} --- ({len(study.trials)} Trials, {len(valid_trials)} zulaessig)")
        print(f"  Bester min_samples: {best.params['min_samples']}")
        print(f"  PnL:    {best.user_attrs.get('total_pnl', best.value):+.2f} USDT")
        print(f"  Trades: {best.user_attrs.get('total_trades', '?')}")
        print(f"  MaxDD:  {best.user_attrs.get('worst_drawdown_pct', '?')}%")
        wins = best.user_attrs.get('total_wins')
        trades = best.user_attrs.get('total_trades')
        if wins is not None and trades:
            print(f"  WR:     {wins / trades:.1%}")
        results[tf] = {
            'min_samples': best.params['min_samples'],
            'pnl_usdt': best.user_attrs.get('total_pnl', best.value),
            'trades': best.user_attrs.get('total_trades'),
            'max_drawdown_pct': best.user_attrs.get('worst_drawdown_pct'),
            'n_trials': len(study.trials),
        }

    with open(RESULTS_PATH, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    total_trades_all = sum(r.get('trades', 0) or 0 for r in results.values())
    print(f"\n{'-' * 78}")
    print(f"  Trades gesamt (alle Timeframes, beste Werte): {total_trades_all}")
    print(f"  Referenz: bei vollem 22-Coin-Pool ueber alle Timeframes werden > 200")
    print(f"  Trades/Jahr erwartet -- deutlich weniger deutet auf zu wenige gescannte")
    print(f"  Coins/Timeframes ODER zu strenge min_samples-Werte hin.")

    print(f"\n{'=' * 78}")
    print(f"  Ergebnisse gespeichert: {RESULTS_PATH}")
    print(f"{'=' * 78}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="min_samples_to_activate Optuna-Sweep pro Timeframe")
    parser.add_argument('--timeframe', type=str, default=None,
                         help="Nur diesen Timeframe testen (z.B. 6h). Ohne Angabe: alle in genome.db vorhandenen.")
    parser.add_argument('--n-trials', type=int, default=N_TRIALS_DEFAULT,
                         help=f"Optuna-Trials pro Timeframe (Standard: {N_TRIALS_DEFAULT})")
    parser.add_argument('--analyze-only', action='store_true',
                         help="Nur bestehende Ergebnisse zusammenfassen, keinen neuen Sweep starten")
    args = parser.parse_args()

    if args.analyze_only:
        print_summary()
    else:
        run_sweep(timeframe_filter=args.timeframe, n_trials=args.n_trials)
