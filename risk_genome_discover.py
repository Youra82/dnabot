#!/usr/bin/env python3
# risk_genome_discover.py
# Discovery fuer Risiko-/Exit-Gene (momentum_exit-Strategie) -- Pendant zu
# scan_and_learn.py, aber fuer die RiskGenomeDB statt der Kerzen-Genome-DB.
#
# Ablauf pro (market, timeframe):
#   1. Historische Kerzen laden (echte Bitget-Daten)
#   2. Kandidaten-Gene aus einem festen Parameter-Raster anlegen
#      (seq_len x rr_ratio x trailing_pct, risk_pct separat -- risk_pct
#      skaliert nur die Positionsgroesse, braucht keinen eigenen
#      simulate_trade()-Lauf, siehe record_trade())
#   3. Fuer jede (seq_len, rr_ratio, trailing_pct)-Kombination: EINMAL durch
#      die Historie laufen, echte simulate_trade() pro Kerze (Momentum-
#      Einstieg = eigene Kerzenrichtung, wie momentum_exit_logic.py live)
#   4. Jeden simulierten Trade fuer JEDE risk_pct-Variante als Occurrence
#      in die RiskGenomeDB schreiben (source='backtest')
#   5. risk_evolver.evolve_risk_genes() aufrufen -- aktiviert das beste Gen
#
# Ausfuehrung:
#   .venv/bin/python3 risk_genome_discover.py --symbol BTC/USDT:USDT --timeframe 6h
#   .venv/bin/python3 risk_genome_discover.py  # alle momentum_exit-Paare aus settings.json

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.analysis.backtester import simulate_trade
from dnabot.genome.risk_genome_db import RiskGenomeDB
from dnabot.genome.risk_evolver import evolve_risk_genes
from dnabot.utils.config_loader import HISTORY_DAYS_MAP, load_settings, load_secrets

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

RISK_DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_genome.db')

# Parameter-Raster -- identisch zur urspruenglichen Recherche
# (recherche/risk_exit_genetic_test.py), die Fund AQ/AR hervorbrachte.
SEQLEN_CHOICES = [5, 10, 20]
RR_CHOICES = [1.5, 2.0, 3.0]
TRAIL_CHOICES = [0.5, 1.5, 3.0]
RISK_CHOICES = [0.25, 0.5, 1.0, 2.0]

MIN_CANDLES_REQUIRED = 35
DUMMY_GENOME = {'genome_id': 'RISKGENE', 'score': 0.0, 'wins': 0, 'total_occurrences': 1}


def to_naive(ts):
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


OOS_WEEKS = 26  # etabliertes Projekt-Konventionsfenster (settings.json::backtest_lookback_weeks)


def _run_config(df, seq_len, rr, trail_frac, entry_range):
    """Simuliert EINE (seq_len,rr,trail)-Konfiguration ueber die Kerzen in
    entry_range (Indexbereich), echte simulate_trade(), ein Trade pro Symbol
    gleichzeitig (busy_until-Gate)."""
    opens, closes = df['open'].values, df['close'].values
    busy_until = None
    trades = []
    for i in entry_range:
        entry_time = to_naive(df.index[i])
        if busy_until is not None and entry_time < busy_until:
            continue
        direction = 'LONG' if closes[i] >= opens[i] else 'SHORT'
        signal = {'seq_len': seq_len, 'direction': direction, 'rr_ratio': rr, 'genome': DUMMY_GENOME}
        t = simulate_trade(signal, df, i, max_hold_candles=20, trailing_callback_pct=trail_frac)
        busy_until = to_naive(t['exit_time'])
        trades.append(t)
    return trades


def discover_pair(exchange, db: RiskGenomeDB, market: str, timeframe: str, history_days: int):
    """
    WICHTIG -- IS/OOS-Trennung (wie im gesamten Projekt Konvention, siehe
    Fund L in research_dnabot_direction_calibration.md): Kandidaten-Gene
    werden NUR auf dem IS-Anteil (alles vor den letzten OOS_WEEKS Wochen)
    bewertet und ausgewaehlt. Das gewaehlte Gen wird danach EINMALIG auf dem
    nie zuvor gesehenen OOS-Anteil geprueft -- nur bei positivem OOS-Calmar
    bleibt es aktiv. Ohne das waere die Gen-Auswahl reine In-Sample-
    Ueberanpassung (109 Kandidaten, bester gewinnt automatisch etwas).
    """
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=history_days)
    logger.info(f"Lade {market} ({timeframe}) | {history_days}d History...")
    df = exchange.fetch_historical_ohlcv(market, timeframe,
                                          start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    if df is None or df.empty:
        logger.warning(f"Keine Daten fuer {market} ({timeframe}).")
        return
    logger.info(f"{len(df)} Kerzen geladen.")

    max_seq = max(SEQLEN_CHOICES)
    n = len(df)
    oos_start_ts = to_naive(df.index.max()) - timedelta(weeks=OOS_WEEKS)
    is_end_idx = int((df.index.map(to_naive) < oos_start_ts).sum())
    is_range = range(max_seq, max(is_end_idx, max_seq + 1))
    oos_range = range(max(is_end_idx, max_seq + 1), n - 1)
    logger.info(f"IS: Kerze {max_seq}..{is_range.stop - 1} | OOS (letzte {OOS_WEEKS}W): "
                f"Kerze {oos_range.start}..{n - 2}")

    with db.batch_writes():
        for seq_len in SEQLEN_CHOICES:
            for rr in RR_CHOICES:
                for trail in TRAIL_CHOICES:
                    trail_frac = trail / 100.0
                    gene_ids = {rp: db.upsert_candidate(market, timeframe, seq_len, rr, trail, rp)
                                for rp in RISK_CHOICES}

                    # NUR IS-Trades fliessen in die Bewertung/Gen-Auswahl ein.
                    is_trades = _run_config(df, seq_len, rr, trail_frac, is_range)
                    for t in is_trades:
                        for rp, gid in gene_ids.items():
                            db.record_trade(gid, str(t['entry_time']), str(t['exit_time']),
                                             t['outcome'], t['pnl_pct'], t['sl_pct'], source='backtest')

                    logger.info(f"  seq_len={seq_len} rr={rr} trail={trail}%: {len(is_trades)} IS-Trades "
                                f"x {len(RISK_CHOICES)} Risk-Varianten verbucht.")

    result = evolve_risk_genes(db, market, timeframe)

    # Finaler, EINMALIGER OOS-Check des gewaehlten Gens -- nie zuvor gesehene Daten.
    active = db.get_active_gene(market, timeframe)
    if active is None:
        logger.info(f"[OOS-Check] {market} ({timeframe}): kein aktives Gen, kein OOS-Check noetig.")
        return result

    oos_trades = _run_config(df, active['seq_len'], active['rr_ratio'],
                              active['trailing_pct'] / 100.0, oos_range)
    if not oos_trades:
        logger.warning(f"[OOS-Check] {market} ({timeframe}): keine OOS-Trades -- Gen bleibt vorsichtshalber inaktiv.")
        db.set_active(active['risk_gene_id'], False)
        return result

    equity, peak, max_dd, wins = 100.0, 100.0, 0.0, 0
    for t in oos_trades:
        sl_pct = max(t['sl_pct'], 0.01)
        risk_amount = equity * (active['risk_pct'] / 100.0)
        pnl = risk_amount * (t['pnl_pct'] / sl_pct)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
        wins += 1 if t['outcome'] == 'WIN' else 0
    oos_pnl_pct = equity - 100.0
    oos_calmar = oos_pnl_pct / max_dd if max_dd > 0 else oos_pnl_pct

    logger.info(f"[OOS-Check] {market} ({timeframe}): n={len(oos_trades)} "
                f"WR={wins/len(oos_trades):.1%} PnL={oos_pnl_pct:+.1f}% MaxDD={max_dd:.1f}% Calmar={oos_calmar:.2f}")

    if oos_calmar <= 0:
        logger.warning(f"[OOS-Check] {market} ({timeframe}): OOS-Calmar nicht positiv -- "
                        f"Gen wird DEAKTIVIERT (war nur IS-Ueberanpassung).")
        db.set_active(active['risk_gene_id'], False)
    else:
        logger.info(f"[OOS-Check] {market} ({timeframe}): OOS bestaetigt -- Gen bleibt aktiv.")

    return result


def main():
    parser = argparse.ArgumentParser(description="Risiko-Gen-Discovery fuer momentum_exit")
    parser.add_argument('--symbol', type=str, default=None)
    parser.add_argument('--timeframe', type=str, default=None)
    parser.add_argument('--history-days', type=int, default=None)
    args = parser.parse_args()

    secrets = load_secrets()
    accounts = secrets.get('dnabot', [])
    if not accounts:
        logger.critical("Keine dnabot-Accounts in secret.json gefunden.")
        sys.exit(1)
    exchange = Exchange(accounts[0])
    db = RiskGenomeDB(RISK_DB_PATH)

    if args.symbol and args.timeframe:
        pairs = [(args.symbol, args.timeframe)]
    else:
        settings = load_settings()
        strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
        pairs = [(s['symbol'], s['timeframe']) for s in strategies
                 if s.get('strategy_type') == 'momentum_exit']
        if not pairs:
            # Kein Fehler -- normal fuer Nutzer, die momentum_exit gar nicht
            # verwenden (z.B. beim automatischen Scheduler-Lauf). Exit 0,
            # damit ein aufrufendes Skript (auto_optimizer_scheduler.py) das
            # nicht faelschlich als Fehlschlag protokolliert.
            logger.info("Keine momentum_exit-Strategien in active_strategies und "
                        "kein --symbol/--timeframe angegeben -- nichts zu tun.")
            db.close()
            return

    for market, timeframe in pairs:
        history_days = args.history_days or HISTORY_DAYS_MAP.get(timeframe, 730)
        print(f"\n{'=' * 70}\nRisiko-Gen-Discovery: {market} ({timeframe})\n{'=' * 70}")
        discover_pair(exchange, db, market, timeframe, history_days)

    db.close()
    print("\nDiscovery abgeschlossen.")


if __name__ == '__main__':
    main()
