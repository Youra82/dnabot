#!/usr/bin/env python3
# analysis/alphabet_optimizer.py
#
# Sucht PRO (Coin, Timeframe) ein eigenes Encoder-Alphabet (encoder.py::
# DEFAULT_ALPHABET-Overrides -- Body-/Wick-/Volumen-Schwellwerte, die eine
# Kerze zu einem Gen-Buchstaben klassifizieren) per Optuna (TPE-Sampler).
#
# Hintergrund: Ein erster Single-Pair-Test (ADA/USDT 30m, reines In-Sample-
# Fitting ueber den vollen Zeitraum) zeigte Calmar -0.17 -> +4.71. Das war
# aber kein belastbarer Beweis -- Discovery UND Backtest liefen auf
# demselben Zeitraum, den Optuna gleichzeitig optimiert hat (klassisches
# Overfitting-Setup). Dieses Skript validiert sauber mit einem echten
# In-Sample/Out-of-Sample-Split:
#
#   1. Pro Pair: OHLCV EINMAL laden (wie min_samples_sweep.py).
#   2. Chronologischer Split bei IS_FRACTION (Standard 70%): die ersten
#      IS_FRACTION der Kerzen sind In-Sample (das sieht Optuna), der Rest
#      ist Out-of-Sample (fliesst NIE in die Zielfunktion ein).
#   3. Pro Trial: Discovery + Backtest laufen ueber den GESAMTEN df (das
#      Genome-System ist ohnehin point-in-time -- get_genome_as_of()
#      verhindert Hindsight-Bias pro Kerze, siehe database.py). Die
#      resultierende Trade-Liste wird danach nach entry_time in IS/OOS
#      gesplittet und JEWEILS EIGENSTAENDIG neu simuliert (wie
#      param_optimizer.py::simulate_trades) -- so verzerrt die im Volllauf
#      interleavte Equity-Kurve nicht die getrennte IS-/OOS-Bewertung.
#      Zielfunktion sieht NUR die IS-Metriken.
#   4. Bestbewerteter Trial: IS- UND OOS-Metriken werden beide reportet.
#      Nur wenn die OOS-Calmar das (durch denselben Split-Prozess laufende)
#      Baseline-Alphabet UEBERTRIFFT und OOS-PnL positiv ist, gilt der Fund
#      als "bestaetigt". Sonst: Ist-Zustand behalten, klar so markiert.
#   5. Ergebnis wird NICHT automatisch in settings.json geschrieben --
#      wie param_optimizer.py fragt das Skript interaktiv nach und schlaegt
#      nur bestaetigte Pairs vor.
#
# Separate Test-DB (artifacts/db/alphabet_optuna_test.db) -- die echte
# genome.db wird von diesem Skript nie angefasst.
#
# Ausfuehrung:
#   python3 analysis/alphabet_optimizer.py --symbol ADA/USDT:USDT --timeframe 30m --n-trials 30
#   python3 analysis/alphabet_optimizer.py --all-scan-pairs --n-trials 25   (alle scan_settings-Pairs)
#   python3 analysis/alphabet_optimizer.py --analyze-only                  (nur bisherige Ergebnisse zeigen)

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import optuna
from tqdm import tqdm

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
sys.path.append(PROJECT_ROOT)

from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.genome.discovery import discover_genomes
from dnabot.genome.encoder import DEFAULT_ALPHABET
from dnabot.genome.alphabet_store import alphabet_hash
from dnabot.genome.scoring import breakeven_winrate
from dnabot.analysis.backtester import run_backtest
from scan_and_learn import load_settings, load_secrets, resolve_history_days, resolve_discovery_horizon

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s: %(message)s', force=True)
logger = logging.getLogger('alphabet_optimizer')
logger.setLevel(logging.INFO)
optuna.logging.set_verbosity(optuna.logging.WARNING)

TEST_DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'alphabet_optuna_test.db')
STORAGE_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'alphabet_optuna.db')
RESULTS_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'alphabet_sweep.json')

IS_FRACTION = 0.70          # vorderer Teil der Historie: Alphabet-Suche (In-Sample)
                             # hinterer Teil (0.70-1.0): reine Validierung (Out-of-Sample)
N_TRIALS_DEFAULT = 30
MIN_IS_TRADES_DEFAULT = 15  # unter dieser Trade-Zahl im IS-Fenster ist der
                             # Vergleich zu verrauscht, um belastbar zu sein
MIN_OOS_TRADES_DEFAULT = 10  # Bestaetigung erfordert zusaetzlich genug OOS-
                              # Trades -- ohne diese Schwelle kann ein Pair mit
                              # z.B. nur 5 OOS-Trades "bestaetigt" werden, obwohl
                              # das Ergebnis genauso gut Zufall sein koennte (das
                              # war ohne diese Schwelle live an BTC/USDT 4h zu
                              # sehen: 5 OOS-Trades reichten fuer "bestaetigt").
MAX_DD_PCT = 30.0            # weich bestraft (Gradient Richtung Grenze), nicht hart verworfen

_EPS = 1e-9


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


def _study_name(market: str, timeframe: str) -> str:
    safe = market.replace('/', '').replace(':', '')
    return f"alphabet_{safe}_{timeframe}"


def reset_test_db(db: GenomeDB):
    db._conn.execute("DELETE FROM genomes")
    db._conn.execute("DELETE FROM genome_occurrences")
    db._conn.execute("DELETE FROM scan_log")
    db._conn.commit()


def calmar(stats: dict) -> float:
    pnl = stats.get('total_pnl_pct', 0.0)
    dd = stats.get('max_drawdown_pct', 0.0)
    return pnl / dd if dd > 0 else pnl


def _simulate_subset(trades: list, capital: float, risk_pct: float, leverage: int = 1) -> dict:
    """
    Rekonstruiert eine EIGENSTAENDIGE Equity-Kurve fuer eine Trade-Teilmenge
    (IS oder OOS) -- wie param_optimizer.py::simulate_trades. Noetig, weil
    run_backtest() eine EINZIGE durchlaufende Equity-Kurve ueber IS+OOS
    zusammen erzeugt (Positionsgroesse haengt von der Equity zum jeweiligen
    Zeitpunkt ab); wuerde man diese direkt splitten, wuerde die IS/OOS-
    Bewertung durch die jeweils andere Teilmenge verzerrt.
    """
    equity = capital
    peak = equity
    max_dd = 0.0
    wins = losses = timeouts = 0
    for t in sorted(trades, key=lambda x: x['entry_time']):
        sl_pct = max(t.get('sl_pct', 1.0), 0.01)
        risk_amount = min(equity * (risk_pct / 100.0),
                          equity * max(leverage, 1) * (sl_pct / 100.0))
        pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        equity += pnl
        outcome = t.get('outcome')
        if outcome == 'WIN':
            wins += 1
        elif outcome == 'LOSS':
            losses += 1
        else:
            timeouts += 1
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    total = len(trades)
    pnl_pct = (equity - capital) / capital * 100.0 if total else 0.0
    return {
        'total_trades': total, 'wins': wins, 'losses': losses, 'timeouts': timeouts,
        'win_rate': wins / total if total else 0.0,
        'total_pnl_pct': pnl_pct, 'max_drawdown_pct': max_dd,
        'final_equity': equity,
    }


def load_genome_cfg(settings: dict):
    genome_cfg_raw = settings.get('genome_settings', {})
    risk_cfg_raw = settings.get('risk_settings', {})
    scan_cfg_raw = settings.get('scan_settings', {})
    rr_ratio = risk_cfg_raw.get('rr_ratio', 2.0)
    genome_cfg = {
        'min_score': genome_cfg_raw.get('min_score', 0.08),
        'min_winrate': genome_cfg_raw.get('min_winrate') or breakeven_winrate(rr_ratio),
        'sequence_lengths': genome_cfg_raw.get('sequence_lengths', [4, 5, 6]),
        'half_life_days': genome_cfg_raw.get('half_life_days', 180.0),
        'allowed_regimes': genome_cfg_raw.get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL']),
        'min_samples': scan_cfg_raw.get('min_samples_to_activate', 2),
    }
    risk_cfg = {
        'rr_ratio': rr_ratio,
        'risk_per_entry_pct': risk_cfg_raw.get('risk_per_entry_pct', 1.0),
        'leverage': int(risk_cfg_raw.get('leverage', 1)),
    }
    return genome_cfg, risk_cfg


def run_alphabet_trial(df: pd.DataFrame, db: GenomeDB, market: str, timeframe: str,
                        alphabet: dict, genome_cfg: dict, risk_cfg: dict,
                        discovery_horizon: int, split_ts, capital: float):
    """Ein voller Discovery+Backtest-Durchlauf fuer EIN Alphabet -- gibt
    (is_stats, oos_stats) zurueck."""
    reset_test_db(db)
    with db.batch_writes():
        discover_genomes(
            df=df, market=market, timeframe=timeframe, db=db,
            sequence_lengths=genome_cfg['sequence_lengths'],
            discovery_horizon=discovery_horizon,
            rr_ratio=risk_cfg['rr_ratio'],
            alphabet=alphabet,
            alphabet_hash=alphabet_hash(alphabet),
        )

    bt_params = {
        'genome': {
            'min_score': genome_cfg['min_score'],
            'min_winrate': genome_cfg['min_winrate'],
            'sequence_lengths': genome_cfg['sequence_lengths'],
            'min_samples': genome_cfg['min_samples'],
            'half_life_days': genome_cfg['half_life_days'],
            'allowed_regimes': genome_cfg['allowed_regimes'],
            'alphabet': alphabet,
        },
        'risk': {'rr_ratio': risk_cfg['rr_ratio']},
    }
    result = run_backtest(
        df=df, market=market, timeframe=timeframe, db=db, params=bt_params,
        start_capital=capital, risk_per_trade_pct=risk_cfg['risk_per_entry_pct'],
        leverage=risk_cfg['leverage'], fine_df=None,
    )
    trades = result.get('trades', [])
    is_trades = [t for t in trades if pd.Timestamp(t['entry_time']) < split_ts]
    oos_trades = [t for t in trades if pd.Timestamp(t['entry_time']) >= split_ts]

    is_stats = _simulate_subset(is_trades, capital, risk_cfg['risk_per_entry_pct'], risk_cfg['leverage'])
    oos_stats = _simulate_subset(oos_trades, capital, risk_cfg['risk_per_entry_pct'], risk_cfg['leverage'])
    return is_stats, oos_stats


def make_objective(df, db, market, timeframe, genome_cfg, risk_cfg,
                    discovery_horizon, split_ts, capital, min_is_trades):
    def objective(trial: optuna.Trial) -> float:
        body_small = trial.suggest_float('body_small', 0.10, 0.45)
        body_large = trial.suggest_float('body_large', body_small + 0.15, 1.30)
        vol_mult = trial.suggest_float('vol_mult', 0.5, 2.0)
        wick_body_ratio = trial.suggest_float('wick_body_ratio', 0.2, 1.0)
        wick_doji_ratio = trial.suggest_float('wick_doji_ratio', 0.1, 0.5)
        vol_rel_mult = trial.suggest_float('vol_rel_mult', 0.7, 2.0)

        alphabet = dict(
            body_small=body_small, body_large=body_large, vol_mult=vol_mult,
            wick_body_ratio=wick_body_ratio, wick_doji_ratio=wick_doji_ratio,
            vol_rel_mult=vol_rel_mult,
        )
        is_baseline = all(abs(alphabet[k] - DEFAULT_ALPHABET[k]) < _EPS for k in DEFAULT_ALPHABET)

        is_stats, oos_stats = run_alphabet_trial(
            df, db, market, timeframe, alphabet, genome_cfg, risk_cfg,
            discovery_horizon, split_ts, capital,
        )
        trial.set_user_attr('is_stats', is_stats)
        trial.set_user_attr('oos_stats', oos_stats)
        trial.set_user_attr('alphabet', alphabet)
        trial.set_user_attr('is_baseline', is_baseline)

        n_trades = is_stats.get('total_trades', 0)
        if n_trades < min_is_trades:
            return -1e6 + n_trades

        dd = is_stats.get('max_drawdown_pct', 0.0)
        if dd > MAX_DD_PCT:
            return -1e5 - (dd - MAX_DD_PCT)

        return calmar(is_stats)
    return objective


def run_pair(exchange, db, market: str, timeframe: str, settings: dict,
             n_trials: int, min_is_trades: int, min_oos_trades: int):
    genome_cfg, risk_cfg = load_genome_cfg(settings)
    history_days = resolve_history_days(timeframe, None)
    discovery_horizon = resolve_discovery_horizon(timeframe, None)
    capital = settings.get('optimization_settings', {}).get('start_capital', 1000.0)

    df = exchange.fetch_historical_ohlcv(market, timeframe, *_date_range(history_days))
    if df is None or df.empty or len(df) < 200:
        logger.warning(f"Zu wenig Daten fuer {market} ({timeframe}) -- ueberspringe.")
        return None

    split_idx = int(len(df) * IS_FRACTION)
    split_ts = df.index[split_idx]
    logger.info(
        f"{market} ({timeframe}): {len(df)} Kerzen | IS bis {split_ts.date()} "
        f"({split_idx} Kerzen) | OOS ab {split_ts.date()} ({len(df) - split_idx} Kerzen)"
    )

    study = optuna.create_study(
        study_name=_study_name(market, timeframe),
        storage=f"sqlite:///{STORAGE_PATH}",
        load_if_exists=True,
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    if len(study.trials) == 0:
        study.enqueue_trial(dict(DEFAULT_ALPHABET))

    remaining = max(0, n_trials - len(study.trials))
    if remaining > 0:
        with tqdm(total=remaining, desc=f"{market} {timeframe}", unit="trial") as pbar:
            def _progress(study, trial):
                pbar.update(1)
            study.optimize(
                make_objective(df, db, market, timeframe, genome_cfg, risk_cfg,
                                discovery_horizon, split_ts, capital, min_is_trades),
                n_trials=remaining,
                callbacks=[_progress],
            )
    else:
        print(f"{market} ({timeframe}): bereits {len(study.trials)} Trials -- ueberspringe.")

    baseline_trial = next((t for t in study.trials if t.user_attrs.get('is_baseline')), None)
    if baseline_trial is None or baseline_trial.value is None:
        logger.warning(f"{market} ({timeframe}): kein Baseline-Trial gefunden -- werte separat aus.")
        b_is, b_oos = run_alphabet_trial(df, db, market, timeframe, dict(DEFAULT_ALPHABET),
                                          genome_cfg, risk_cfg, discovery_horizon, split_ts, capital)
    else:
        b_is = baseline_trial.user_attrs['is_stats']
        b_oos = baseline_trial.user_attrs['oos_stats']

    valid = [t for t in study.trials if t.value is not None and t.value > -1e4]
    if not valid:
        print(f"  {market} ({timeframe}): keine gueltigen Trials (zu wenig IS-Trades oder DD > {MAX_DD_PCT}%).")
        return None

    best = max(valid, key=lambda t: t.value)
    best_is = best.user_attrs['is_stats']
    best_oos = best.user_attrs['oos_stats']

    confirmed = (
        best_oos.get('total_trades', 0) >= min_oos_trades
        and calmar(best_oos) > calmar(b_oos)
        and best_oos.get('total_pnl_pct', 0.0) > 0.0
    )

    result = {
        'market': market, 'timeframe': timeframe,
        'n_trials': len(study.trials),
        'split_date': str(split_ts.date()),
        'baseline_params': dict(DEFAULT_ALPHABET),
        'baseline_is': b_is, 'baseline_oos': b_oos,
        'best_params': best.params,
        'best_is': best_is, 'best_oos': best_oos,
        'confirmed': confirmed,
    }

    mark = '[BESTAETIGT]' if confirmed else '[nicht bestaetigt -- Ist-Zustand behalten]'
    print(f"\n  --- {market} ({timeframe}) --- {mark}")
    print(f"  {'Metrik':<14}{'Baseline IS':>13}{'Best IS':>13}   |{'Baseline OOS':>14}{'Best OOS':>13}")
    print(f"  {'Trades':<14}{b_is['total_trades']:>13}{best_is['total_trades']:>13}   |"
          f"{b_oos['total_trades']:>14}{best_oos['total_trades']:>13}")
    print(f"  {'WinRate':<14}{b_is['win_rate']:>12.1%} {best_is['win_rate']:>12.1%}   |"
          f"{b_oos['win_rate']:>13.1%} {best_oos['win_rate']:>12.1%}")
    print(f"  {'PnL %':<14}{b_is['total_pnl_pct']:>+12.1f}%{best_is['total_pnl_pct']:>+12.1f}%   |"
          f"{b_oos['total_pnl_pct']:>+13.1f}%{best_oos['total_pnl_pct']:>+12.1f}%")
    print(f"  {'Calmar':<14}{calmar(b_is):>13.2f}{calmar(best_is):>13.2f}   |"
          f"{calmar(b_oos):>14.2f}{calmar(best_oos):>13.2f}")

    return result


def run_sweep(pairs: list, n_trials: int, min_is_trades: int, min_oos_trades: int):
    settings = load_settings()
    secrets = load_secrets()
    accounts = secrets.get('dnabot', [])
    if not accounts:
        logger.critical("Kein 'dnabot'-Account in secret.json gefunden.")
        sys.exit(1)
    exchange = Exchange(accounts[0])
    db = GenomeDB(TEST_DB_PATH)

    os.makedirs(os.path.dirname(STORAGE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)

    print(f"\n{'=' * 70}")
    print(f"  Alphabet-Optimizer: {len(pairs)} Pair(s) | {n_trials} Trials/Pair | "
          f"IS/OOS-Split {IS_FRACTION:.0%}/{1 - IS_FRACTION:.0%}")
    print(f"{'=' * 70}")

    results = {}
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH) as f:
                results = json.load(f)
        except Exception:
            results = {}

    sweep_start = time.time()
    for idx, (market, timeframe) in enumerate(pairs):
        elapsed = time.time() - sweep_start
        print(f"\n[{idx + 1}/{len(pairs)}] {market} ({timeframe}) | bisher gelaufen: {_fmt_duration(elapsed)}")
        try:
            r = run_pair(exchange, db, market, timeframe, settings, n_trials, min_is_trades, min_oos_trades)
        except Exception as e:
            logger.error(f"Fehler bei {market} ({timeframe}): {e}", exc_info=True)
            continue
        if r is not None:
            results[f"{market}|{timeframe}"] = r
            with open(RESULTS_PATH, 'w') as f:
                json.dump(results, f, indent=2, default=str)

    db.close()

    print(f"\n{'=' * 70}")
    print(f"  Sweep abgeschlossen in {_fmt_duration(time.time() - sweep_start)}")
    print(f"  Ergebnisse gespeichert: {RESULTS_PATH}")
    print(f"{'=' * 70}")

    print_summary(results)
    offer_apply(results, settings)


def print_summary(results: dict):
    if not results:
        print("\nKeine Ergebnisse.")
        return
    print(f"\n{'=' * 70}")
    print("  ZUSAMMENFASSUNG")
    print(f"{'=' * 70}")
    for key, r in results.items():
        mark = 'OK ' if r.get('confirmed') else 'no '
        b_oos_calmar = calmar(r['baseline_oos'])
        best_oos_calmar = calmar(r['best_oos'])
        print(f"  {mark} {key:<28} OOS-Calmar: Baseline {b_oos_calmar:>7.2f} -> Best {best_oos_calmar:>7.2f} "
              f"(OOS-PnL {r['best_oos']['total_pnl_pct']:+.1f}%)")
    n_confirmed = sum(1 for r in results.values() if r.get('confirmed'))
    print(f"\n  {n_confirmed}/{len(results)} Pairs bestaetigt (OOS besser als Baseline UND OOS-PnL > 0).")


def offer_apply(results: dict, settings: dict):
    confirmed = {k: r for k, r in results.items() if r.get('confirmed')}
    if not confirmed:
        print("\nKeine bestaetigten Pairs -- settings.json bleibt unveraendert.")
        return
    print(f"\n{len(confirmed)} bestaetigte(s) Pair(s) koennen als Alphabet-Override uebernommen werden:")
    for key in confirmed:
        print(f"  - {key}")
    try:
        ans = input("\nIn settings.json uebernehmen (genome_settings.alphabet_by_pair)? (j/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n(nicht-interaktiv -- settings.json bleibt unveraendert)")
        return
    if ans not in ('j', 'ja', 'y', 'yes'):
        print("Abgebrochen -- settings.json bleibt unveraendert.")
        return

    settings_path = os.path.join(PROJECT_ROOT, 'settings.json')
    with open(settings_path) as f:
        s = json.load(f)
    by_pair = s.setdefault('genome_settings', {}).setdefault('alphabet_by_pair', {})
    for key, r in confirmed.items():
        market, timeframe = key.split('|')
        by_pair.setdefault(market, {})[timeframe] = r['best_params']
    with open(settings_path, 'w') as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    print(f"settings.json aktualisiert ({len(confirmed)} Pair(s)).")
    print("  WICHTIG: naechster scan_and_learn.py-Lauf erkennt die Alphabet-Aenderung automatisch")
    print("  und fuehrt fuer diese Pairs einen vollstaendigen Rescan durch (alte Genome werden geloescht).")


def resolve_pairs(args, settings: dict) -> list:
    if args.symbol and args.timeframe:
        return [(args.symbol, args.timeframe)]
    if args.all_scan_pairs:
        scan_cfg = settings.get('scan_settings', {})
        symbols = scan_cfg.get('symbols', [])
        timeframes = scan_cfg.get('timeframes', [])
        if not symbols or not timeframes:
            logger.critical("scan_settings.symbols/timeframes nicht gesetzt.")
            sys.exit(1)
        return [(s, t) for s in symbols for t in timeframes]
    # Default: aktive Live-Strategien
    active = settings.get('live_trading_settings', {}).get('active_strategies', [])
    pairs = [(s['symbol'], s['timeframe']) for s in active if s.get('active', True)]
    if not pairs:
        logger.critical("Keine Pairs gefunden -- --symbol/--timeframe, --all-scan-pairs oder active_strategies noetig.")
        sys.exit(1)
    return pairs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Pro-Pair Encoder-Alphabet-Optimierung (Optuna, IS/OOS-Split)")
    parser.add_argument('--symbol', type=str, default=None, help="Nur dieses Pair (mit --timeframe)")
    parser.add_argument('--timeframe', type=str, default=None)
    parser.add_argument('--all-scan-pairs', action='store_true',
                        help="Alle Pairs aus scan_settings.symbols x timeframes")
    parser.add_argument('--n-trials', type=int, default=N_TRIALS_DEFAULT)
    parser.add_argument('--min-is-trades', type=int, default=MIN_IS_TRADES_DEFAULT)
    parser.add_argument('--min-oos-trades', type=int, default=MIN_OOS_TRADES_DEFAULT)
    parser.add_argument('--analyze-only', action='store_true', help="Nur bestehende Ergebnisse zeigen")
    args = parser.parse_args()

    if args.analyze_only:
        results = {}
        if os.path.exists(RESULTS_PATH):
            with open(RESULTS_PATH) as f:
                results = json.load(f)
        print_summary(results)
        sys.exit(0)

    _settings = load_settings()
    _pairs = resolve_pairs(args, _settings)
    run_sweep(_pairs, args.n_trials, args.min_is_trades, args.min_oos_trades)
