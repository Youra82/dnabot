# src/dnabot/genome/risk_genome_db.py
# SQLite-Datenbank fuer Risiko-/Exit-Gene (momentum_exit-Strategie).
#
# Spiegelt genome/database.py 1:1 im Aufbau, aber ein "Gen" ist hier NICHT
# ein Kerzenmuster zur Richtungsvorhersage, sondern eine Kombination aus
# Risiko-/Exit-Parametern (seq_len, rr_ratio, trailing_callback_pct,
# risk_pct). Siehe Fund AQ/AR in research_dnabot_direction_calibration.md:
# der validierte Edge kommt aus GEZIELT ENTWORFENEN Exit-Parametern bei
# nicht-praediktivem Einstieg, nicht aus Richtungs-Vorhersage.
#
# Tabellen:
#   risk_genes            — alle Kandidaten-Gene mit Performance-Statistiken
#   risk_gene_occurrences — ein Eintrag pro geschlossenem Trade (Self-Learning)
#
# Selektionskriterium ist NICHT Winrate (wie bei genome/database.py),
# sondern Calmar-Ratio (PnL / MaxDD) -- risikoadjustierte Performance, siehe
# Fund AN: bei duennem Edge ist die Positionsgroesse/Drawdown wichtiger als
# die reine Trefferquote.

import sqlite3
import hashlib
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _risk_gene_id(market: str, timeframe: str, seq_len: int, rr_ratio: float,
                   trailing_pct: float, risk_pct: float) -> str:
    raw = f"{market}::{timeframe}::{seq_len}::{rr_ratio}::{trailing_pct}::{risk_pct}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


class RiskGenomeDB:
    """Thread-sicheres SQLite-Interface fuer die Risiko-Gen-Datenbank."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS risk_genes (
        risk_gene_id        TEXT PRIMARY KEY,
        market              TEXT NOT NULL,
        timeframe           TEXT NOT NULL,
        seq_len             INTEGER NOT NULL,
        rr_ratio            REAL NOT NULL,
        trailing_pct        REAL NOT NULL,
        risk_pct            REAL NOT NULL,
        total_trades        INTEGER DEFAULT 0,
        wins                INTEGER DEFAULT 0,
        total_pnl_pct       REAL DEFAULT 0.0,
        max_dd_pct          REAL DEFAULT 0.0,
        peak_equity         REAL DEFAULT 100.0,
        equity              REAL DEFAULT 100.0,
        calmar              REAL DEFAULT 0.0,
        active              INTEGER DEFAULT 0,
        discovered_at       TEXT NOT NULL,
        last_seen           TEXT NOT NULL,
        last_updated        TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS risk_gene_occurrences (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        risk_gene_id        TEXT NOT NULL,
        entry_time          TEXT NOT NULL,
        exit_time           TEXT NOT NULL,
        outcome             TEXT NOT NULL,
        pnl_pct             REAL NOT NULL,
        sl_pct              REAL NOT NULL,
        source              TEXT NOT NULL DEFAULT 'live'
    );

    CREATE INDEX IF NOT EXISTS idx_risk_genes_market_tf
        ON risk_genes (market, timeframe, active);

    CREATE INDEX IF NOT EXISTS idx_risk_occurrences_gene
        ON risk_gene_occurrences (risk_gene_id, entry_time);
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._batch_mode = False
        for statement in self.SCHEMA.strip().split(";"):
            s = statement.strip()
            if s:
                self._conn.execute(s)
        self._conn.commit()
        logger.debug(f"RiskGenomeDB initialisiert: {db_path}")

    def _commit(self):
        if not self._batch_mode:
            self._conn.commit()

    @contextmanager
    def batch_writes(self):
        self._batch_mode = True
        try:
            yield
        finally:
            self._batch_mode = False
            self._conn.commit()

    def close(self):
        self._conn.close()

    # -------------------------------------------------------------------------
    # Risk-Gene CRUD
    # -------------------------------------------------------------------------

    def upsert_candidate(self, market: str, timeframe: str, seq_len: int,
                          rr_ratio: float, trailing_pct: float, risk_pct: float) -> str:
        """Legt ein Kandidaten-Gen an, falls es noch nicht existiert. Gibt die ID zurueck."""
        gid = _risk_gene_id(market, timeframe, seq_len, rr_ratio, trailing_pct, risk_pct)
        now = datetime.now(timezone.utc).isoformat()
        existing = self._conn.execute(
            "SELECT risk_gene_id FROM risk_genes WHERE risk_gene_id = ?", (gid,)
        ).fetchone()
        if existing is None:
            self._conn.execute("""
                INSERT INTO risk_genes
                    (risk_gene_id, market, timeframe, seq_len, rr_ratio, trailing_pct, risk_pct,
                     discovered_at, last_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (gid, market, timeframe, seq_len, rr_ratio, trailing_pct, risk_pct, now, now, now))
            self._commit()
        return gid

    def record_trade(self, risk_gene_id: str, entry_time: str, exit_time: str,
                      outcome: str, pnl_pct: float, sl_pct: float, source: str = 'live'):
        """
        Verbucht EIN Trade-Ergebnis fuer ein Risiko-Gen -- Self-Learning-Aequivalent
        zu genome/database.py::upsert_genome_outcome(). source='backtest' fuer
        Discovery-Seeding, 'live' fuer echte, geschlossene Trades.
        Equity/Peak/MaxDD werden inkrementell fortgeschrieben (einfache
        Kompoundierung mit risk_pct, wie simulate_trade_subset()).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO risk_gene_occurrences
                (risk_gene_id, entry_time, exit_time, outcome, pnl_pct, sl_pct, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (risk_gene_id, entry_time, exit_time, outcome, pnl_pct, sl_pct, source))

        row = self._conn.execute(
            "SELECT total_trades, wins, total_pnl_pct, peak_equity, equity, risk_pct "
            "FROM risk_genes WHERE risk_gene_id = ?", (risk_gene_id,)
        ).fetchone()
        if row is None:
            self._commit()
            return

        sl_pct_safe = max(sl_pct, 0.01)
        risk_amount = row['equity'] * (row['risk_pct'] / 100.0)
        pnl_abs = risk_amount * (pnl_pct / sl_pct_safe)
        new_equity = row['equity'] + pnl_abs
        new_peak = max(row['peak_equity'], new_equity)
        dd = ((new_peak - new_equity) / new_peak * 100.0) if new_peak > 0 else 0.0

        total = row['total_trades'] + 1
        wins = row['wins'] + (1 if outcome == 'WIN' else 0)
        total_pnl_pct = (new_equity - 100.0)  # Basis 100 wie simulate_trade_subset()
        old_max_dd = self._conn.execute(
            "SELECT max_dd_pct FROM risk_genes WHERE risk_gene_id = ?", (risk_gene_id,)
        ).fetchone()['max_dd_pct']
        max_dd = max(old_max_dd, dd)
        calmar = (total_pnl_pct / max_dd) if max_dd > 0 else total_pnl_pct

        self._conn.execute("""
            UPDATE risk_genes
            SET total_trades = ?, wins = ?, total_pnl_pct = ?, max_dd_pct = ?,
                peak_equity = ?, equity = ?, calmar = ?, last_seen = ?, last_updated = ?
            WHERE risk_gene_id = ?
        """, (total, wins, total_pnl_pct, max_dd, new_peak, new_equity, calmar, now, now, risk_gene_id))
        self._commit()

    def set_active(self, risk_gene_id: str, active: bool):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE risk_genes SET active = ?, last_updated = ? WHERE risk_gene_id = ?",
            (1 if active else 0, now, risk_gene_id)
        )
        self._commit()

    def get_active_gene(self, market: str, timeframe: str) -> Optional[dict]:
        row = self._conn.execute("""
            SELECT * FROM risk_genes
            WHERE market = ? AND timeframe = ? AND active = 1
            ORDER BY calmar DESC LIMIT 1
        """, (market, timeframe)).fetchone()
        return dict(row) if row else None

    def get_candidates(self, market: str, timeframe: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM risk_genes WHERE market = ? AND timeframe = ? ORDER BY calmar DESC",
            (market, timeframe)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_market_pairs(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT DISTINCT market, timeframe FROM risk_genes ORDER BY market, timeframe"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
