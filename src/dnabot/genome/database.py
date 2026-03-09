# src/dnabot/genome/database.py
# SQLite-Datenbank für das Genome-System
#
# Tabellen:
#   genomes   — alle entdeckten Muster mit Statistiken
#   scan_log  — Protokoll der Discovery-Läufe

import sqlite3
import hashlib
import math
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
    ) -> bool:
        """
        Erstellt oder aktualisiert ein Genome mit einem Trade-Ergebnis.
        Gibt True zurück wenn es ein neues Genome war, sonst False.
        """
        gid = _genome_id(sequence, market, timeframe, direction)
        now = datetime.now(timezone.utc).isoformat()

        existing = self._conn.execute(
            "SELECT genome_id, total_occurrences, wins, sum_move_pct FROM genomes WHERE genome_id = ?",
            (gid,)
        ).fetchone()

        if existing is None:
            self._conn.execute("""
                INSERT INTO genomes
                    (genome_id, sequence, market, timeframe, direction, seq_length,
                     total_occurrences, wins, sum_move_pct, avg_move_pct, score,
                     active, discovered_at, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0.0, 0, ?, ?)
            """, (
                gid, sequence, market, timeframe, direction, seq_length,
                1 if is_win else 0,
                move_pct,
                move_pct,   # avg_move_pct bei erstem Eintrag = move_pct
                now, now
            ))
            self._conn.commit()
            return True
        else:
            total = existing['total_occurrences'] + 1
            wins = existing['wins'] + (1 if is_win else 0)
            sum_move = existing['sum_move_pct'] + move_pct
            avg_move = sum_move / total
            self._conn.execute("""
                UPDATE genomes
                SET total_occurrences = ?,
                    wins = ?,
                    sum_move_pct = ?,
                    avg_move_pct = ?,
                    last_updated = ?
                WHERE genome_id = ?
            """, (total, wins, sum_move, avg_move, now, gid))
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

    def update_genome_score(self, genome_id: str, score: float, active: bool):
        """Aktualisiert Score und Aktivierungsstatus eines Genomes."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            UPDATE genomes SET score = ?, active = ?, last_updated = ?
            WHERE genome_id = ?
        """, (score, 1 if active else 0, now, genome_id))
        self._conn.commit()

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
                   wins, total_occurrences,
                   CAST(wins AS REAL) / total_occurrences AS winrate
            FROM genomes
            WHERE active = 1 AND total_occurrences >= 20
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
