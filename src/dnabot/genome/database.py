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
#
# Zeitstempel:
#   discovered_at — wann das Genome erstmals entdeckt wurde (unveränderlich)
#   last_seen     — wann das Genome zuletzt in Discovery/Live-Trade beobachtet wurde
#                   (Basis für Decay-Weighting)
#   last_updated  — wann der DB-Eintrag zuletzt geschrieben wurde (inkl. Evolver)

import sqlite3
import hashlib
import json
import logging
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
import os

from dnabot.genome.scoring import compute_score as _compute_score, wilson_lower_bound as _wilson_lower_bound

logger = logging.getLogger(__name__)


def _genome_id(sequence: str, market: str, timeframe: str, direction: str) -> str:
    """Erzeugt einen deterministischen Hash-ID für ein Genome."""
    raw = f"{sequence}::{market}::{timeframe}::{direction}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _decay_as_of(occurred_iso: str, cutoff_iso: str, effective_half_life: float) -> float:
    """Wie evolver.py::compute_decay(), aber relativ zu einem simulierten
    Stichtag (cutoff_iso) statt zur echten aktuellen Zeit -- Basis fuer
    zeitpunktbezogene Backtest-Auswertung ohne Hindsight-Bias."""
    if effective_half_life <= 0:
        return 1.0
    try:
        occurred = datetime.fromisoformat(occurred_iso)
        cutoff = datetime.fromisoformat(cutoff_iso)
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        age_days = max((cutoff - occurred).days, 0)
        return math.exp(-age_days / effective_half_life)
    except Exception:
        return 1.0


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
        last_seen           TEXT NOT NULL,
        last_updated        TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS scan_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        market              TEXT NOT NULL,
        timeframe           TEXT NOT NULL,
        scanned_at          TEXT NOT NULL,
        candles_processed   INTEGER DEFAULT 0,
        new_genomes         INTEGER DEFAULT 0,
        updated_genomes     INTEGER DEFAULT 0,
        data_end_date       TEXT DEFAULT NULL
    );

    CREATE TABLE IF NOT EXISTS genome_occurrences (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        genome_id           TEXT NOT NULL,
        occurred_at         TEXT NOT NULL,
        resolved_at         TEXT NOT NULL,
        is_win              INTEGER NOT NULL,
        move_pct            REAL NOT NULL,
        regime              TEXT NOT NULL DEFAULT 'NEUTRAL'
    );

    CREATE INDEX IF NOT EXISTS idx_genomes_market_tf
        ON genomes (market, timeframe, active);

    CREATE INDEX IF NOT EXISTS idx_genomes_sequence
        ON genomes (sequence, market, timeframe, direction);

    CREATE INDEX IF NOT EXISTS idx_occurrences_genome_time
        ON genome_occurrences (genome_id, occurred_at);
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
        self._conn.execute("PRAGMA cache_size = -8192;")      # max 8 MB Page-Cache
        self._conn.execute("PRAGMA temp_store = FILE;")       # Temp-Tabellen auf Disk statt RAM
        self._conn.execute("PRAGMA wal_autocheckpoint = 100;") # WAL klein halten
        self._batch_mode = False
        self._init_schema()
        logger.debug(f"GenomeDB initialisiert: {db_path}")

    def _commit(self):
        """Commit, der waehrend batch_writes() unterdrueckt wird."""
        if not self._batch_mode:
            self._conn.commit()

    @contextmanager
    def batch_writes(self):
        """
        Unterdrueckt zwischenzeitliche Commits waehrend des Blocks (ein
        einziger Commit am Ende statt einem pro upsert_genome_outcome()-
        Aufruf) -- discover_genomes() kann pro Scan zehntausende Occurrences
        schreiben, mit Einzel-Commits dominiert das die Laufzeit. Gedacht fuer
        Bulk-Discovery-Läufe, die (anders als der normale inkrementelle
        Live-Scan) ohnehin viele Kerzen auf einmal verarbeiten, z.B.
        analysis/alphabet_optimizer.py, das pro Optuna-Trial einen vollen
        Rescan durchfuehrt.
        """
        self._batch_mode = True
        try:
            yield
        finally:
            self._batch_mode = False
            self._conn.commit()

    def _init_schema(self):
        for statement in self.SCHEMA.strip().split(";"):
            s = statement.strip()
            if s:
                self._conn.execute(s)
        self._conn.commit()
        self._migrate()
        self._migrate_scan_log()
        self._migrate_occurrences()

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
            # last_seen = Basis für Decay; Fallback auf last_updated für alte DBs
            ("last_seen",      f"TEXT DEFAULT '{datetime.now(timezone.utc).isoformat()}'"),
        ]
        for col, definition in migrations:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE genomes ADD COLUMN {col} {definition}")
                logger.info(f"DB Migration: Spalte '{col}' hinzugefügt.")
        self._conn.commit()

    def _migrate_scan_log(self):
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(scan_log)")}
        if 'data_end_date' not in existing:
            self._conn.execute("ALTER TABLE scan_log ADD COLUMN data_end_date TEXT DEFAULT NULL")
            logger.info("DB Migration: scan_log Spalte 'data_end_date' hinzugefügt.")
        if 'alphabet_hash' not in existing:
            self._conn.execute("ALTER TABLE scan_log ADD COLUMN alphabet_hash TEXT DEFAULT NULL")
            logger.info("DB Migration: scan_log Spalte 'alphabet_hash' hinzugefügt.")
        self._conn.commit()

    def _migrate_occurrences(self):
        """resolved_at nachtraeglich hinzufuegen (siehe get_genome_as_of()-Docstring:
        occurred_at markiert den ENTRY-Zeitpunkt eines Vorkommens, nicht den Zeitpunkt,
        ab dem sein Ergebnis tatsaechlich feststand -- ein Backtest, der nur nach
        occurred_at < cutoff filtert, kann Vorkommen "kennen", deren SL/TP/Timeout in
        Wahrheit erst bis zu discovery_horizon Kerzen SPAETER feststand. Fuer
        Alt-Zeilen ohne echten Aufloesungszeitpunkt bleibt occurred_at als Fallback
        (identisch zum bisherigen, leicht optimistischen Verhalten) -- erst ein
        frischer discover_genomes()-Lauf schreibt den echten resolved_at-Wert."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(genome_occurrences)")}
        if 'resolved_at' not in existing:
            self._conn.execute("ALTER TABLE genome_occurrences ADD COLUMN resolved_at TEXT")
            self._conn.execute("UPDATE genome_occurrences SET resolved_at = occurred_at WHERE resolved_at IS NULL")
            logger.info("DB Migration: genome_occurrences Spalte 'resolved_at' hinzugefügt (Alt-Zeilen: Fallback = occurred_at).")
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
        occurred_at: str = None,
        resolved_at: str = None,
    ) -> bool:
        """
        Erstellt oder aktualisiert ein Genome mit einem Trade-Ergebnis.
        Inkrementiert die richtigen per-Regime-Zähler.
        Setzt last_seen = jetzt (Basis für Decay).
        Gibt True zurück wenn es ein neues Genome war, sonst False.

        occurred_at: Zeitpunkt DIESES Vorkommens (z.B. Kerzen-Zeitstempel bei
        Discovery-Scans historischer Daten). Standardmaessig "jetzt" (fuer
        Live-Self-Learning-Aufrufe, wo das Vorkommen tatsaechlich jetzt
        passiert ist). Wird zusaetzlich zu den Aggregat-Spalten in
        genome_occurrences gespeichert -- Basis fuer zeitpunktbezogene
        ("as-of") Backtest-Auswertungen ohne Hindsight-Bias (siehe
        get_genome_as_of()).

        resolved_at: Zeitpunkt, ab dem das Ergebnis (is_win) DIESES Vorkommens
        tatsaechlich feststand -- bei Discovery-Scans ist das NICHT occurred_at
        (Entry-Kerze), sondern die Kerze, an der SL/TP/Timeout eintrat, bis zu
        discovery_horizon Kerzen spaeter (siehe discovery.py::discover_genomes()).
        Ohne das wuerde get_genome_as_of() ein Vorkommen schon als "bekannt"
        zaehlen, sobald occurred_at vor dem Cutoff liegt, obwohl das Ergebnis in
        Wahrheit erst mehrere Kerzen spaeter feststand -- ein kleineres, aber
        echtes Lookahead-Leck zusaetzlich zum grossen, bereits gefixten Bug (alle
        Backtests nutzten frueher die All-Time-DB statt zeitpunktbezogener Scores).
        None (Default) = resolved_at = occurred_at, das bestehende Verhalten fuer
        Live-Self-Learning-Aufrufe (dort passiert Insert erst NACH Trade-Abschluss,
        occurred_at ist dort also bereits der Aufloesungszeitpunkt).
        """
        gid = _genome_id(sequence, market, timeframe, direction)
        now = datetime.now(timezone.utc).isoformat()
        occ_ts = occurred_at or now
        resolved_ts = resolved_at or occ_ts

        # Regime auf bekannte Werte beschränken (HIGH_VOL zählen wir nicht)
        regime_key = regime if regime in self._REGIME_COLS else 'NEUTRAL'
        occ_col, wins_col = self._REGIME_COLS[regime_key]

        self._conn.execute(
            "INSERT INTO genome_occurrences (genome_id, occurred_at, resolved_at, is_win, move_pct, regime) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (gid, occ_ts, resolved_ts, 1 if is_win else 0, move_pct, regime_key)
        )

        existing = self._conn.execute(
            "SELECT genome_id, total_occurrences, wins, sum_move_pct FROM genomes WHERE genome_id = ?",
            (gid,)
        ).fetchone()

        if existing is None:
            self._conn.execute(f"""
                INSERT INTO genomes
                    (genome_id, sequence, market, timeframe, direction, seq_length,
                     total_occurrences, wins, sum_move_pct, avg_move_pct, score,
                     active, {occ_col}, {wins_col}, active_regimes,
                     discovered_at, last_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0.0, 0, 1, ?, '[]', ?, ?, ?)
            """, (
                gid, sequence, market, timeframe, direction, seq_length,
                1 if is_win else 0,
                move_pct,
                move_pct,
                1 if is_win else 0,
                now, now, now
            ))
            self._commit()
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
                    last_seen = ?,
                    last_updated = ?
                WHERE genome_id = ?
            """, (total, wins, sum_move, avg_move, 1 if is_win else 0, now, now, gid))
            self._commit()
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

    def get_genome_as_of(
        self,
        sequence: str,
        market: str,
        timeframe: str,
        direction: str,
        cutoff_iso: str,
        regime: str = None,
        min_samples: int = 100,
        min_winrate: float = 0.45,
        score_threshold: float = 0.08,
        half_life_days: float = 180.0,
        vol_factor: float = 1.0,
    ) -> Optional[dict]:
        """
        Wie get_genome(), aber Score/Winrate/Aktivierung werden NUR aus
        Occurrences berechnet, die VOR cutoff_iso passiert sind (point-in-
        time), statt aus der aktuellen All-Time-Aggregation der genomes-
        Tabelle. Repliziert die Score-/Decay-/Aktivierungs-Formel aus
        evolver.py::evolve() 1:1, aber relativ zu cutoff_iso statt "jetzt".

        Gedacht für Backtests, die keinen Hindsight-Bias haben sollen:
        genome_logic.py (live) nutzt weiterhin get_genome() unverändert.

        Gibt None zurück, wenn keine Occurrences vor cutoff_iso existieren
        (z.B. weil die DB noch keine genome_occurrences-Historie hat) oder
        kein Regime die Aktivierungs-Schwellen erreicht.

        Regime-Semantik repliziert jetzt evolver.py::evolve() 1:1 (vorher
        divergierte sie: diese Funktion waehlte nur das EINE bestbewertete
        Regime und verlangte spaeter exakte Gleichheit mit current_regime,
        waehrend live/evolver.py ein Genome in MEHREREN Regimen gleichzeitig
        aktivieren kann -- active_regimes als Liste, current_regime muss nur
        IRGENDWO darin vorkommen. Die alte Exact-Match-Variante verwarf dadurch
        systematisch gueltige Signale, siehe backtester.py::_find_best_signal().
        Die zurueckgegebenen Stats (total_occurrences/wins/avg_move_pct) sind
        GLOBAL (alle Regime kombiniert, wie db.get_genome() live liefert) --
        nur die Aktivierungs-Pruefung selbst laeuft pro Regime.
        """
        gid = _genome_id(sequence, market, timeframe, direction)
        # Filter auf resolved_at (nicht occurred_at): ein Vorkommen darf erst
        # zaehlen, wenn sein Ergebnis tatsaechlich feststand -- bei
        # Discovery-Scans ist das bis zu discovery_horizon Kerzen NACH
        # occurred_at (siehe upsert_genome_outcome()-Docstring). Sonst kann ein
        # Backtest an Kerze C das Ergebnis eines Vorkommens "kennen", das in
        # Wahrheit erst nach C feststand -- ein kleines, aber echtes
        # Lookahead-Leck. ORDER BY bleibt occurred_at (Decay misst, wann das
        # MUSTER zuletzt auftrat, nicht wann sein Ergebnis feststand).
        rows = self._conn.execute(
            "SELECT is_win, move_pct, regime, occurred_at FROM genome_occurrences "
            "WHERE genome_id = ? AND resolved_at < ? ORDER BY occurred_at",
            (gid, cutoff_iso)
        ).fetchall()
        if not rows:
            return None

        vol_factor_clamped = max(0.5, min(vol_factor, 3.0))
        effective_half_life = half_life_days / vol_factor_clamped if half_life_days > 0 else 0.0

        total = len(rows)
        global_wins = sum(r['is_win'] for r in rows)
        global_avg_move = sum(r['move_pct'] for r in rows) / total
        global_winrate = global_wins / total

        def _regime_score(subset):
            occ = len(subset)
            if occ == 0:
                return 0, 0.0, 0.0, 0
            wins = sum(r['is_win'] for r in subset)
            winrate = wins / occ
            last_occurred = subset[-1]['occurred_at']
            decay = _decay_as_of(last_occurred, cutoff_iso, effective_half_life)
            effective_occ = occ * decay
            score = _compute_score(winrate, global_avg_move, effective_occ)
            return occ, winrate, score, wins

        # Wilson-Score-Konfidenzuntergrenze statt Punktschaetzung als
        # Aktivierungs-Kriterium -- siehe evolver.py::evolve() fuer
        # Begruendung (research_dnabot_direction_calibration.md Fund C/O),
        # muss hier identisch angewendet werden (feedback_live_backtest_
        # must_match.md).
        regimes_to_try = [regime] if regime else ['TREND', 'RANGE', 'NEUTRAL']
        active_regimes = []
        best_score = 0.0
        for r in regimes_to_try:
            subset = [row for row in rows if row['regime'] == r]
            occ, winrate, score, wins = _regime_score(subset)
            if occ < min_samples:
                continue
            wr_lower = _wilson_lower_bound(wins, occ)
            if wr_lower >= min_winrate and score >= score_threshold:
                active_regimes.append(r)
                best_score = max(best_score, score)

        if not active_regimes and total >= min_samples:
            last_occurred = rows[-1]['occurred_at']
            decay = _decay_as_of(last_occurred, cutoff_iso, effective_half_life)
            global_score = _compute_score(global_winrate, global_avg_move, total * decay)
            global_wr_lower = _wilson_lower_bound(global_wins, total)
            if global_wr_lower >= min_winrate and global_score >= score_threshold:
                active_regimes = ['NEUTRAL']
                best_score = global_score

        if not active_regimes:
            return None

        return {
            'genome_id': gid,
            'sequence': sequence,
            'market': market,
            'timeframe': timeframe,
            'direction': direction,
            'total_occurrences': total,
            'wins': global_wins,
            'avg_move_pct': global_avg_move,
            'score': best_score,
            'active_regimes': active_regimes,
            'active': True,
        }

    def get_active_genomes_for_market(self, market: str, timeframe: str) -> list[dict]:
        """Gibt alle aktiven Genomes für ein Markt/Timeframe zurück."""
        rows = self._conn.execute("""
            SELECT * FROM genomes
            WHERE market = ? AND timeframe = ? AND active = 1
            ORDER BY score DESC
        """, (market, timeframe)).fetchall()
        return [dict(r) for r in rows]

    def get_all_market_pairs(self) -> list[tuple[str, str]]:
        """Gibt alle eindeutigen (market, timeframe)-Paare aus der DB zurück."""
        rows = self._conn.execute(
            "SELECT DISTINCT market, timeframe FROM genomes ORDER BY market, timeframe"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
        Aktualisiert NUR last_updated, NICHT last_seen — Decay bleibt korrekt.

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

    def get_pair_stats(self) -> list[dict]:
        """Aggregierte Stats pro (market, timeframe) via SQL — kein Full-Table-Scan."""
        rows = self._conn.execute("""
            SELECT
                market, timeframe,
                COUNT(*) AS total,
                SUM(active) AS active,
                AVG(CASE WHEN active = 1 THEN score END) AS avg_score,
                AVG(CASE WHEN active = 1 THEN CAST(wins AS REAL) / MAX(total_occurrences, 1) END) AS avg_wr
            FROM genomes
            GROUP BY market, timeframe
            ORDER BY market, timeframe
        """).fetchall()
        return [dict(r) for r in rows]

    def get_db_summary(self) -> dict:
        """Gibt eine Zusammenfassung der Datenbank zurück."""
        total = self._conn.execute("SELECT COUNT(*) FROM genomes").fetchone()[0]
        active = self._conn.execute("SELECT COUNT(*) FROM genomes WHERE active = 1").fetchone()[0]
        markets = self._conn.execute(
            "SELECT DISTINCT market FROM genomes"
        ).fetchall()
        top = self._conn.execute("""
            SELECT sequence, market, timeframe, direction, score,
                   wins, total_occurrences, active_regimes, last_seen,
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
        candles_processed: int, new_genomes: int, updated_genomes: int,
        data_end_date: str = None, alphabet_hash: str = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO scan_log
                (market, timeframe, scanned_at, candles_processed, new_genomes, updated_genomes,
                 data_end_date, alphabet_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (market, timeframe, now, candles_processed, new_genomes, updated_genomes,
              data_end_date, alphabet_hash))
        self._commit()

    def get_last_scan(self, market: str, timeframe: str) -> Optional[dict]:
        row = self._conn.execute("""
            SELECT * FROM scan_log
            WHERE market = ? AND timeframe = ?
            ORDER BY scanned_at DESC LIMIT 1
        """, (market, timeframe)).fetchone()
        return dict(row) if row else None

    def delete_pair(self, market: str, timeframe: str):
        """
        Loescht alle Genome/Occurrences/Scan-Log-Eintraege fuer ein Pair.
        Noetig wenn sich das Encoder-Alphabet eines Pairs aendert -- alte
        Sequenz-Strings werden unter dem neuen Alphabet nie wieder erzeugt
        und blieben sonst als tote Eintraege liegen (siehe
        alphabet_store.py::alphabet_hash() und scan_and_learn.py).
        """
        gids = [r[0] for r in self._conn.execute(
            "SELECT genome_id FROM genomes WHERE market = ? AND timeframe = ?",
            (market, timeframe)
        ).fetchall()]
        if gids:
            placeholders = ",".join("?" * len(gids))
            self._conn.execute(
                f"DELETE FROM genome_occurrences WHERE genome_id IN ({placeholders})", gids
            )
        self._conn.execute("DELETE FROM genomes WHERE market = ? AND timeframe = ?", (market, timeframe))
        self._conn.execute("DELETE FROM scan_log WHERE market = ? AND timeframe = ?", (market, timeframe))
        self._conn.commit()
        logger.info(f"Pair geloescht (Alphabet-Wechsel): {market} ({timeframe}), {len(gids)} Genome entfernt.")
