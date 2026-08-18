# src/dnabot/analysis/backtester.py
# Backtester für das dnabot Genome-System
#
# Simuliert den Bot auf historischen Daten:
#   1. Für jede Kerze: Prüfe ob ein Genome-Signal vorliegt
#   2. Wenn Signal: Simuliere Trade (Entry, SL, TP)
#   3. Prüfe in den Folgekerzen ob SL oder TP zuerst getroffen wurde
#   4. Berechne Gesamtstatistiken

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.genome.database import GenomeDB
from dnabot.genome.encoder import (
    encode_dataframe, genes_to_sequence_string, build_pattern_sequence,
    build_momentum_pattern_sequence,
)
from dnabot.genome.regime import detect_regime, is_regime_allowed
from dnabot.genome.daily_bias import compute_daily_bias_series, daily_bias_blocks
from dnabot.genome.scoring import kelly_multiplier


def _compute_cvd_slope(df: pd.DataFrame, slope_period: int = 5) -> pd.Series:
    """
    Cumulative Volume Delta (CVD) als Order-Flow-Bestaetigung -- portiert aus
    probebot/features/volume.py::add_all_volume (dort als cvd_slope bereits
    als Feature genutzt) und mbots Layer-5-Volumen-Bestaetigung, unabhaengig
    voneinander auf denselben Ansatz gekommen. Approximation ohne Tick-Daten:
    Delta = Volumen * Vorzeichen(close-open) pro Kerze. Positiver Slope =
    Kaufdruck ueberwiegt (fuer LONG), negativer = Verkaufsdruck (fuer SHORT).
    Rein rueckwaertsgewandt (cumsum + diff auf bereits geschlossenen Kerzen),
    kein Look-Ahead.
    """
    delta = df['volume'] * np.sign(df['close'] - df['open'])
    cvd = delta.cumsum()
    return cvd.diff(slope_period)


# Regime wird alle N Kerzen neu berechnet (Performance, wie discovery.py)
REGIME_RECALC_INTERVAL_BT = 20


def _get_backtest_regime(df: pd.DataFrame, idx: int, cache: dict, window: int = 100) -> str:
    """
    Live-aequivalente Regime-Erkennung fuer den Backtest (bisher fehlte das hier
    komplett -- der Backtest hat JEDES Genom unabhaengig vom aktuellen Markt-
    regime gematcht, waehrend genome_logic.py live nur Genome zulaesst, deren
    active_regimes das AKTUELLE Regime enthalten. Vor dem discovery.py-Regime-
    Fix (2026-08-13) fiel das nicht auf, weil ohnehin alles NEUTRAL war.
    """
    bucket = (idx // REGIME_RECALC_INTERVAL_BT) * REGIME_RECALC_INTERVAL_BT
    if bucket not in cache:
        sub_df = df.iloc[max(0, bucket - window): bucket + 1]
        cache[bucket] = detect_regime(sub_df)
    return cache[bucket]


logger = logging.getLogger(__name__)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
MAX_NOTIONAL_USDT = 200_000.0

# Feinere Timeframe je Strategie-Timeframe fuer die Trailing-Stop-Intrabar-Simulation
# (oraclebot-Muster).
FINE_TF_MAP = {
    '5m': '1m', '15m': '1m', '30m': '1m',
    '1h': '5m', '2h': '5m',
    '4h': '15m', '6h': '15m',
    '1d': '1h',
}


def _find_best_signal(genes: list[str], market: str, timeframe: str,
                       db: GenomeDB, params: dict, cutoff_iso: str = None,
                       current_regime: str = None) -> dict | None:
    """Sucht das beste aktive Genome-Signal in den letzten genome_cfg['sequence_lengths'] Genen.

    Wenn cutoff_iso gesetzt ist, werden Genome-Stats point-in-time aus
    genome_occurrences berechnet (nur Occurrences vor cutoff_iso) statt aus
    der aktuellen All-Time-Aggregation -- verhindert Hindsight-Bias im
    Backtest (siehe get_genome_as_of() in database.py). Ohne cutoff_iso
    (z.B. Live-Nutzung) bleibt das alte Verhalten über db.get_genome().

    current_regime: aktuelles Markt-Regime (TREND/RANGE/NEUTRAL). get_genome_as_of()
    wird MIT regime=None aufgerufen, damit es wie evolver.py::evolve() ALLE drei
    Regime-Buckets prueft und eine active_regimes-Liste zurueckgibt (ein Genome
    kann in mehreren Regimen gleichzeitig aktiv sein) -- current_regime muss nur
    IRGENDWO in dieser Liste vorkommen, aequivalent zu genome_logic.py's
    _regime_active() live (Listen-Mitgliedschaft, kein Exact-Match auf ein
    einzelnes "bestes" Regime). Die fruehere Exact-Match-Variante verwarf
    systematisch gueltige Signale, sobald ein Genome nur regime-uebergreifend
    (globaler NEUTRAL-Fallback) genug point-in-time-Samples hatte, obwohl live
    dieselbe Situation als "ueberall aktiv" behandelt.
    """
    genome_cfg = params.get('genome', {})
    min_score = genome_cfg.get('min_score', 0.05)
    seq_lengths = genome_cfg.get('sequence_lengths', [4, 5, 6])
    rr_ratio = params.get('risk', {}).get('rr_ratio', 2.0)
    min_winrate = genome_cfg.get('min_winrate', 0.45)
    min_samples = genome_cfg.get('min_samples', 20)
    half_life_days = genome_cfg.get('half_life_days', 180.0)

    best = None
    best_score = -1.0

    for seq_len in sorted(seq_lengths, reverse=True):
        if len(genes) < seq_len:
            continue
        window = genes[-seq_len:]
        # Exakte Sequenz UND beide Wildcard-Pattern-Sequenzen versuchen (siehe
        # encoder.py::build_pattern_sequence()/build_momentum_pattern_
        # sequence() -- muss mit genome_logic.py::get_genome_signal() in
        # lockstep bleiben, sonst validiert der Backtest ein anderes Matching
        # als live tatsaechlich laeuft).
        candidates = (
            genes_to_sequence_string(window),
            build_pattern_sequence(window),
            build_momentum_pattern_sequence(window),
        )
        for seq in candidates:
            for direction in ['LONG', 'SHORT']:
                if cutoff_iso is not None:
                    g = db.get_genome_as_of(
                        seq, market, timeframe, direction, cutoff_iso,
                        regime=None,
                        min_samples=min_samples, min_winrate=min_winrate,
                        score_threshold=min_score, half_life_days=half_life_days,
                    )
                    if g and current_regime is not None and current_regime not in g['active_regimes']:
                        g = None
                else:
                    g = db.get_genome(seq, market, timeframe, direction)
                if g and g['active'] and g['score'] >= min_score and g['score'] > best_score:
                    best_score = g['score']
                    best = {
                        'direction': direction,
                        'genome': g,
                        'seq_len': seq_len,
                        'rr_ratio': rr_ratio,
                    }

    return best


class LazyFineData:
    """
    On-Demand-Fetcher fuer Fein-Daten (Intrabar-Trailing-Simulation). Laedt
    Fein-Kerzen nur fuer die Tage, die tatsaechlich von einem simulierten
    Trade durchlaufen werden, statt den kompletten Backtest-Zeitraum vorab
    herunterzuladen. Ergebnis ist identisch zum eagerly geladenen DataFrame,
    nur Zeitpunkt und Groesse der Netzwerk-Fetches aendern sich. Ein Trade
    kann viele Coarse-Kerzen ueberspannen, daher Tage-Bucketing ueber
    mehrere Kalendertage hinweg.
    """
    def __init__(self, symbol, fine_tf):
        self.symbol = symbol
        self.fine_tf = fine_tf
        self._days = {}
        self._exchange = None

    def _get_exchange(self):
        if self._exchange is not None:
            return self._exchange
        try:
            with open(os.path.join(PROJECT_ROOT, 'secret.json'), 'r') as f:
                secrets = json.load(f)
            accounts = secrets.get('dnabot', [])
            if accounts:
                from dnabot.utils.exchange import Exchange
                self._exchange = Exchange(accounts[0])
        except Exception:
            self._exchange = None
        return self._exchange

    def _ensure_day(self, day):
        if day in self._days:
            return
        exchange = self._get_exchange()
        if exchange is None or not exchange.markets:
            self._days[day] = None
            return
        try:
            day_str = day.strftime('%Y-%m-%d')
            next_day_str = (day + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            df = exchange.fetch_historical_ohlcv(self.symbol, self.fine_tf, day_str, next_day_str)
            self._days[day] = df if df is not None and not df.empty else None
        except Exception:
            self._days[day] = None

    def get_slice(self, start_ts, end_ts):
        if self.fine_tf is None:
            return None
        start_ts = pd.Timestamp(start_ts)
        end_ts = pd.Timestamp(end_ts)
        first_day = start_ts.floor('D')
        last_day = (end_ts - pd.Timedelta(microseconds=1)).floor('D')
        parts = []
        day = first_day
        while day <= last_day:
            self._ensure_day(day)
            if self._days[day] is not None:
                parts.append(self._days[day])
            day += pd.Timedelta(days=1)
        if not parts:
            return None
        combined = pd.concat(parts).sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]
        return combined.loc[(combined.index >= start_ts) & (combined.index < end_ts)]


def _get_fine_slice(fine_data, start_ts, end_ts):
    if fine_data is None:
        return None
    if hasattr(fine_data, 'get_slice'):
        return fine_data.get_slice(start_ts, end_ts)
    return fine_data.loc[(fine_data.index >= start_ts) & (fine_data.index < end_ts)]


def simulate_trade(signal: dict, df: pd.DataFrame, entry_idx: int,
                    max_hold_candles: int = 20,
                    trailing_callback_pct: float = None,
                    fine_df: pd.DataFrame = None) -> dict:
    """
    Simuliert einen Trade auf historischen Daten.

    Entry = Close der Signal-Kerze
    SL = Low/High der letzten seq_len Kerzen
    TP = Entry ± rr_ratio × SL-Distanz

    trailing_callback_pct (0-1, z.B. 0.01 = 1%): wenn gesetzt, wird tp_price nur
    als AKTIVIERUNGS-Preis fuer einen Trailing Stop behandelt (wie live in
    trade_manager.py via place_trailing_stop_order), statt als sofortiger Take-
    Profit-Exit. Das ist die Live-Realitaet -- ohne diesen Parameter (None)
    bleibt das alte Verhalten (fixer TP-Exit) fuer Rueckwaertskompatibilitaet
    erhalten.
    fine_df: feinere Kerzen (oraclebot-Muster) fuer eine praezisere Trailing-
    Simulation als auf Basis der Coarse-Kerzen von `df` allein moeglich waere.
    Ohne fine_df wird auf `df` selbst zurueckgefallen (grobere Naeherung).
    """
    seq_len = signal['seq_len']
    direction = signal['direction']
    rr_ratio = signal['rr_ratio']

    seq_df = df.iloc[max(0, entry_idx - seq_len + 1): entry_idx + 1]
    entry_price = float(df['close'].iloc[entry_idx])

    if direction == 'LONG':
        sl_price = float(seq_df['low'].min())
        sl_dist = entry_price - sl_price
        if sl_dist <= 0:
            sl_price = entry_price * 0.98
            sl_dist = entry_price - sl_price
        tp_price = entry_price + rr_ratio * sl_dist
    else:
        sl_price = float(seq_df['high'].max())
        sl_dist = sl_price - entry_price
        if sl_dist <= 0:
            sl_price = entry_price * 1.02
            sl_dist = sl_price - entry_price
        tp_price = entry_price - rr_ratio * sl_dist

    sl_pct = sl_dist / entry_price * 100.0

    entry_time = df.index[entry_idx]
    last_idx = min(entry_idx + max_hold_candles, len(df) - 1)
    end_time = df.index[last_idx]

    # Walk-Bars: feinere Kerzen bevorzugt (praeziserer Trailing-Verlauf),
    # sonst die Coarse-Kerzen von df selbst (altes Verhalten).
    if fine_df is not None:
        walk_bars = _get_fine_slice(fine_df, entry_time + pd.Timedelta(microseconds=1), end_time + pd.Timedelta(microseconds=1))
        using_fine = walk_bars is not None and not walk_bars.empty
    else:
        using_fine = False
    if not using_fine:
        walk_bars = df.iloc[entry_idx + 1: last_idx + 1]

    outcome = 'TIMEOUT'
    exit_price = float(df['close'].iloc[last_idx])
    exit_time = end_time
    trailing_active = False
    peak_price = None

    for ts, bar in walk_bars.iterrows():
        h, l = float(bar['high']), float(bar['low'])

        if direction == 'LONG':
            if l <= sl_price:
                outcome, exit_price, exit_time = 'LOSS', sl_price, ts
                break
            if trailing_callback_pct is None:
                if h >= tp_price:
                    outcome, exit_price, exit_time = 'WIN', tp_price, ts
                    break
            else:
                if not trailing_active and h >= tp_price:
                    trailing_active = True
                    peak_price = h
                if trailing_active:
                    peak_price = max(peak_price, h)
                    trail_level = peak_price * (1 - trailing_callback_pct)
                    if l <= trail_level:
                        outcome, exit_price, exit_time = 'WIN', trail_level, ts
                        break
        else:
            if h >= sl_price:
                outcome, exit_price, exit_time = 'LOSS', sl_price, ts
                break
            if trailing_callback_pct is None:
                if l <= tp_price:
                    outcome, exit_price, exit_time = 'WIN', tp_price, ts
                    break
            else:
                if not trailing_active and l <= tp_price:
                    trailing_active = True
                    peak_price = l
                if trailing_active:
                    peak_price = min(peak_price, l)
                    trail_level = peak_price * (1 + trailing_callback_pct)
                    if h >= trail_level:
                        outcome, exit_price, exit_time = 'WIN', trail_level, ts
                        break

    # exit_idx (Coarse-Index) wird fuer die Fortsetzung der Hauptschleife gebraucht --
    # bei Fein-Simulation ueber die Zeit statt ueber den Index bestimmt.
    if using_fine:
        exit_idx = df.index.searchsorted(exit_time)
        exit_idx = min(exit_idx, len(df) - 1)
    else:
        exit_idx = last_idx if outcome == 'TIMEOUT' else df.index.get_loc(exit_time)

    if direction == 'LONG':
        pnl_pct = (exit_price - entry_price) / entry_price * 100.0
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100.0

    return {
        'entry_time': str(df.index[entry_idx]),
        'exit_time': str(exit_time),
        'direction': direction,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'sl_price': sl_price,
        'tp_price': tp_price,
        'sl_pct': sl_pct,
        'outcome': outcome,
        'pnl_pct': pnl_pct,
        'genome_id': signal['genome']['genome_id'],
        'genome_score': signal['genome']['score'],
        'genome_winrate': signal['genome']['wins'] / max(signal['genome']['total_occurrences'], 1),
        'seq_len': seq_len,
        'exit_idx': exit_idx,
    }


def run_backtest(
    df: pd.DataFrame,
    market: str,
    timeframe: str,
    db: GenomeDB,
    params: dict,
    start_capital: float = 1000.0,
    risk_per_trade_pct: float = 1.0,
    max_hold_candles: int = 20,
    warmup_candles: int = 35,
    leverage: int = 1,
    fine_df: pd.DataFrame = None,
) -> dict:
    """
    Führt einen vollständigen Backtest durch.

    Returns:
        dict mit trades (Liste), stats (Zusammenfassung)
    """
    if len(df) < warmup_candles + 10:
        logger.warning(f"Zu wenig Kerzen für Backtest ({len(df)}). Min: {warmup_candles + 10}")
        return {"trades": [], "stats": {}}

    logger.info(f"[Backtest] {market} ({timeframe}) | {len(df)} Kerzen | Kapital: {start_capital} USDT")

    # Trailing Stop (wie live in trade_manager.py via place_trailing_stop_order) statt
    # fixem TP-Exit -- tp_price wird zum Aktivierungspreis. None = altes Verhalten.
    trailing_callback_pct = params.get('risk', {}).get('trailing_callback_rate_pct')
    if trailing_callback_pct is not None:
        trailing_callback_pct = float(trailing_callback_pct) / 100.0

    # Kelly-Sizing (optional, standardmaessig aus): dieselbe Formel wie live
    # in trade_manager.py::place_entry_orders() (genome/scoring.py::
    # kelly_multiplier(), geteilt statt getrennt implementiert) -- sonst
    # validiert dieser Backtest eine Positionsgroesse, die live gar nicht
    # zustande kommt, sobald use_kelly_sizing fuer dieses Pair aktiv ist.
    risk_cfg_bt = params.get('risk', {})
    use_kelly_sizing = bool(risk_cfg_bt.get('use_kelly_sizing', False))
    kelly_min_mult = risk_cfg_bt.get('kelly_min_mult', 0.5)
    kelly_max_mult = risk_cfg_bt.get('kelly_max_mult', 3.0)
    kelly_dampening = risk_cfg_bt.get('kelly_dampening', 0.3)
    kelly_threshold_winrate = params.get('genome', {}).get('min_winrate', 0.45)

    # Tagestrend-Filter (optional, standardmaessig aus -- siehe daily_bias.py,
    # dieselbe Funktion wie live in genome_logic.py, damit Backtest und Live
    # garantiert dasselbe Signal blocken/durchlassen): laufender Bias der
    # AKTUELLEN Tageskerze (Tages-Open vs. jeweils aktuellem Close) -- rot
    # bisher -> nur Shorts, gruen bisher -> nur Longs.
    use_daily_trend_filter = bool(params.get('genome', {}).get('use_daily_trend_filter', False))
    daily_bias_series = compute_daily_bias_series(df) if use_daily_trend_filter else None

    # CVD-Bestaetigungsfilter (optional, standardmaessig aus): Order-Flow-Schicht
    # orthogonal zum Tagestrend -- blockt Signale gegen den aktuellen Kauf-/
    # Verkaufsdruck statt gegen den uebergeordneten Trend.
    use_cvd_filter = bool(params.get('genome', {}).get('use_cvd_filter', False))
    cvd_slope = None
    if use_cvd_filter:
        cvd_period = int(params.get('genome', {}).get('cvd_slope_period', 5))
        cvd_slope = _compute_cvd_slope(df, slope_period=cvd_period)

    # Markt-Regime-Gate (Aequivalent zu genome_logic.py live) -- fehlte hier bisher
    # komplett, siehe _get_backtest_regime()-Docstring.
    allowed_regimes = params.get('genome', {}).get('allowed_regimes', ['TREND', 'RANGE', 'NEUTRAL'])
    regime_cache_bt = {}

    # Alle Gene vorberechnen (Alphabet-Override fuer dieses Pair, falls in
    # settings.json::genome_settings.alphabet_by_pair gesetzt -- muss zum
    # Alphabet passen, mit dem die Genome-DB fuer dieses Pair befuellt wurde,
    # sonst matchen die hier gebauten Sequenz-Strings nie einen DB-Eintrag)
    alphabet = params.get('genome', {}).get('alphabet')
    genes = encode_dataframe(df, alphabet=alphabet)

    trades = []
    equity = start_capital
    equity_curve = [equity]
    i = warmup_candles

    while i < len(df) - max_hold_candles:
        current_regime = _get_backtest_regime(df, i, regime_cache_bt)
        if not is_regime_allowed(current_regime, allowed_regimes):
            i += 1
            equity_curve.append(equity)
            continue

        current_genes = genes[:i + 1]

        # Signal suchen (point-in-time -- kein Hindsight-Bias aus zukünftigen Occurrences)
        cutoff_iso = df.index[i].isoformat()
        signal = _find_best_signal(current_genes, market, timeframe, db, params,
                                    cutoff_iso=cutoff_iso, current_regime=current_regime)

        if signal is not None and daily_bias_series is not None:
            if daily_bias_blocks(daily_bias_series.iloc[i], signal['direction']):
                signal = None

        if signal is not None and use_cvd_filter and cvd_slope is not None:
            _cvd_val = cvd_slope.iloc[i]
            if pd.notna(_cvd_val):
                if _cvd_val < 0 and signal['direction'] == 'LONG':
                    signal = None
                elif _cvd_val > 0 and signal['direction'] == 'SHORT':
                    signal = None

        if signal is None:
            i += 1
            equity_curve.append(equity)
            continue

        # Trade simulieren
        trade = simulate_trade(signal, df, i, max_hold_candles,
                               trailing_callback_pct=trailing_callback_pct, fine_df=fine_df)

        # Kelly-Multiplikator fuer DIESEN Trade, aus der point-in-time
        # Winrate des Signals (trade['genome_winrate'], via get_genome_as_of()
        # -- kein Hindsight-Bias) und dem fuer dieses Pair verwendeten
        # rr_ratio. 1.0 wenn use_kelly_sizing aus ist -- unveraendertes altes
        # Verhalten.
        if use_kelly_sizing:
            trade_kelly_mult = kelly_multiplier(
                winrate=trade['genome_winrate'],
                rr_ratio=signal['rr_ratio'],
                threshold_winrate=kelly_threshold_winrate,
                min_mult=kelly_min_mult,
                max_mult=kelly_max_mult,
                dampening=kelly_dampening,
            )
        else:
            trade_kelly_mult = 1.0
        trade['kelly_multiplier'] = trade_kelly_mult

        # Equity berechnen (Positionsgröße auf equity × leverage deckeln)
        risk_amount = equity * (risk_per_trade_pct / 100.0) * trade_kelly_mult
        sl_pct = trade['sl_pct']
        if sl_pct > 0:
            position_size = risk_amount / (sl_pct / 100.0)
            max_position = equity * max(leverage, 1)
            if position_size > max_position:
                position_size = max_position
                risk_amount = position_size * (sl_pct / 100.0)
            if position_size > MAX_NOTIONAL_USDT:
                position_size = MAX_NOTIONAL_USDT
                risk_amount = position_size * (sl_pct / 100.0)
            actual_pnl = position_size * (trade['pnl_pct'] / 100.0)
            equity += actual_pnl
        else:
            actual_pnl = 0.0

        trade['equity_after'] = equity
        trade['pnl_usdt'] = actual_pnl
        trades.append(trade)

        # Weiter nach Trade-Abschluss (verhindert doppelte Trades)
        i = trade['exit_idx'] + 1
        equity_curve.append(equity)

    # Statistiken berechnen
    stats = _compute_stats(trades, equity_curve, start_capital)
    logger.info(
        f"[Backtest] Abgeschlossen: {stats.get('total_trades', 0)} Trades | "
        f"WR: {stats.get('win_rate', 0):.1%} | "
        f"Total PnL: {stats.get('total_pnl_usdt', 0):+.2f} USDT | "
        f"Max DD: {stats.get('max_drawdown_pct', 0):.1f}%"
    )

    return {"trades": trades, "stats": stats, "equity_curve": equity_curve}


def _compute_stats(trades: list[dict], equity_curve: list[float], start_capital: float) -> dict:
    if not trades:
        return {"total_trades": 0}

    wins = [t for t in trades if t['outcome'] == 'WIN']
    losses = [t for t in trades if t['outcome'] == 'LOSS']
    timeouts = [t for t in trades if t['outcome'] == 'TIMEOUT']

    total = len(trades)
    win_rate = len(wins) / total if total > 0 else 0.0

    pnl_list = [t.get('pnl_usdt', 0) for t in trades]
    total_pnl = sum(pnl_list)
    avg_win = sum(t['pnl_usdt'] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t['pnl_usdt'] for t in losses) / len(losses) if losses else 0.0

    profit_factor = (
        abs(sum(t['pnl_usdt'] for t in wins)) /
        abs(sum(t['pnl_usdt'] for t in losses))
        if losses and sum(t['pnl_usdt'] for t in losses) != 0 else float('inf')
    )

    # Max Drawdown
    eq = equity_curve if equity_curve else [start_capital]
    peak = eq[0]
    max_dd = 0.0
    for e in eq:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "win_rate": win_rate,
        "total_pnl_usdt": total_pnl,
        "total_pnl_pct": (total_pnl / start_capital) * 100,
        "avg_win_usdt": avg_win,
        "avg_loss_usdt": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "final_equity": equity_curve[-1] if equity_curve else start_capital,
    }


def save_results(results: dict, market: str, timeframe: str):
    """Speichert Backtest-Ergebnisse als JSON."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe_name = f"{market.replace('/', '').replace(':', '')}_{timeframe}"
    path = os.path.join(RESULTS_DIR, f"backtest_{safe_name}.json")

    output = {
        "market": market,
        "timeframe": timeframe,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "stats": results.get("stats", {}),
        "trades": results.get("trades", []),
    }
    with open(path, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"Backtest-Ergebnisse gespeichert: {path}")
    return path


def print_backtest_summary(results: dict, market: str, timeframe: str):
    stats = results.get("stats", {})
    trades = results.get("trades", [])

    print(f"\n{'=' * 60}")
    print(f"  BACKTEST: {market} ({timeframe})")
    print(f"{'=' * 60}")
    print(f"  Trades gesamt:   {stats.get('total_trades', 0)}")
    print(f"  Wins / Losses:   {stats.get('wins', 0)} / {stats.get('losses', 0)}")
    print(f"  Win-Rate:        {stats.get('win_rate', 0):.1%}")
    print(f"  Profit Factor:   {stats.get('profit_factor', 0):.2f}")
    print(f"  Total PnL:       {stats.get('total_pnl_usdt', 0):+.2f} USDT ({stats.get('total_pnl_pct', 0):+.1f}%)")
    print(f"  Avg Win:         {stats.get('avg_win_usdt', 0):+.2f} USDT")
    print(f"  Avg Loss:        {stats.get('avg_loss_usdt', 0):+.2f} USDT")
    print(f"  Max Drawdown:    {stats.get('max_drawdown_pct', 0):.1f}%")
    print(f"  Final Equity:    {stats.get('final_equity', 0):.2f} USDT")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

    parser = argparse.ArgumentParser(description="dnabot Backtester")
    parser.add_argument('--symbol', type=str, default='BTC/USDT:USDT')
    parser.add_argument('--timeframe', type=str, default='4h')
    parser.add_argument('--capital', type=float, default=1000.0)
    parser.add_argument('--risk', type=float, default=1.0, help="Risiko pro Trade in %")
    args = parser.parse_args()

    with open(os.path.join(PROJECT_ROOT, 'settings.json'), 'r') as f:
        settings = json.load(f)

    # Für Backtest brauchen wir historische Daten — Einfacher Modus: aus DB lesen
    # (In der Praxis: Exchange-Verbindung und historischen Download)
    print(f"Backtest für {args.symbol} ({args.timeframe})")
    print("Hinweis: Historische Daten werden benötigt. Nutze run_pipeline.sh für vollständigen Ablauf.")
