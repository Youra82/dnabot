#!/usr/bin/env python3
# analysis/alphabet_optimizer.py
#
# Sucht PRO (Coin, Timeframe) ein eigenes Encoder-Alphabet (encoder.py::
# DEFAULT_ALPHABET-Overrides -- Body-/Wick-/Volumen-Schwellwerte, die eine
# Kerze zu einem Gen-Buchstaben klassifizieren) UND eine eigene RR-Ratio
# (Take-Profit-Distanz relativ zum strukturellen Stop) per Optuna (TPE-
# Sampler), gemeinsam in einem Trial. RR-Ratio ist auf 1.0-4.0 begrenzt --
# der Sweet Spot zwischen Mean-Reversion- und Trend-Following-Extremen
# (Breakeven-Winrate-Kurve WR=1/(1+RR), siehe scoring.py::breakeven_winrate,
# dieselbe Begrenzung wie in analysis/param_optimizer.py). Anders als die
# Alphabet-Schwellwerte (die nur beeinflussen WELCHE Muster gefunden
# werden) veraendert RR direkt die TP-Distanz und damit die noetige
# Ziel-Winrate jedes gefundenen Musters -- beides gemeinsam zu suchen
# erschliesst Kombinationen, die bei fixem RR nie sichtbar waeren.
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
#   4. Zielfunktion: moeglichst viele IS-Trades (nicht Calmar/PnL) -- die
#      meisten Pairs aktivieren mit dem Default-Alphabet kaum/keine Genome
#      (siehe evolver.py-Reports mit "Aktive Genome: 0"), was die ganze
#      Auswertung erst unmoeglich macht. Jeder gezaehlte Trade hat trotzdem
#      schon min_score/min_winrate/min_samples bestanden (siehe
#      get_genome_as_of()) -- "mehr Trades" heisst hier nicht "mehr
#      Rauschen", sondern "mehr bereits qualifizierte Signale finden". Eine
#      Drawdown-Schranke (MAX_DD_PCT) bleibt als Sicherheitsnetz bestehen.
#   5. Bestbewerteter Trial: IS- UND OOS-Metriken werden beide reportet. Nur
#      wenn die OOS-Trade-Zahl die Baseline UEBERTRIFFT, genug OOS-Trades
#      fuer eine belastbare Aussage vorliegen UND OOS-PnL positiv ist, gilt
#      der Fund als "bestaetigt". Sonst: Ist-Zustand behalten, klar markiert.
#   6. Ergebnis wird NICHT automatisch in settings.json geschrieben --
#      wie param_optimizer.py fragt das Skript interaktiv nach und schlaegt
#      nur bestaetigte Pairs vor (--auto-apply uebernimmt ohne Rueckfrage,
#      fuer den Aufruf aus run_pipeline.sh).
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

# alphabet_by_pair/rr_ratio_by_pair leben in einer eigenen Repo-Root-Datei,
# nicht mehr in settings.json (siehe alphabet_store.py) -- wuerden die fuer
# Menschen gedachte settings.json sonst mit hunderten Zahlen zumuellen.
ALPHABET_OVERRIDES_PATH = os.path.join(PROJECT_ROOT, 'alphabet_overrides.json')

from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.genome.discovery import discover_genomes
from dnabot.genome.encoder import DEFAULT_ALPHABET
from dnabot.genome.alphabet_store import alphabet_hash
from dnabot.genome.scoring import breakeven_winrate
from dnabot.analysis.backtester import run_backtest
from scan_and_learn import (
    load_settings, load_secrets, resolve_history_days, resolve_discovery_horizon,
    resolve_min_samples, get_min_samples_override,
)

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s: %(message)s', force=True)
logger = logging.getLogger('alphabet_optimizer')
logger.setLevel(logging.INFO)
optuna.logging.set_verbosity(optuna.logging.WARNING)

TEST_DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'alphabet_optuna_test.db')
STORAGE_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'alphabet_optuna.db')
RESULTS_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'alphabet_sweep.json')
# Nur lesend verwendet (resolve_full_pool_pairs()) -- Discovery/Backtest-Trials
# selbst laufen ausschliesslich gegen TEST_DB_PATH, nie gegen die echte DB.
PROD_DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')

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

RECHECK_AFTER_DAYS_DEFAULT = 30  # Nicht-bestaetigte Pairs werden erst nach
                             # dieser Sperrfrist erneut geprueft (Zeitstempel
                             # in alphabet_sweep.json::checked_at). Ohne das
                             # wuerde jeder Scheduler-Lauf ALLE nicht
                             # bestaetigten Pairs (typischerweise die
                             # Mehrheit) erneut voll optimieren -- fuer
                             # immer, jede Woche wieder mehrere Stunden.

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


def _study_name(market: str, timeframe: str, history_days: int) -> str:
    """history_days ist Teil des Study-Namens: aendert sich das Lookback-Fenster
    (z.B. HISTORY_DAYS_MAP angepasst), landen neue Trials automatisch in
    einer frischen Studie statt sich mit alten, auf einem anderen Datenfenster
    berechneten Trials (andere Kerzenzahl, anderer Split-Zeitpunkt) zu vermischen."""
    safe = market.replace('/', '').replace(':', '')
    return f"alphabet_{safe}_{timeframe}_{history_days}d"


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


def load_genome_cfg(settings: dict, timeframe: str):
    genome_cfg_raw = settings.get('genome_settings', {})
    risk_cfg_raw = settings.get('risk_settings', {})
    scan_cfg_raw = settings.get('scan_settings', {})
    rr_ratio = risk_cfg_raw.get('rr_ratio', 2.0)
    # min_winrate_explicit: nur gesetzt wenn der User es in settings.json
    # explizit vorgibt -- dann gilt das fuer JEDEN Trial unveraendert.
    # Fehlt es, wird min_winrate PRO TRIAL aus dessen eigenem rr_ratio
    # abgeleitet (siehe make_objective()), nicht einmalig hier aus dem
    # globalen rr_ratio -- sonst waere die Aktivierungsschwelle inkonsistent
    # zu der TP-Distanz, mit der ein Trial tatsaechlich simuliert.
    min_winrate_explicit = genome_cfg_raw.get('min_winrate')
    genome_cfg = {
        'min_score': genome_cfg_raw.get('min_score', 0.08),
        'min_winrate': min_winrate_explicit or breakeven_winrate(rr_ratio),
        'min_winrate_explicit': min_winrate_explicit,
        'sequence_lengths': genome_cfg_raw.get('sequence_lengths', [4, 5, 6]),
        'half_life_days': genome_cfg_raw.get('half_life_days', 180.0),
        'allowed_regimes': genome_cfg_raw.get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL']),
        # Dieselbe Aufloesung wie scan_and_learn.py/run_backtest.py (Prioritaet:
        # scan_settings.min_samples_by_timeframe[timeframe] -- z.B. per
        # analysis/min_samples_sweep.py optimiert -- > min_samples_to_activate
        # pauschal > MIN_SAMPLES_MAP-Default pro Timeframe). Vorher las diese
        # Funktion nur den pauschalen min_samples_to_activate-Wert und ignorierte
        # eine per-Timeframe-Optimierung komplett -- der Optimizer validierte
        # damit mit einer anderen Aktivierungsschwelle als die Produktions-
        # Pipeline tatsaechlich verwendet, was Confirmed-Pairs mit voneinander
        # abweichenden Trade-Zahlen/PnL zwischen Optimizer und echtem Backtest
        # erklaeren kann (siehe ETH: 113 Trades im Optimizer vs. 53 im Backtest).
        'min_samples': resolve_min_samples(
            timeframe, get_min_samples_override(scan_cfg_raw, timeframe)),
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
    base_rr_ratio = risk_cfg['rr_ratio']
    explicit_min_winrate = genome_cfg.get('min_winrate_explicit')

    def objective(trial: optuna.Trial) -> float:
        body_small = trial.suggest_float('body_small', 0.10, 0.45)
        body_large = trial.suggest_float('body_large', body_small + 0.15, 1.30)
        vol_mult = trial.suggest_float('vol_mult', 0.5, 2.0)
        wick_body_ratio = trial.suggest_float('wick_body_ratio', 0.2, 1.0)
        wick_doji_ratio = trial.suggest_float('wick_doji_ratio', 0.1, 0.5)
        vol_rel_mult = trial.suggest_float('vol_rel_mult', 0.7, 2.0)
        # RR-Ratio wird JOINT mit dem Alphabet gesucht, begrenzt auf den
        # "Sweet Spot" zwischen Mean-Reversion- und Trend-Following-Extremen
        # (Breakeven-Winrate-Kurve WR=1/(1+RR), siehe scoring.py::
        # breakeven_winrate() und param_optimizer.py, dieselbe Begrenzung).
        # Anders als die Alphabet-Schwellwerte (die nur beeinflussen WELCHE
        # Muster gefunden werden) veraendert RR direkt TP-Distanz und damit
        # die noetige Ziel-Winrate jedes gefundenen Musters.
        rr_ratio = trial.suggest_float('rr_ratio', 1.0, 4.0)

        alphabet = dict(
            body_small=body_small, body_large=body_large, vol_mult=vol_mult,
            wick_body_ratio=wick_body_ratio, wick_doji_ratio=wick_doji_ratio,
            vol_rel_mult=vol_rel_mult,
        )
        is_baseline = (
            all(abs(alphabet[k] - DEFAULT_ALPHABET[k]) < _EPS for k in DEFAULT_ALPHABET)
            and abs(rr_ratio - base_rr_ratio) < _EPS
        )

        # min_winrate haengt von rr_ratio ab (Breakeven + Puffer) -- muss pro
        # Trial neu abgeleitet werden, AUSSER explizit in settings.json
        # gesetzt (dann hat das wie ueberall sonst Vorrang).
        trial_genome_cfg = dict(genome_cfg)
        trial_genome_cfg['min_winrate'] = explicit_min_winrate or breakeven_winrate(rr_ratio)
        trial_risk_cfg = dict(risk_cfg, rr_ratio=rr_ratio)

        is_stats, oos_stats = run_alphabet_trial(
            df, db, market, timeframe, alphabet, trial_genome_cfg, trial_risk_cfg,
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

        # Ziel: moeglichst viele Trades -- die meisten Pairs aktivieren mit dem
        # Default-Alphabet kaum/keine Genome (zu wenig statistische Basis fuer
        # Discovery UND fuer die OOS-Validierung selbst). Jeder gezaehlte Trade
        # hat trotzdem schon min_score/min_winrate/min_samples bestanden (siehe
        # _find_best_signal()/get_genome_as_of()) -- "mehr Trades" heisst hier
        # nicht "mehr Rauschen durchlassen", sondern "mehr bereits qualifizierte
        # Signale finden". DD-Schranke oben bleibt als Sicherheitsnetz bestehen.
        return float(n_trades)
    return objective


def run_pair(exchange, db, market: str, timeframe: str, settings: dict,
             n_trials: int, min_is_trades: int, min_oos_trades: int):
    genome_cfg, risk_cfg = load_genome_cfg(settings, timeframe)
    # Dieselbe history_days wie scan_and_learn.py/run_backtest.py -- vorher
    # nutzte dieser Optimizer per HISTORY_MULTIPLIER=2.0 die doppelte Historie,
    # was Alphabet+RR-Kombinationen bestaetigte, die auf der tatsaechlich von
    # der Produktions-Pipeline genutzten (kuerzeren) Historie oft 0 Trades
    # ergaben -- die vielen extrem seltenen Gen-Sequenzen (siehe "Sparsity" in
    # den Projekt-Notizen) brauchten die zusaetzlichen Jahre, um point-in-time
    # ueberhaupt zweimal (min_samples) aufzutreten. Eine Bestaetigung ist nur
    # aussagekraeftig, wenn sie auf denselben Daten beruht, die live/backtest
    # tatsaechlich sehen.
    scan_cfg = settings.get('scan_settings', {})
    history_days = resolve_history_days(timeframe, scan_cfg.get('history_days'))
    discovery_horizon = resolve_discovery_horizon(timeframe, scan_cfg.get('discovery_horizon'))
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

    # Jeder Lauf startet frisch -- n_trials ist "N neue Trials ab jetzt", nicht
    # "Ziel-Gesamtzahl einer fortlaufenden Studie" (letzteres verwirrte beim
    # ersten echten Einsatz: 50 eingegeben, aber nur ~30 liefen, weil schon
    # ~20 aus einem frueheren Lauf mit demselben Pair/Fenster gespeichert
    # waren). Alte Studie fuer dieses Pair/Fenster wird verworfen.
    study_name = _study_name(market, timeframe, history_days)
    try:
        optuna.delete_study(study_name=study_name, storage=f"sqlite:///{STORAGE_PATH}")
    except KeyError:
        pass  # existierte noch nicht

    study = optuna.create_study(
        study_name=study_name,
        storage=f"sqlite:///{STORAGE_PATH}",
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.enqueue_trial(dict(DEFAULT_ALPHABET, rr_ratio=risk_cfg['rr_ratio']))

    with tqdm(total=n_trials, desc=f"{market} {timeframe}", unit="trial") as pbar:
        def _progress(study, trial):
            pbar.update(1)
        study.optimize(
            make_objective(df, db, market, timeframe, genome_cfg, risk_cfg,
                            discovery_horizon, split_ts, capital, min_is_trades),
            n_trials=n_trials,
            callbacks=[_progress],
        )

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

    # Bestaetigung folgt dem Such-Ziel: mehr OOS-Trades als die Baseline (das
    # eigentliche Ziel), plus zwei Sicherheitsnetze -- genug OOS-Trades fuer
    # eine belastbare Aussage ueberhaupt, und OOS-PnL nicht negativ (mehr
    # Trades ja, aber nicht garantiert Geld verlieren).
    confirmed = (
        best_oos.get('total_trades', 0) >= min_oos_trades
        and best_oos.get('total_trades', 0) > b_oos.get('total_trades', 0)
        and best_oos.get('total_pnl_pct', 0.0) > 0.0
    )

    # best.params ist ein flacher Optuna-Namespace (6 Alphabet-Keys + rr_ratio
    # gemischt) -- fuer settings.json muessen die getrennt werden: Alphabet
    # nach alphabet_by_pair, RR-Ratio nach rr_ratio_by_pair (siehe offer_apply()).
    best_rr_ratio = best.params.get('rr_ratio', risk_cfg['rr_ratio'])
    best_alphabet = {k: v for k, v in best.params.items() if k != 'rr_ratio'}

    result = {
        'market': market, 'timeframe': timeframe,
        'n_trials': len(study.trials),
        'split_date': str(split_ts.date()),
        'baseline_params': dict(DEFAULT_ALPHABET),
        'baseline_rr_ratio': risk_cfg['rr_ratio'],
        'baseline_is': b_is, 'baseline_oos': b_oos,
        'best_params': best_alphabet,
        'best_rr_ratio': best_rr_ratio,
        'best_is': best_is, 'best_oos': best_oos,
        'confirmed': confirmed,
        'checked_at': datetime.now(timezone.utc).isoformat(),
    }

    mark = '[BESTAETIGT]' if confirmed else '[nicht bestaetigt -- Ist-Zustand behalten]'
    print(f"\n  --- {market} ({timeframe}) --- {mark}")
    print(f"  RR-Ratio: Baseline {risk_cfg['rr_ratio']:.2f} -> Best {best_rr_ratio:.2f}")
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


def run_sweep(pairs: list, n_trials: int, min_is_trades: int, min_oos_trades: int,
              auto_apply: bool = False, skip_confirmed: bool = True,
              recheck_after_days: int = RECHECK_AFTER_DAYS_DEFAULT):
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

    existing_results = {}
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH) as f:
                existing_results = json.load(f)
        except Exception:
            existing_results = {}

    # Pairs ueberspringen, wenn entweder (a) bereits bestaetigtes Alphabet
    # (skip_confirmed) oder (b) erst kuerzlich geprueft wurde, egal mit
    # welchem Ergebnis (recheck_after_days) -- (b) fehlte urspruenglich:
    # nicht bestaetigte Pairs (typischerweise die MEHRHEIT, siehe BTC: nur
    # 2/5 Timeframes bestaetigt) wurden sonst bei JEDEM Scheduler-Lauf erneut
    # voll optimiert (mehrere Stunden pro Lauf), fuer immer -- "nicht
    # zweites Mal drueberbuegeln" galt nur fuer den Erfolgsfall.
    # --recheck-confirmed (skip_confirmed=False) ignoriert beide Sperren.
    if skip_confirmed:
        try:
            with open(ALPHABET_OVERRIDES_PATH) as f:
                by_pair = json.load(f).get('alphabet_by_pair', {})
        except Exception:
            by_pair = {}
        now = datetime.now(timezone.utc)
        kept = []
        skipped_confirmed = 0
        skipped_recent = 0
        for (market, timeframe) in pairs:
            if timeframe in by_pair.get(market, {}):
                skipped_confirmed += 1
                continue
            prev = existing_results.get(f"{market}|{timeframe}")
            checked_at = prev.get('checked_at') if prev else None
            if checked_at:
                try:
                    checked = datetime.fromisoformat(checked_at)
                    if (now - checked).days < recheck_after_days:
                        skipped_recent += 1
                        continue
                except (ValueError, TypeError):
                    pass
            kept.append((market, timeframe))
        if skipped_confirmed:
            print(f"  {skipped_confirmed} Pair(s) mit bereits bestaetigtem Alphabet uebersprungen.")
        if skipped_recent:
            print(f"  {skipped_recent} Pair(s) innerhalb der letzten {recheck_after_days} Tage bereits "
                  f"geprueft (nicht bestaetigt) -- uebersprungen.")
        if skipped_confirmed or skipped_recent:
            print("  (--recheck-confirmed erzwingt eine erneute Pruefung aller Pairs.)")
        pairs = kept

    if not pairs:
        print("\nAlle Pairs wurden bereits bestaetigt oder kuerzlich geprueft -- nichts zu tun.")
        return

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
    offer_apply(results, settings, auto_apply=auto_apply)


def print_summary(results: dict):
    if not results:
        print("\nKeine Ergebnisse.")
        return
    print(f"\n{'=' * 70}")
    print("  ZUSAMMENFASSUNG")
    print(f"{'=' * 70}")
    for key, r in results.items():
        mark = 'OK ' if r.get('confirmed') else 'no '
        b_trades = r['baseline_oos'].get('total_trades', 0)
        best_trades = r['best_oos'].get('total_trades', 0)
        best_oos_calmar = calmar(r['best_oos'])
        print(f"  {mark} {key:<28} OOS-Trades: Baseline {b_trades:>3} -> Best {best_trades:>3} "
              f"(OOS-PnL {r['best_oos']['total_pnl_pct']:+.1f}%, Calmar {best_oos_calmar:+.2f})")
    n_confirmed = sum(1 for r in results.values() if r.get('confirmed'))
    print(f"\n  {n_confirmed}/{len(results)} Pairs bestaetigt (OOS besser als Baseline UND OOS-PnL > 0).")


def offer_apply(results: dict, settings: dict, auto_apply: bool = False):
    confirmed = {k: r for k, r in results.items() if r.get('confirmed')}
    if not confirmed:
        print("\nKeine bestaetigten Pairs -- alphabet_overrides.json bleibt unveraendert.")
        return
    print(f"\n{len(confirmed)} bestaetigte(s) Pair(s) koennen als Alphabet+RR-Ratio-Override uebernommen werden:")
    for key, r in confirmed.items():
        print(f"  - {key} (RR-Ratio {r.get('baseline_rr_ratio', 2.0):.2f} -> {r.get('best_rr_ratio', 2.0):.2f})")

    if auto_apply:
        print("\n--auto-apply gesetzt -- uebernehme ohne Rueckfrage.")
    else:
        try:
            ans = input("\nIn alphabet_overrides.json uebernehmen? (j/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n(nicht-interaktiv -- alphabet_overrides.json bleibt unveraendert)")
            return
        if ans not in ('j', 'ja', 'y', 'yes'):
            print("Abgebrochen -- alphabet_overrides.json bleibt unveraendert.")
            return

    try:
        with open(ALPHABET_OVERRIDES_PATH) as f:
            overrides = json.load(f)
    except Exception:
        overrides = {}
    alphabet_by_pair = overrides.setdefault('alphabet_by_pair', {})
    rr_ratio_by_pair = overrides.setdefault('rr_ratio_by_pair', {})
    for key, r in confirmed.items():
        market, timeframe = key.split('|')
        alphabet_by_pair.setdefault(market, {})[timeframe] = r['best_params']
        rr_ratio_by_pair.setdefault(market, {})[timeframe] = r.get('best_rr_ratio', 2.0)
    with open(ALPHABET_OVERRIDES_PATH, 'w') as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)
    print(f"alphabet_overrides.json aktualisiert ({len(confirmed)} Pair(s), Alphabet + RR-Ratio).")
    print("  WICHTIG: naechster scan_and_learn.py-Lauf erkennt die Alphabet-Aenderung automatisch")
    print("  und fuehrt fuer diese Pairs einen vollstaendigen Rescan durch (alte Genome werden geloescht).")


def _env_override_pairs(settings: dict):
    """
    DNABOT_OVERRIDE_COINS/DNABOT_OVERRIDE_TFS -- dieselben Env-Vars, mit denen
    run_pipeline.sh die interaktive Coin/Timeframe-Auswahl an run_backtest.py
    durchreicht. Erlaubt run_pipeline.sh, denselben Pair-Pool fuer den
    Alphabet-Optimizer zu verwenden wie fuer Discovery/Backtest, ohne die
    Pair-Liste zweimal zu bauen. None wenn keine der beiden Vars gesetzt ist.
    """
    coins_raw = os.environ.get('DNABOT_OVERRIDE_COINS', '').strip()
    tfs_raw = os.environ.get('DNABOT_OVERRIDE_TFS', '').strip()
    if not coins_raw and not tfs_raw:
        return None

    def to_symbol(coin: str) -> str:
        coin = coin.strip().upper()
        return coin if '/' in coin else f"{coin}/USDT:USDT"

    # Fallback-Prioritaet MUSS scan_and_learn.py entsprechen: zuerst
    # scan_settings.symbols/timeframes (breiterer Discovery-Pool), erst dann
    # active_strategies (schmalere Live-Trading-Auswahl). War vorher nur auf
    # active_strategies gestuetzt -- bei leerem active_strategies (aber
    # vollem scan_settings-Pool) kam faelschlich nur der 1-Coin-Notfall-
    # Default statt des eigentlich vorhandenen Pools raus.
    scan_cfg = settings.get('scan_settings', {})
    active = settings.get('live_trading_settings', {}).get('active_strategies', [])
    active_coins = list(dict.fromkeys(s['symbol'] for s in active if s.get('symbol')))
    active_tfs = list(dict.fromkeys(s['timeframe'] for s in active if s.get('timeframe')))
    auto_coins = scan_cfg.get('symbols') or active_coins or ['BTC/USDT:USDT']
    auto_tfs = scan_cfg.get('timeframes') or active_tfs or ['4h']

    coins = [to_symbol(c) for c in coins_raw.split()] if coins_raw else auto_coins
    tfs = [t.strip() for t in tfs_raw.split()] if tfs_raw else auto_tfs
    return [(c, t) for c in coins for t in tfs]


def resolve_full_pool_pairs(settings: dict) -> list:
    """
    Pool-Aufloesung fuer --all-scan-pairs / auto_optimizer_scheduler.py --
    MUSS dieselbe Prioritaet wie scan_and_learn.py haben (siehe dortiger Fix
    2026-08-15): scan_all_db_pairs (wenn die echte Genome-DB schon Paare
    hat) vor der statischen scan_settings-Liste, sonst active_strategies,
    sonst Einzel-Default. Sonst wuerde der Alphabet-Optimizer einen anderen
    Pool optimieren als scan_and_learn.py danach tatsaechlich scannt.
    """
    scan_cfg = settings.get('scan_settings', {})
    if scan_cfg.get('scan_all_db_pairs', False) and os.path.exists(PROD_DB_PATH):
        _db = GenomeDB(PROD_DB_PATH)
        db_pairs = _db.get_all_market_pairs()
        _db.close()
        if db_pairs:
            return db_pairs

    symbols = scan_cfg.get('symbols')
    timeframes = scan_cfg.get('timeframes')
    if symbols and timeframes:
        return [(s, t) for s in symbols for t in timeframes]

    active = settings.get('live_trading_settings', {}).get('active_strategies', [])
    pairs = [(s['symbol'], s['timeframe']) for s in active if s.get('active', True)]
    if pairs:
        return pairs

    return [('BTC/USDT:USDT', '4h')]


def resolve_pairs(args, settings: dict) -> list:
    if args.symbol and args.timeframe:
        return [(args.symbol, args.timeframe)]
    env_pairs = _env_override_pairs(settings)
    if env_pairs is not None:
        return env_pairs
    if args.all_scan_pairs:
        return resolve_full_pool_pairs(settings)
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
    parser.add_argument('--reapply', action='store_true',
                        help="Bestaetigte Pairs aus dem vorhandenen alphabet_sweep.json direkt "
                             "in alphabet_overrides.json uebernehmen, OHNE neu zu optimieren (keine "
                             "Exchange-Verbindung/Optuna/Discovery noetig, dauert Sekunden). Fuer den "
                             "Fall, dass alphabet_overrides.json verloren ging, waehrend "
                             "artifacts/results/alphabet_sweep.json (nicht in Git) noch die schon "
                             "berechneten Bestaetigungen enthaelt.")
    parser.add_argument('--auto-apply', action='store_true',
                        help="Bestaetigte Pairs ohne Rueckfrage in settings.json uebernehmen "
                             "(fuer nicht-interaktive Aufrufe, z.B. aus run_pipeline.sh)")
    parser.add_argument('--recheck-confirmed', action='store_true',
                        help="Auch Pairs mit bereits bestaetigtem ODER kuerzlich (siehe "
                             "--recheck-after-days) geprueftem Alphabet neu pruefen")
    parser.add_argument('--recheck-after-days', type=int, default=RECHECK_AFTER_DAYS_DEFAULT,
                        help=f"Nicht bestaetigte Pairs erst nach so vielen Tagen erneut pruefen "
                             f"(Standard: {RECHECK_AFTER_DAYS_DEFAULT})")
    args = parser.parse_args()

    # Ein leerer (aber uebergebener) --symbol/--timeframe ist immer ein
    # Aufrufer-Bug -- niemals still auf active_strategies zurueckfallen (siehe
    # scan_and_learn.py/run_backtest.py, derselbe Fix).
    if args.symbol is not None and not args.symbol.strip():
        logger.critical("--symbol wurde leer uebergeben -- Abbruch statt stillem Fallback.")
        sys.exit(1)
    if args.timeframe is not None and not args.timeframe.strip():
        logger.critical("--timeframe wurde leer uebergeben -- Abbruch statt stillem Fallback.")
        sys.exit(1)

    if args.analyze_only:
        results = {}
        if os.path.exists(RESULTS_PATH):
            with open(RESULTS_PATH) as f:
                results = json.load(f)
        print_summary(results)
        sys.exit(0)

    if args.reapply:
        results = {}
        if os.path.exists(RESULTS_PATH):
            with open(RESULTS_PATH) as f:
                results = json.load(f)
        if not results:
            print(f"Keine gespeicherten Ergebnisse in {RESULTS_PATH} gefunden.")
            sys.exit(0)
        print_summary(results)
        offer_apply(results, load_settings(), auto_apply=args.auto_apply)
        sys.exit(0)

    _settings = load_settings()
    _pairs = resolve_pairs(args, _settings)
    run_sweep(_pairs, args.n_trials, args.min_is_trades, args.min_oos_trades,
              auto_apply=args.auto_apply, skip_confirmed=not args.recheck_confirmed,
              recheck_after_days=args.recheck_after_days)
