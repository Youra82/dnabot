# tests/test_discovery_pathdep.py
"""
Testet die pfadabhaengige SL/TP-Simulation in discovery.py (siehe
src/dnabot/genome/discovery.py Modul-Docstring fuer den Hintergrund: die
vorherige Win/Loss-Definition war pfadunabhaengig -- "max Exkursion > Threshold"
-- und erzeugte deshalb eine Genome-Winrate, die nicht zur echten
Backtest-/Live-Winrate passte).

Zwei Test-Ebenen:
1. Reine Unit-Tests fuer _simulate_sl_tp_path() (kein DB/Pandas-Overhead noetig)
2. Ein Integrationstest pro Szenario ueber discover_genomes() mit synthetischem
   OHLCV, der bestaetigt, dass die Verdrahtung (strukturelles SL/TP aus dem
   Sequenz-Fenster + Pfadsimulation + upsert_genome_outcome) end-to-end korrekt
   ist.
"""
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

import numpy as np
import pandas as pd
import pytest

from dnabot.genome.discovery import _simulate_sl_tp_path, discover_genomes
from dnabot.genome.database import GenomeDB


# ── 1. Unit-Tests: _simulate_sl_tp_path ─────────────────────────────────────

def test_long_sl_hit_first_is_loss():
    future_highs = np.array([101.0, 120.0, 119.0])
    future_lows = np.array([90.0, 95.0, 95.0])   # Kerze 0 durchbricht SL=95
    outcome, exit_price = _simulate_sl_tp_path(
        sl_price=95.0, tp_price=110.0, future_highs=future_highs, future_lows=future_lows,
        direction="LONG", timeout_price=100.0,
    )
    assert outcome == "LOSS"
    assert exit_price == 95.0


def test_long_tp_hit_first_is_win():
    future_highs = np.array([115.0, 113.0, 112.0])   # Kerze 0 durchbricht TP=110
    future_lows = np.array([99.0, 111.0, 111.0])
    outcome, exit_price = _simulate_sl_tp_path(
        sl_price=95.0, tp_price=110.0, future_highs=future_highs, future_lows=future_lows,
        direction="LONG", timeout_price=100.0,
    )
    assert outcome == "WIN"
    assert exit_price == 110.0


def test_long_neither_hit_is_timeout():
    future_highs = np.array([103.0, 104.0, 105.0])
    future_lows = np.array([98.0, 99.0, 100.0])
    outcome, exit_price = _simulate_sl_tp_path(
        sl_price=95.0, tp_price=110.0, future_highs=future_highs, future_lows=future_lows,
        direction="LONG", timeout_price=101.5,
    )
    assert outcome == "TIMEOUT"
    assert exit_price == 101.5


def test_long_same_bar_tie_break_favors_sl():
    # Kerze 0 durchbricht SOWOHL SL als auch TP -- SL muss gewinnen (konservative
    # Konvention, identisch zu backtester.py::simulate_trade, das SL pro Bar
    # vor TP prueft).
    future_highs = np.array([115.0])
    future_lows = np.array([90.0])
    outcome, exit_price = _simulate_sl_tp_path(
        sl_price=95.0, tp_price=110.0, future_highs=future_highs, future_lows=future_lows,
        direction="LONG", timeout_price=100.0,
    )
    assert outcome == "LOSS"


def test_short_sl_hit_first_is_loss():
    # SHORT: SL = Preis STEIGT ueber sl_price, TP = Preis FAELLT unter tp_price
    future_highs = np.array([106.0, 108.0])   # Kerze 0 durchbricht SL=105
    future_lows = np.array([100.0, 90.0])
    outcome, exit_price = _simulate_sl_tp_path(
        sl_price=105.0, tp_price=90.0, future_highs=future_highs, future_lows=future_lows,
        direction="SHORT", timeout_price=100.0,
    )
    assert outcome == "LOSS"
    assert exit_price == 105.0


def test_short_tp_hit_first_is_win():
    future_highs = np.array([101.0, 102.0])
    future_lows = np.array([89.0, 88.0])   # Kerze 0 durchbricht TP=90
    outcome, exit_price = _simulate_sl_tp_path(
        sl_price=105.0, tp_price=90.0, future_highs=future_highs, future_lows=future_lows,
        direction="SHORT", timeout_price=100.0,
    )
    assert outcome == "WIN"
    assert exit_price == 90.0


# ── 2. Integrationstest: discover_genomes() end-to-end ──────────────────────

def _make_df(scenario: str) -> pd.DataFrame:
    """
    Baut 90 Kerzen: 80 Warmup-Kerzen (fuer ATR/Regime-Lookback), 4 Sequenz-
    Kerzen (Index 80-83, seq_low=95 durch Kerze 81 definiert, Entry-Close=100
    in Kerze 83), 5 Horizon-Kerzen (Index 84-88, je nach Szenario), 1
    Puffer-Kerze (Index 89, noetig damit discover_genomes' max_start-Grenze
    i=80 als gueltiges Fenster zulaesst).

    Bei rr_ratio=2.0 und seq_low=95/entry=100: SL=95, sl_dist=5, TP=110.
    """
    rows = []
    for k in range(80):
        wiggle = 0.05 if k % 2 == 0 else -0.05
        o, c = 100.0, 100.0 + wiggle
        h, l = max(o, c) + 0.1, min(o, c) - 0.1
        rows.append([o, h, l, c, 100.0])

    # Sequenz-Kerzen (Index 80-83) -- seq_low=95, Entry-Close=100
    rows.extend([
        [100.0, 101.0, 97.0, 99.0, 100.0],
        [99.0, 100.0, 95.0, 98.0, 100.0],    # definiert seq_low=95
        [98.0, 100.0, 96.0, 99.5, 100.0],
        [99.5, 101.0, 98.0, 100.0, 100.0],   # Close=100 -> entry_price
    ])

    if scenario == "loss":
        future = [
            [100.0, 101.0, 90.0, 95.0, 100.0],   # low=90 <= SL(95) -> LOSS sofort
            [95.0, 120.0, 95.0, 118.0, 100.0],   # spaeterer Spike darf nichts mehr aendern
            [118.0, 119.0, 117.0, 118.0, 100.0],
            [118.0, 119.0, 117.0, 118.0, 100.0],
            [118.0, 119.0, 117.0, 118.0, 100.0],
        ]
    elif scenario == "win":
        future = [
            [100.0, 115.0, 99.0, 112.0, 100.0],  # high=115 >= TP(110) -> WIN sofort
            [112.0, 113.0, 111.0, 112.0, 100.0],
            [112.0, 113.0, 111.0, 112.0, 100.0],
            [112.0, 113.0, 111.0, 112.0, 100.0],
            [112.0, 113.0, 111.0, 112.0, 100.0],
        ]
    else:  # timeout
        future = [
            [100.0, 103.0, 98.0, 101.0, 100.0],  # bleibt strikt zwischen SL(95) und TP(110)
            [101.0, 104.0, 99.0, 102.0, 100.0],
            [102.0, 105.0, 100.0, 103.0, 100.0],
            [103.0, 106.0, 101.0, 104.0, 100.0],
            [104.0, 107.0, 102.0, 105.0, 100.0],
        ]
    rows.extend(future)
    rows.append([105.0, 106.0, 104.0, 105.0, 100.0])  # Puffer-Kerze (Index 89)

    idx = pd.date_range("2024-01-01", periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def _run_discovery_for_scenario(tmp_path, scenario: str) -> dict:
    df = _make_df(scenario)
    db_path = str(tmp_path / f"genome_{scenario}.db")
    db = GenomeDB(db_path)
    try:
        discover_genomes(
            df=df, market="TEST/USDT:USDT", timeframe="1h", db=db,
            sequence_lengths=[4], discovery_horizon=5, rr_ratio=2.0,
            start_candle_index=80,   # nur das eine praeparierte Fenster (i=80) verarbeiten
        )
        genomes = db.get_all_genomes(market="TEST/USDT:USDT", timeframe="1h")
    finally:
        db.close()
    return {g['direction']: g for g in genomes}


def test_discover_genomes_loss_scenario_end_to_end(tmp_path):
    genomes = _run_discovery_for_scenario(tmp_path, "loss")
    long_genome = genomes["LONG"]
    assert long_genome['total_occurrences'] == 1
    assert long_genome['wins'] == 0   # SL zuerst getroffen -> kein Win, trotz spaeterem Spike ueber TP


def test_discover_genomes_win_scenario_end_to_end(tmp_path):
    genomes = _run_discovery_for_scenario(tmp_path, "win")
    long_genome = genomes["LONG"]
    assert long_genome['total_occurrences'] == 1
    assert long_genome['wins'] == 1


def test_discover_genomes_timeout_scenario_end_to_end(tmp_path):
    genomes = _run_discovery_for_scenario(tmp_path, "timeout")
    long_genome = genomes["LONG"]
    assert long_genome['total_occurrences'] == 1
    assert long_genome['wins'] == 0   # TIMEOUT zaehlt nicht als Win (wie backtester._compute_stats)
