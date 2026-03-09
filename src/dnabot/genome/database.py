# src/dnabot/genome/database.py
# SQLite-Datenbank für das Genome-System
#
# Tabellen:
#   genomes   — alle entdeckten Muster mit Statistiken
#   scan_log  — Protokoll der Discovery-Läufe
#
# Per-Regime-Tracking:
#   occ_trend / wins_trend   — Vorkommen + Wins im TREND-Regime
#   occ_range / wins_range   — Vorkommen + Wins im RANGE-Regime
#   occ_neutral / wins_neutral — Vorkommen + Wins im NEUTRAL-Regime
#   active_regimes           — JSON-Liste der Regime, in denen das Genome aktiv ist

import sqlite3
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import os

logger = logging.getLogger(__name__)


def _genome_id(sequence: str, market: str, timeframe: str, direction: str) -> str:
    """Erzeugt einen deterministischen Hash-ID für ein Genome."""
    raw = f"{sequence}::{market}::{timeframe}::{direction}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


class GenomeDB:
    """
    Thread-sicheres SQLite-Interface für die Genome-Datenbank.
    Jede Instanz öffnet ihre eigene Verbindung mit check_same_thread=False.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS genomes (
        genome_id           TEXT PRIMARY KEY,
        sequence            TEXT NOT NULL,
        market              TEXT NOT NULL,
        timeframe           TEXT NOT NULL,
        direction           TEXT NOT NULL,
        seq_length          INTEGER NOT NULL,
        total_occurrences   INTEGER DEFAULT 0,
        wins                INTEGER DEFAULT 0,
        sum_move_pct        REAL DEFAULT 0.0,
        avg_move_pct        REAL DEFAULT 0.0,
        score               REAL DEFAULT 0.0,
        active              INTEGER DEFAULT 0,
        occ_trend           INTEGER DEFAULT 0,
        wins_trend          INTEGER DEFAULT 0,
        occ_range           INTEGER DEFAULT 0,
        wins_range          INTEGER DEFAULT 0,
        occ_neutral         INTEGER DEFAULT 0,
        wins_neutral        INTEGER DEFAULT 0,
        active_regimes      TEXT DEFAULT '[]',
        discovered_at       TEXT NOT NULL,
        last_updated        TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS scan_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        market              TEXT NOT NULL,
        timeframe           TEXT NOT NULL,
        scanned_at          TEXT NOT NULL,
        candles_processed   INTEGER DEFAULT 0,
        new_genomes         INTEGER DEFAULT 0,
        updated_genomes     INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_genomes_market_tf
        ON genomes (market, timeframe, active);

    CREATE INDEX IF NOT EXISTS idx_genomes_sequence
        ON genomes (sequence, market, timeframe, direction);
    """

    # Regime-Column-Mapping: regime → (occ_col, wins_col)
    _REGIME_COLS = {
        'TREND':   ('occ_trend',   'wins_trend'),
        'RANGE':   ('occ_range',   'wins_range'),
        'NEUTRAL': ('occ_neutral', 'wins_neutral'),
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()
        logger.debug(f"GenomeDB initialisiert: {db_path}")

    def _init_schema(self):
        for statement in self.SCHEMA.strip().split(";"):
            s = statement.strip()
            if s:
                self._conn.execute(s)
        self._conn.commit()
        self._migrate()

    def _migrate(self):
        """Fügt fehlende Spalten zu bestehenden DBs hinzu (rückwärtskompatibel)."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(genomes)")}
        migrations = [
            ("occ_trend",      "INTEGER DEFAULT 0"),
            ("wins_trend",     "INTEGER DEFAULT 0"),
            ("occ_range",      "INTEGER DEFAULT 0"),
            ("wins_range",     "INTEGER DEFAULT 0"),
            ("occ_neutral",    "INTEGER DEFAULT 0"),
            ("wins_neutral",   "INTEGER DEFAULT 0"),
            ("active_regimes", "TEXT DEFAULT '[]'"),
        ]
        for col, definition in migrations:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE genomes ADD COLUMN {col} {definition}")
                logger.info(f"DB Migration: Spalte '{col}' hinzugefügt.")
        self._conn.commit()

    def close(self):
        self._conn.close()

    # -------------------------------------------------------------------------
    # Genome CRUD
    # -------------------------------------------------------------------------

    def upsert_genome_outcome(
        self,
        sequence: str,
        market: str,
        timeframe: str,
        direction: str,
        seq_length: int,
        is_win: bool,
        move_pct: float,
        regime: str = 'NEUTRAL',
    ) -> bool:
        """
        Erstellt oder aktualisiert ein Genome mit einem Trade-Ergebnis.
        Inkrementiert die richtigen per-Regime-Zähler.
        Gibt True zurück wenn es ein neues Genome war, sonst False.
        """
        gid = _genome_id(sequence, market, timeframe, direction)
        now = datetime.now(timezone.utc).isoformat()

        # Regime auf bekannte Werte beschränken (HIGH_VOL zählen wir nicht)
        regime_key = regime if regime in self._REGIME_COLS else 'NEUTRAL'
        occ_col, wins_col = self._REGIME_COLS[regime_key]

        existing = self._conn.execute(
            "SELECT genome_id, total_occurrences, wins, sum_move_pct FROM genomes WHERE genome_id = ?",
            (gid,)
        ).fetchone()

        if existing is None:
            self._conn.execute(f"""
                INSERT INTO genomes
                    (genome_id, sequence, market, timeframe, direction, seq_length,
                     total_occurrences, wins, sum_move_pct, avg_move_pct, score,
                     active, {occ_col}, {wins_col}, active_regimes, discovered_at, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0.0, 0, 1, ?, '[]', ?, ?)
            """, (
                gid, sequence, market, timeframe, direction, seq_length,
                1 if is_win else 0,
                move_pct,
                move_pct,
                1 if is_win else 0,
                now, now
            ))
            self._conn.commit()
            return True
        else:
            total = existing['total_occurrences'] + 1
            wins = existing['wins'] + (1 if is_win else 0)
            sum_move = existing['sum_move_pct'] + move_pct
            avg_move = sum_move / total
            self._conn.execute(f"""
                UPDATE genomes
                SET total_occurrences = ?,
                    wins = ?,
                    sum_move_pct = ?,
                    avg_move_pct = ?,
                    {occ_col} = {occ_col} + 1,
                    {wins_col} = {wins_col} + ?,
                    last_updated = ?
                WHERE genome_id = ?
            """, (total, wins, sum_move, avg_move, 1 if is_win else 0, now, gid))
            self._conn.commit()
            return False

    def get_genome(
        self, sequence: str, market: str, timeframe: str, direction: str
    ) -> Optional[dict]:
        """Liest ein spezifisches Genome aus der DB."""
        gid = _genome_id(sequence, market, timeframe, direction)
        row = self._conn.execute(
            "SELECT * FROM genomes WHERE genome_id = ?", (gid,)
        ).fetchone()
        return dict(row) if row else None

    def get_active_genomes_for_market(self, market: str, timeframe: str) -> list[dict]:
        """Gibt alle aktiven Genomes für ein Markt/Timeframe zurück."""
        rows = self._conn.execute("""
            SELECT * FROM genomes
            WHERE market = ? AND timeframe = ? AND active = 1
            ORDER BY score DESC
        """, (market, timeframe)).fetchall()
        return [dict(r) for r in rows]

    def get_all_genomes(self, market: str = None, timeframe: str = None) -> list[dict]:
        """Gibt alle Genomes zurück, optional gefiltert."""
        if market and timeframe:
            rows = self._conn.execute(
                "SELECT * FROM genomes WHERE market = ? AND timeframe = ?",
                (market, timeframe)
            ).fetchall()
        elif market:
            rows = self._conn.execute(
                "SELECT * FROM genomes WHERE market = ?", (market,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM genomes").fetchall()
        return [dict(r) for r in rows]

    def update_genome_evolution(
        self,
        genome_id: str,
        score: float,
        active: bool,
        active_regimes: list,
    ):
        """
        Aktualisiert Score, Aktivierungsstatus und aktive Regime nach einem Evolver-Lauf.

        Args:
            active_regimes: Liste der Regime, in denen das Genome profitabel ist.
                            Z.B. ["RANGE", "NEUTRAL"] oder [] (kein aktives Regime)
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            UPDATE genomes
            SET score = ?, active = ?, active_regimes = ?, last_updated = ?
            WHERE genome_id = ?
        """, (score, 1 if active else 0, json.dumps(active_regimes), now, genome_id))
        self._conn.commit()

    # Rückwärtskompatibilität — wird intern nicht mehr genutzt
    def update_genome_score(self, genome_id: str, score: float, active: bool,
                             primary_regime: str = None):
        """Legacy-Methode. Nutze update_genome_evolution() für neue Aufrufe."""
        self.update_genome_evolution(genome_id, score, active, active_regimes=[])

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    def get_db_summary(self) -> dict:
        """Gibt eine Zusammenfassung der Datenbank zurück."""
        total = self._conn.execute("SELECT COUNT(*) FROM genomes").fetchone()[0]
        active = self._conn.execute("SELECT COUNT(*) FROM genomes WHERE active = 1").fetchone()[0]
        markets = self._conn.execute(
            "SELECT DISTINCT market FROM genomes"
        ).fetchall()
        top = self._conn.execute("""
            SELECT sequence, market, timeframe, direction, score,
                   wins, total_occurrences, active_regimes,
                   CAST(wins AS REAL) / total_occurrences AS winrate
            FROM genomes
            WHERE active = 1 AND total_occurrences >= 100
            ORDER BY score DESC
            LIMIT 10
        """).fetchall()

        return {
            "total_genomes": total,
            "active_genomes": active,
            "markets": [r[0] for r in markets],
            "top_patterns": [dict(r) for r in top],
        }

    # -------------------------------------------------------------------------
    # Scan Log
    # -------------------------------------------------------------------------

    def log_scan(
        self, market: str, timeframe: str,
        candles_processed: int, new_genomes: int, updated_genomes: int
    ):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO scan_log
                (market, timeframe, scanned_at, candles_processed, new_genomes, updated_genomes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (market, timeframe, now, candles_processed, new_genomes, updated_genomes))
        self._conn.commit()

    def get_last_scan(self, market: str, timeframe: str) -> Optional[dict]:
        row = self._conn.execute("""
            SELECT * FROM scan_log
            WHERE market = ? AND timeframe = ?
            ORDER BY scanned_at DESC LIMIT 1
        """, (market, timeframe)).fetchone()
        return dict(row) if row else None
