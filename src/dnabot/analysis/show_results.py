# src/dnabot/analysis/show_results.py
# Genome-Analyse — 4 Modi
#
# Modus 1: Genome Bibliothek        — Top-Patterns + Backtest-Ergebnisse
# Modus 2: Regime-Analyse           — Welches Regime funktioniert wo am besten
# Modus 3: Decay-Status             — Welche Genome altern / drohen zu deaktivieren
# Modus 4: Interaktiver Chart       — Candlestick + Entry/Exit-Marker + Equity-Kurve
#
# Ausführung über show_results.sh oder direkt:
#   python3 src/dnabot/analysis/show_results.py --mode 1
#   python3 src/dnabot/analysis/show_results.py --mode 2 --symbol BTC/USDT:USDT --timeframe 4h

import os
import sys
import json
import glob
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.genome.database import GenomeDB

RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
DB_PATH     = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')

G  = '\033[0;32m'
B  = '\033[0;34m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
DIM = '\033[2m'
NC = '\033[0m'

SEP  = '=' * 72
SEP2 = '-' * 72


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_db() -> GenomeDB | None:
    if not os.path.exists(DB_PATH):
        print(f"{R}Genome-Datenbank nicht gefunden. Erst scan_and_learn.py ausführen.{NC}")
        return None
    return GenomeDB(DB_PATH)


def _age_days(iso: str | None) -> float:
    if not iso:
        return 9999.0
    try:
        ts = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return 9999.0


def _regime_list(genome: dict) -> list:
    try:
        return json.loads(genome.get('active_regimes', '[]'))
    except (json.JSONDecodeError, TypeError):
        return []


def _winrate(g: dict) -> float:
    return g['wins'] / max(g['total_occurrences'], 1)


def _regime_wr(g: dict, regime: str) -> tuple[float, int]:
    """(winrate, occ) für ein bestimmtes Regime."""
    occ  = g.get(f'occ_{regime.lower()}', 0)
    wins = g.get(f'wins_{regime.lower()}', 0)
    if occ == 0:
        return 0.0, 0
    return wins / occ, occ


# ─────────────────────────────────────────────────────────────────────────────
# Modus 1: Genome Übersicht
# ─────────────────────────────────────────────────────────────────────────────

def mode_overview(db: GenomeDB):
    summary = db.get_db_summary()

    print(f"\n{SEP}")
    print(f"  {Y}GENOME LIBRARY — ÜBERSICHT{NC}")
    print(SEP)
    print(f"  Gesamt-Genome:    {summary['total_genomes']}")
    print(f"  Aktive Genome:    {G}{summary['active_genomes']}{NC}")
    print(f"  Märkte in DB:     {', '.join(summary['markets']) or '—'}")

    # Per-Markt Zusammenfassung
    all_genomes = db.get_all_genomes()
    by_pair: dict[tuple, list] = {}
    for g in all_genomes:
        key = (g['market'], g['timeframe'])
        by_pair.setdefault(key, []).append(g)

    if by_pair:
        print()
        print(f"  {'Markt':<22} {'TF':<5} {'Gesamt':>7} {'Aktiv':>6} {'Ø Score':>8} {'Ø WR':>7}")
        print(f"  {SEP2}")
        for (mkt, tf), gs in sorted(by_pair.items()):
            active = [g for g in gs if g['active']]
            avg_score = sum(g['score'] for g in active) / max(len(active), 1)
            avg_wr    = sum(_winrate(g) for g in gs) / max(len(gs), 1)
            print(
                f"  {mkt:<22} {tf:<5} {len(gs):>7} "
                f"{G}{len(active):>6}{NC} {avg_score:>8.3f} {avg_wr:>6.1%}"
            )

    # Top 10 global
    top = summary['top_patterns']
    if top:
        print()
        print(f"  {Y}Top 10 Patterns (global, aktiv){NC}")
        print(f"  {SEP2}")
        for i, p in enumerate(top, 1):
            regimes = _regime_list(p)
            wr = p.get('winrate', 0)
            print(
                f"  {i:2}. [{p['direction']:<5}] Score {G}{p['score']:.3f}{NC} | "
                f"WR {wr:.1%} | Avg Move {p.get('avg_move_pct', 0):.2f}% | "
                f"n={p['total_occurrences']}"
            )
            print(
                f"      {DIM}{p['sequence']}{NC}  "
                f"Regime: {', '.join(regimes) or '—'} | {p['market']} {p['timeframe']}"
            )

    # Backtest-Ergebnisse (falls vorhanden)
    result_files = glob.glob(os.path.join(RESULTS_DIR, 'backtest_*.json'))
    if result_files:
        print()
        print(f"  {Y}Backtest-Ergebnisse{NC}")
        print(f"  {SEP2}")
        print(f"  {'Markt':<22} {'TF':<5} {'Trades':>7} {'WR':>7} {'PnL%':>8} {'PF':>6} {'MaxDD':>7}")
        summaries = []
        for path in sorted(result_files):
            try:
                with open(path) as f:
                    data = json.load(f)
                st = data.get('stats', {})
                summaries.append({
                    'market': data.get('market', '?'),
                    'tf': data.get('timeframe', '?'),
                    'trades': st.get('total_trades', 0),
                    'wr': st.get('win_rate', 0),
                    'pnl': st.get('total_pnl_pct', 0),
                    'pf': st.get('profit_factor', 0),
                    'dd': st.get('max_drawdown_pct', 0),
                })
            except Exception:
                pass
        for s in sorted(summaries, key=lambda x: x['pnl'], reverse=True):
            sign = '+' if s['pnl'] >= 0 else ''
            col  = G if s['pnl'] >= 0 else R
            print(
                f"  {s['market']:<22} {s['tf']:<5} {s['trades']:>7} "
                f"{s['wr']:>6.1%} {col}{sign}{s['pnl']:>7.1f}%{NC} "
                f"{s['pf']:>6.2f} {s['dd']:>6.1f}%"
            )

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Modus 2: Symbol-Detail
# ─────────────────────────────────────────────────────────────────────────────

def mode_symbol_detail(db: GenomeDB, symbol: str, timeframe: str, top_n: int = 20):
    all_g = db.get_all_genomes(symbol, timeframe)
    if not all_g:
        print(f"{R}Keine Genome für {symbol} ({timeframe}) gefunden.{NC}")
        return

    active = [g for g in all_g if g['active']]
    inactive = [g for g in all_g if not g['active']]

    print(f"\n{SEP}")
    print(f"  {Y}SYMBOL-DETAIL: {symbol} ({timeframe}){NC}")
    print(SEP)
    print(f"  Gesamt: {len(all_g)} | Aktiv: {G}{len(active)}{NC} | Inaktiv: {DIM}{len(inactive)}{NC}")

    if not active:
        print(f"\n  {R}Keine aktiven Genome. scan_and_learn.py ausführen.{NC}\n")
        print(f"{SEP}\n")
        return

    sorted_g = sorted(active, key=lambda x: x['score'], reverse=True)[:top_n]

    REGIMES = ['TREND', 'RANGE', 'NEUTRAL']

    print()
    print(f"  {'#':<3} {'Dir':<6} {'Score':>7} {'WR':>7} {'Avg%':>7} {'n':>5}  Regime (WR/occ)")
    print(f"  {SEP2}")

    for i, g in enumerate(sorted_g, 1):
        regimes = _regime_list(g)
        wr = _winrate(g)

        # Regime-Detail
        regime_parts = []
        for reg in REGIMES:
            rwr, rocc = _regime_wr(g, reg)
            if rocc > 0:
                active_marker = G + '●' + NC if reg in regimes else DIM + '○' + NC
                regime_parts.append(
                    f"{active_marker}{reg[:2]} {rwr:.0%}/{rocc}"
                )
        regime_str = '  '.join(regime_parts) if regime_parts else '—'

        dir_col = G if g['direction'] == 'LONG' else R
        print(
            f"  {i:<3} {dir_col}{g['direction']:<6}{NC} "
            f"{g['score']:>7.3f} {wr:>6.1%} "
            f"{g.get('avg_move_pct', 0):>6.2f}% {g['total_occurrences']:>5}  "
            f"{regime_str}"
        )
        print(f"      {DIM}{g['sequence']}{NC}  "
              f"last_seen: {g.get('last_seen', '—')[:10] if g.get('last_seen') else '—'}")

    # Richtungsverteilung
    longs  = sum(1 for g in active if g['direction'] == 'LONG')
    shorts = sum(1 for g in active if g['direction'] == 'SHORT')
    print()
    print(f"  Richtungsverteilung:  {G}LONG {longs}{NC}  /  {R}SHORT {shorts}{NC}")

    # Sequenzlängen
    by_len: dict[int, int] = {}
    for g in active:
        by_len[g['seq_length']] = by_len.get(g['seq_length'], 0) + 1
    len_str = '  '.join(f"{l}er: {c}" for l, c in sorted(by_len.items()))
    print(f"  Sequenzlängen:        {len_str}")
    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Modus 3: Regime-Analyse
# ─────────────────────────────────────────────────────────────────────────────

def mode_regime_analysis(db: GenomeDB):
    all_g = db.get_all_genomes()

    print(f"\n{SEP}")
    print(f"  {Y}REGIME-ANALYSE — Alle Märkte{NC}")
    print(SEP)

    if not all_g:
        print(f"  {R}Datenbank leer.{NC}\n")
        return

    REGIMES = ['TREND', 'RANGE', 'NEUTRAL']

    # Global über alle aktiven Genome
    active = [g for g in all_g if g['active']]
    print(f"  Aktive Genome gesamt: {len(active)}")
    print()

    # Per-Regime Aggregat
    print(f"  {Y}Global (alle Märkte){NC}")
    print(f"  {'Regime':<10} {'Aktive':>7} {'Ø WR':>7} {'Ø occ':>7}")
    print(f"  {SEP2}")
    for reg in REGIMES:
        reg_l = reg.lower()
        with_data = [g for g in active if g.get(f'occ_{reg_l}', 0) > 0]
        if not with_data:
            print(f"  {reg:<10} {'—':>7}")
            continue
        avg_wr  = sum(_regime_wr(g, reg)[0] for g in with_data) / len(with_data)
        avg_occ = sum(_regime_wr(g, reg)[1] for g in with_data) / len(with_data)
        n_active_in_regime = sum(
            1 for g in active if reg in _regime_list(g)
        )
        print(f"  {reg:<10} {n_active_in_regime:>7} {avg_wr:>6.1%} {avg_occ:>7.0f}")

    # Per-Markt/TF-Pair
    by_pair: dict[tuple, list] = {}
    for g in active:
        key = (g['market'], g['timeframe'])
        by_pair.setdefault(key, []).append(g)

    print()
    print(f"  {Y}Per Markt{NC}")
    header = f"  {'Markt':<22} {'TF':<5}"
    for reg in REGIMES:
        header += f"  {reg:<15}"
    print(header)
    print(f"  {SEP2}")

    for (mkt, tf), gs in sorted(by_pair.items()):
        row = f"  {mkt:<22} {tf:<5}"
        for reg in REGIMES:
            reg_l = reg.lower()
            with_data = [g for g in gs if g.get(f'occ_{reg_l}', 0) > 0]
            if not with_data:
                row += f"  {'—':<15}"
            else:
                avg_wr = sum(_regime_wr(g, reg)[0] for g in with_data) / len(with_data)
                n = sum(1 for g in gs if reg in _regime_list(g))
                row += f"  {G if n > 0 else DIM}{n} aktiv / {avg_wr:.0%}{NC:<15}"
        print(row)

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Modus 4: Decay-Status
# ─────────────────────────────────────────────────────────────────────────────

def mode_decay_status(db: GenomeDB):
    all_g = db.get_all_genomes()
    active = [g for g in all_g if g['active']]

    print(f"\n{SEP}")
    print(f"  {Y}DECAY-STATUS — Genome nach Alter (last_seen){NC}")
    print(SEP)

    if not active:
        print(f"  {R}Keine aktiven Genome.{NC}\n")
        return

    # Alter berechnen
    for g in active:
        g['_age_days'] = _age_days(g.get('last_seen'))

    sorted_g = sorted(active, key=lambda x: x['_age_days'], reverse=True)

    # Kategorien
    critical = [g for g in sorted_g if g['_age_days'] > 180]
    warning  = [g for g in sorted_g if 90 < g['_age_days'] <= 180]
    fresh    = [g for g in sorted_g if g['_age_days'] <= 90]

    def _age_str(days: float) -> str:
        if days > 9000:
            return 'nie gesehen'
        if days >= 365:
            return f"{days/365:.1f} Jahre"
        return f"{days:.0f} Tage"

    print(
        f"  {G}Frisch (≤90d):{NC}   {len(fresh):>4}  |  "
        f"  {Y}Warnung (90–180d):{NC} {len(warning):>4}  |  "
        f"  {R}Kritisch (>180d):{NC}  {len(critical):>4}"
    )

    for label, group, col in [
        ('KRITISCH', critical, R),
        ('WARNUNG',  warning,  Y),
        ('FRISCH',   fresh,    G),
    ]:
        if not group:
            continue
        print()
        print(f"  {col}── {label} ({len(group)} Genome) ──{NC}")
        print(f"  {'Markt':<22} {'TF':<5} {'Dir':<6} {'Alter':>12} {'Score':>7} {'Sequenz'}")
        print(f"  {SEP2}")
        for g in group[:25]:  # max 25 pro Kategorie
            print(
                f"  {g['market']:<22} {g['timeframe']:<5} "
                f"{g['direction']:<6} {_age_str(g['_age_days']):>12} "
                f"{g['score']:>7.3f}  {DIM}{g['sequence'][:50]}{NC}"
            )
        if len(group) > 25:
            print(f"  {DIM}  ... und {len(group) - 25} weitere{NC}")

    # Empfehlung
    if critical:
        print()
        print(f"  {Y}Empfehlung:{NC} {len(critical)} Genome wurden >180 Tage nicht mehr beobachtet.")
        print(f"  Ihr effektives Gewicht ist stark reduziert. scan_and_learn.py ausführen,")
        print(f"  um sie mit aktuellen Daten zu aktualisieren oder zu deaktivieren.")

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="dnabot Genome Analyse")
    # Modi aus show_results.sh:
    #   1 = Genome Bibliothek (Übersicht)
    #   2 = Regime-Analyse
    #   3 = Decay-Status (nicht im Menü genutzt, aber per CLI aufrufbar)
    #   4 = Interaktiver Chart
    # Direkt aufrufbar:
    #   --symbol + --timeframe → Symbol-Detail
    parser.add_argument('--mode', type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument('--symbol', type=str, default=None)
    parser.add_argument('--timeframe', type=str, default=None)
    parser.add_argument('--top', type=int, default=20, help="Max Patterns für Symbol-Detail")
    args = parser.parse_args()

    # Modus 4: Interaktiver Chart (braucht keine DB direkt)
    if args.mode == 4:
        import json
        from dnabot.analysis.interactive_chart import run_interactive_chart
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
            settings = json.load(f)
        secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
        secrets = {}
        if os.path.exists(secret_path):
            with open(secret_path) as f:
                secrets = json.load(f)
        run_interactive_chart(settings, secrets)
        return

    db = _load_db()
    if db is None:
        sys.exit(1)

    # Symbol-Detail: explizit via --symbol + --timeframe (nicht im Menü)
    if args.symbol and args.timeframe:
        mode_symbol_detail(db, args.symbol, args.timeframe, args.top)
    elif args.mode == 1:
        mode_overview(db)
    elif args.mode == 2:
        mode_regime_analysis(db)
    elif args.mode == 3:
        mode_decay_status(db)

    db.close()


if __name__ == '__main__':
    main()
