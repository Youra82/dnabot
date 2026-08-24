# src/dnabot/genome/risk_evolver.py
# Self-Learning Evolution fuer Risiko-/Exit-Gene (momentum_exit-Strategie).
#
# Spiegelt genome/evolver.py, aber das Selektionskriterium ist Calmar-Ratio
# (PnL / MaxDD) statt Winrate/Score -- siehe Fund AN/AQ in
# research_dnabot_direction_calibration.md: bei duennem, momentum-basiertem
# Einstieg entscheidet die Positionsgroesse/Drawdown-Kontrolle ueber
# Profitabilitaet, nicht die Trefferquote.
#
# Anders als beim Kerzen-Genome-System (viele gleichzeitig aktive Muster)
# waehlt der Risk-Evolver GENAU EIN aktives Gen pro (market, timeframe) --
# das ist die Konfiguration, mit der momentum_exit_logic.py live tatsaechlich
# handelt. Alle anderen Kandidaten bleiben inaktiv in der DB (Vergleichsbasis
# fuer den naechsten Evolver-Lauf).

import logging

from dnabot.genome.risk_genome_db import RiskGenomeDB

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_ACTIVATION = 50  # zu wenig Trades = Calmar statistisch unzuverlaessig


def evolve_risk_genes(db: RiskGenomeDB, market: str, timeframe: str) -> dict:
    """
    Bewertet alle Kandidaten-Risiko-Gene fuer (market, timeframe) und
    aktiviert das mit dem hoechsten Calmar (PnL/MaxDD), sofern es eine
    Mindest-Trade-Zahl erreicht UND positiv ist (Calmar > 0 -- sonst lieber
    gar kein Gen aktiv als ein negatives, siehe konservative Philosophie
    des Kerzen-Genome-Systems: nur handeln wenn etwas eine Schwelle nimmt).
    """
    candidates = db.get_candidates(market, timeframe)
    eligible = [c for c in candidates if c['total_trades'] >= MIN_TRADES_FOR_ACTIVATION]

    # Alle erst deaktivieren, dann genau einen Gewinner (falls vorhanden) aktivieren --
    # verhindert dass ein alter Gewinner aktiv bleibt, wenn der neue Lauf keinen findet.
    for c in candidates:
        if c['active']:
            db.set_active(c['risk_gene_id'], False)

    if not eligible:
        logger.info(f"[RiskEvolver] {market} ({timeframe}): keine Kandidaten mit "
                     f">= {MIN_TRADES_FOR_ACTIVATION} Trades -- kein Gen aktiv.")
        return {"market": market, "timeframe": timeframe, "activated": None, "candidates": len(candidates)}

    best = max(eligible, key=lambda c: c['calmar'])
    if best['calmar'] <= 0:
        logger.info(f"[RiskEvolver] {market} ({timeframe}): bestes Calmar "
                     f"({best['calmar']:.2f}) nicht positiv -- kein Gen aktiv.")
        return {"market": market, "timeframe": timeframe, "activated": None, "candidates": len(candidates)}

    db.set_active(best['risk_gene_id'], True)
    logger.info(
        f"[RiskEvolver] {market} ({timeframe}): Gen aktiviert "
        f"seq_len={best['seq_len']} rr={best['rr_ratio']} trail={best['trailing_pct']}% "
        f"risk={best['risk_pct']}% | Calmar={best['calmar']:.2f} "
        f"PnL={best['total_pnl_pct']:+.1f}% MaxDD={best['max_dd_pct']:.1f}% n={best['total_trades']}"
    )
    return {
        "market": market, "timeframe": timeframe, "activated": best['risk_gene_id'],
        "calmar": best['calmar'], "candidates": len(candidates),
    }
