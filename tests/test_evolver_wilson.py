# tests/test_evolver_wilson.py
"""
Testet die Umstellung des Evolver-Aktivierungs-Gates von einem festen
min_samples-Cutoff auf eine Wilson-Score-Konfidenzuntergrenze (siehe
src/dnabot/genome/scoring.py und evolver.py-Moduldocstring fuer den
Hintergrund: ein fester Cutoff steckte in einer Zwickmuehle zwischen
Kleinstichproben-Rauschen (min_samples=2) und praktisch nichts mehr
aktivieren (min_samples=30) bei der typischen Vorkommen-Sparsity der
Genome-Discovery).
"""
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

import pytest

from dnabot.genome.scoring import wilson_lower_bound
from dnabot.genome.evolver import evolve
from dnabot.genome.database import GenomeDB


# ── 1. Unit-Tests: wilson_lower_bound ───────────────────────────────────────

def test_wilson_zero_samples_is_zero():
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_single_win_stays_far_below_point_estimate():
    # 1/1 = 100% Punktschaetzung, aber die Konfidenzuntergrenze muss deutlich
    # niedriger liegen -- genau das soll Kleinstichproben-Rauschen ausfiltern.
    lb = wilson_lower_bound(1, 1)
    assert 0.0 < lb < 0.45


def test_wilson_single_loss_is_zero():
    assert wilson_lower_bound(0, 1) == 0.0


def test_wilson_more_samples_same_ratio_increases_lower_bound():
    # Gleiche Punktschaetzung (60%), aber mehr Beobachtungen -> naeher am
    # Punktschaetzer, hoehere Untergrenze.
    lb_small = wilson_lower_bound(6, 10)
    lb_large = wilson_lower_bound(600, 1000)
    assert lb_large > lb_small
    assert lb_large < 0.6   # bleibt immer noch unter der reinen Punktschaetzung
    assert lb_large == pytest.approx(0.6, abs=0.03)  # konvergiert bei grossem n dorthin


def test_wilson_50_of_100_clears_45_percent_threshold_reference_case():
    # 30/50 = 60% roh; Referenzwert konkret geprueft, damit die Formel nicht
    # versehentlich durch eine andere Naeherung ersetzt wird.
    lb = wilson_lower_bound(30, 50)
    assert lb == pytest.approx(0.4838, abs=0.001)
    assert lb >= 0.45


# ── 2. Integrationstest: evolve() aktiviert nur noch konfidenz-basiert ─────

def test_evolve_rejects_tiny_sample_but_accepts_confident_larger_sample(tmp_path):
    db_path = str(tmp_path / "wilson_test.db")
    db = GenomeDB(db_path)
    try:
        # Genome A: 1 Vorkommen, 1 Win (100% roh) -- darf NICHT aktivieren,
        # obwohl die alte reine Winrate-Schwelle (>=45%) das durchgelassen haette.
        db.upsert_genome_outcome(
            sequence="AAAA", market="TEST/USDT:USDT", timeframe="1h", direction="LONG",
            seq_length=4, is_win=True, move_pct=1.0, regime="NEUTRAL",
        )

        # Genome B: 50 Vorkommen, 30 Wins (60% roh, Wilson-Untergrenze ~0.48) --
        # muss aktivieren.
        for _ in range(30):
            db.upsert_genome_outcome(
                sequence="BBBB", market="TEST/USDT:USDT", timeframe="1h", direction="LONG",
                seq_length=4, is_win=True, move_pct=1.0, regime="NEUTRAL",
            )
        for _ in range(20):
            db.upsert_genome_outcome(
                sequence="BBBB", market="TEST/USDT:USDT", timeframe="1h", direction="LONG",
                seq_length=4, is_win=False, move_pct=1.0, regime="NEUTRAL",
            )

        # half_life_days=0 -> kein Decay, score_threshold=0 -> isoliert testen
        # wir nur das Winrate-Gate, nicht das Score-Gate.
        evolve(db, market="TEST/USDT:USDT", timeframe="1h",
               min_samples=1, min_winrate=0.45, score_threshold=0.0, half_life_days=0)

        genomes = {g['sequence']: g for g in db.get_all_genomes(market="TEST/USDT:USDT", timeframe="1h")}
        assert genomes["AAAA"]["active"] == 0
        assert genomes["BBBB"]["active"] == 1
    finally:
        db.close()
