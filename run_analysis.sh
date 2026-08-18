#!/bin/bash
# run_analysis.sh — dnabot Wissenschaftliche Analysen
#
# Alle 20 Analysen unter einem Befehl. Interaktive Auswahl.
# Ergebnisse werden als Chart via Telegram gesendet.
#
# Ausführung:
#   ./run_analysis.sh
#   ./run_analysis.sh --no-telegram    (kein Telegram, nur lokale Charts)

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
NO_TELEGRAM=""

# --no-telegram Flag auswerten
for arg in "$@"; do
    [[ "$arg" == "--no-telegram" ]] && NO_TELEGRAM="--no-telegram"
done

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

# analysis/ im PYTHONPATH damit 'from analysis.utils import *' funktioniert
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH}"

# ─── Menü ─────────────────────────────────────────────────────────────────────

echo ""
echo "======================================================="
echo -e "  ${BOLD}dnabot — Wissenschaftliche Analysen${NC}"
echo "======================================================="
echo ""
echo -e "  ${CYAN}── Priorität 1: Fundament ─────────────────────────${NC}"
echo "   1) Walk-Forward Lookback-Analyse"
echo "   2) Slippage & Fee Impact"
echo "   3) Monte Carlo Simulation"
echo "   4) Bootstrap Signifikanztest"
echo ""
echo -e "  ${CYAN}── Priorität 2: Direkte Gewinnoptimierung ──────────${NC}"
echo "   5) RR-Ratio Optimierung          (Walk-Forward)"
echo "   6) Score Threshold Sweep         (Walk-Forward)"
echo "   7) Trailing Callback Optimierung (Walk-Forward)"
echo "   8) Parameter Sensitivity         (Tornado-Diagramm)"
echo ""
echo -e "  ${CYAN}── Priorität 3: Systemverbesserung ─────────────────${NC}"
echo "   9) Multi-Timeframe Confirmation"
echo "  10) Genome Decay Analysis"
echo "  11) Anti-Korrelations-Portfolio"
echo "  12) Kelly Position Sizing"
echo ""
echo -e "  ${CYAN}── Priorität 4–6: Feintuning & Portfolio ───────────${NC}"
echo "  13) Regime Performance Analysis"
echo "  14) Sequenzlängen-Analyse"
echo "  15) Confluence Score"
echo "  16) Volatilitäts-Filter Optimierung"
echo "  17) Tageszeit-Analyse"
echo "  18) Regime-adaptive Parameter"
echo "  19) Drawdown Duration Analysis"
echo ""
echo -e "  ${CYAN}── Strategie-Vergleich ─────────────────────────────${NC}"
echo "  20) WF Re-Opt vs. Alle Configs       (Langzeit-Vergleich)"
echo ""
echo "   0) Alle Analysen nacheinander ausführen"
echo ""
read -p "Auswahl (0-20): " MODE
MODE="${MODE//[$'\r\n ']/}"
echo ""

# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

ask_capital() {
    read -p "Startkapital in USDT [Standard: 100]: " CAP
    CAP="${CAP//[$'\r\n ']/}"
    if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
    echo "$CAP"
}

ask_risk() {
    read -p "Risiko pro Trade in % [Standard: 2.5]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=2.5; fi
    echo "$RISK"
}

run_mode() {
    local m="$1"
    case "$m" in

    # ── 1: Walk-Forward ───────────────────────────────────────────────────────
    1)
        echo -e "${GREEN}▶ Walk-Forward Lookback-Analyse${NC}"
        echo "  Ermittelt den optimalen Lookback-Zeitraum für den Auto-Optimizer."
        echo "  Tipp: Mehr Backtest-Wochen = zuverlässigeres Ergebnis."
        echo "        ./show_results.sh → Mode 1 mit Startdatum 2025-01-01"
        echo ""
        read -p "Risiko pro Trade in % [Standard: aus settings.json]: " RISK
        RISK="${RISK//[$'\r\n ']/}"
        read -p "Startkapital in USDT [Standard: 100]: " CAP
        CAP="${CAP//[$'\r\n ']/}"
        if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
        # Mindest-Trade-Zahl wird jetzt automatisch pro Lookback skaliert
        # (scaled_min_trades() in run_portfolio_optimizer.py) statt fix
        # abgefragt -- ein fester Wert schloss kurze Lookback-Fenster
        # strukturell von niedrigfrequenten Pairs aus.
        read -p "OOS-Testzeitraum auf die letzten N Wochen eingrenzen [leer=voller Zeitraum]: " OOS_W
        OOS_W="${OOS_W//[$'\r\n ']/}"
        ARGS="--capital $CAP $NO_TELEGRAM"
        [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]] && ARGS="$ARGS --risk $RISK"
        [[ "$OOS_W" =~ ^[0-9]+$ ]] && ARGS="$ARGS --oos-weeks $OOS_W"
        $PYTHON "$SCRIPT_DIR/walk_forward_test.py" $ARGS
        ;;

    # ── 2: Fee Impact ─────────────────────────────────────────────────────────
    2)
        echo -e "${GREEN}▶ Slippage & Fee Impact${NC}"
        echo "  Zeigt ab welcher Gebühr der Bot unrentabel wird (Break-Even)."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/fee_impact.py" \
            --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 3: Monte Carlo ────────────────────────────────────────────────────────
    3)
        echo -e "${GREEN}▶ Monte Carlo Simulation${NC}"
        echo "  10.000 zufällige Trade-Reihenfolgen → Konfidenzintervall & Ruin-Risiko."
        echo ""
        read -p "Anzahl Simulationen [Standard: 10000]: " SIMS
        SIMS="${SIMS//[$'\r\n ']/}"
        if ! [[ "$SIMS" =~ ^[0-9]+$ ]]; then SIMS=10000; fi
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/monte_carlo.py" \
            --simulations "$SIMS" --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 4: Bootstrap Signifikanztest ──────────────────────────────────────────
    4)
        echo -e "${GREEN}▶ Bootstrap Signifikanztest${NC}"
        echo "  Prüft ob Genome-Win-Raten statistisch signifikant über Zufall (50%) liegen."
        echo ""
        read -p "Min. Samples pro Genome [Standard: 10]: " MIN_S
        MIN_S="${MIN_S//[$'\r\n ']/}"
        if ! [[ "$MIN_S" =~ ^[0-9]+$ ]]; then MIN_S=10; fi
        read -p "Signifikanzniveau Alpha [Standard: 0.05]: " ALPHA
        ALPHA="${ALPHA//[$'\r\n ']/}"
        if ! [[ "$ALPHA" =~ ^0\.[0-9]+$ ]]; then ALPHA=0.05; fi
        $PYTHON "$SCRIPT_DIR/analysis/bootstrap_test.py" \
            --min-samples "$MIN_S" --alpha "$ALPHA" $NO_TELEGRAM
        ;;

    # ── 5: RR-Ratio Optimierung ───────────────────────────────────────────────
    5)
        echo -e "${GREEN}▶ RR-Ratio Optimierung (Walk-Forward)${NC}"
        echo "  Findet out-of-sample den optimalen RR (1.0 / 1.5 / 2.0 / 2.5 / 3.0)."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
            --param rr --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 6: Score Threshold Sweep ──────────────────────────────────────────────
    6)
        echo -e "${GREEN}▶ Score Threshold Sweep (Walk-Forward)${NC}"
        echo "  Findet out-of-sample den optimalen min_score (0.01 bis 0.30)."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
            --param score --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 7: Trailing Callback Optimierung ──────────────────────────────────────
    7)
        echo -e "${GREEN}▶ Trailing Callback Optimierung (Walk-Forward)${NC}"
        echo "  Findet out-of-sample den optimalen Callback % (0.3 bis 3.0)."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
            --param callback --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 8: Parameter Sensitivity ──────────────────────────────────────────────
    8)
        echo -e "${GREEN}▶ Parameter Sensitivity Analysis${NC}"
        echo "  Tornado-Diagramm: Welche Parameter machen das System fragil?"
        echo "  Breiter Balken = sensitiv = Overfitting-Risiko."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/sensitivity.py" \
            --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 9: Multi-TF Confirmation ──────────────────────────────────────────────
    9)
        echo -e "${GREEN}▶ Multi-Timeframe Confirmation${NC}"
        echo "  Trades die gleichzeitig auf mehreren Pairs signalisieren → besser?"
        echo ""
        read -p "Gleichzeitigkeit-Fenster in Stunden [Standard: 2]: " WH
        WH="${WH//[$'\r\n ']/}"
        if ! [[ "$WH" =~ ^[0-9]+$ ]]; then WH=2; fi
        $PYTHON "$SCRIPT_DIR/analysis/multitf_analysis.py" \
            --window-hours "$WH" $NO_TELEGRAM
        ;;

    # ── 10: Genome Decay ──────────────────────────────────────────────────────
    10)
        echo -e "${GREEN}▶ Genome Decay Analysis${NC}"
        echo "  Wie schnell verlieren Genome ihre Vorhersagekraft?"
        echo "  Validiert den half_life_days Parameter."
        echo ""
        $PYTHON "$SCRIPT_DIR/analysis/genome_decay.py" $NO_TELEGRAM
        ;;

    # ── 11: Anti-Korrelation ──────────────────────────────────────────────────
    11)
        echo -e "${GREEN}▶ Anti-Korrelations-Portfolio${NC}"
        echo "  Welche Pairs verlieren/gewinnen selten gleichzeitig?"
        echo "  Korrelationsmatrix → Portfolio mit minimalem Drawdown."
        echo ""
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/correlation.py" \
            --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 12: Kelly Position Sizing ─────────────────────────────────────────────
    12)
        echo -e "${GREEN}▶ Kelly Position Sizing${NC}"
        echo "  Mathematisch optimaler Einsatz pro Genome (Half-Kelly)."
        echo "  Starke Genome mehr riskieren, schwache weniger."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/kelly_sizing.py" \
            --capital "$CAP" --risk "$RISK" --half-kelly $NO_TELEGRAM
        ;;

    # ── 13: Regime Performance ────────────────────────────────────────────────
    13)
        echo -e "${GREEN}▶ Regime Performance Analysis${NC}"
        echo "  Win-Rate pro Regime (TREND / RANGE / NEUTRAL) aus der Genome-DB."
        echo "  Sollte man bestimmte Regime ausschalten?"
        echo ""
        read -p "Min. Samples pro Regime [Standard: 10]: " MIN_S
        MIN_S="${MIN_S//[$'\r\n ']/}"
        if ! [[ "$MIN_S" =~ ^[0-9]+$ ]]; then MIN_S=10; fi
        $PYTHON "$SCRIPT_DIR/analysis/regime_analysis.py" \
            --min-samples "$MIN_S" $NO_TELEGRAM
        ;;

    # ── 14: Sequenzlängen-Analyse ─────────────────────────────────────────────
    14)
        echo -e "${GREEN}▶ Sequenzlängen-Analyse${NC}"
        echo "  Sind 4er, 5er oder 6er Sequenzen profitabler?"
        echo "  Walk-Forward getrennt pro Sequenzlänge."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        read -p "Lookback Wochen [Standard: 2]: " LB
        LB="${LB//[$'\r\n ']/}"
        if ! [[ "$LB" =~ ^[0-9]+$ ]]; then LB=2; fi
        $PYTHON "$SCRIPT_DIR/analysis/seq_length_analysis.py" \
            --capital "$CAP" --risk "$RISK" --lookback "$LB" $NO_TELEGRAM
        ;;

    # ── 15: Confluence Score ──────────────────────────────────────────────────
    15)
        echo -e "${GREEN}▶ Confluence Score${NC}"
        echo "  Nur traden wenn mehrere Genome gleichzeitig dasselbe signalisieren."
        echo "  Weniger Trades, höhere Qualität?"
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        read -p "Gleichzeitigkeit-Fenster in Stunden [Standard: 4]: " WH
        WH="${WH//[$'\r\n ']/}"
        if ! [[ "$WH" =~ ^[0-9]+$ ]]; then WH=4; fi
        $PYTHON "$SCRIPT_DIR/analysis/confluence.py" \
            --capital "$CAP" --risk "$RISK" --window-hours "$WH" $NO_TELEGRAM
        ;;

    # ── 16: Volatilitäts-Filter ───────────────────────────────────────────────
    16)
        echo -e "${GREEN}▶ Volatilitäts-Filter Optimierung${NC}"
        echo "  Optimiert den ATR-Schwellwert für den HIGH_VOL-Filter."
        echo "  1.5×, 2.0×, 2.5×, 3.0× ATR-MA verglichen."
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/volatility_filter.py" \
            --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 17: Tageszeit-Analyse ─────────────────────────────────────────────────
    17)
        echo -e "${GREEN}▶ Tageszeit-Analyse${NC}"
        echo "  Performen Signale zu bestimmten Uhrzeiten besser?"
        echo "  Asiatische / Europäische / US-Session verglichen."
        echo ""
        $PYTHON "$SCRIPT_DIR/analysis/time_analysis.py" $NO_TELEGRAM
        ;;

    # ── 18: Regime-adaptive Parameter ────────────────────────────────────────
    18)
        echo -e "${GREEN}▶ Regime-adaptive Parameter${NC}"
        echo "  Verschiedene RR-Ratios pro Timeframe-Gruppe: besser oder nicht?"
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/regime_adaptive.py" \
            --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 19: Drawdown Duration ─────────────────────────────────────────────────
    19)
        echo -e "${GREEN}▶ Drawdown Duration Analysis${NC}"
        echo "  Wie lange dauern Drawdown-Phasen? Wie lange muss man aussitzen?"
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        $PYTHON "$SCRIPT_DIR/analysis/drawdown_duration.py" \
            --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
        ;;

    # ── 20: Strategie-Vergleich ───────────────────────────────────────────────
    20)
        echo -e "${GREEN}▶ WF Re-Opt vs. Alle Configs — Langzeit-Vergleich${NC}"
        echo "  Vergleicht wöchentliche Re-Optimierung gegen alle Configs dauerhaft."
        echo "  Beantwortet: Hilft die wöchentliche Optimierung langfristig oder nicht?"
        echo ""
        CAP=$(ask_capital)
        RISK=$(ask_risk)
        read -p "Max. Drawdown-Limit für Portfolio-Auswahl in % [Standard: 30]: " MAX_DD
        MAX_DD="${MAX_DD//[$'\r\n ']/}"
        if ! [[ "$MAX_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then MAX_DD=30; fi
        read -p "Fenster-Größe in Wochen [Standard: aus settings.json]: " LB
        LB="${LB//[$'\r\n ']/}"
        ARGS="--capital $CAP --risk $RISK --max-dd $MAX_DD $NO_TELEGRAM"
        [[ "$LB" =~ ^[0-9]+$ ]] && ARGS="$ARGS --lookback $LB"
        $PYTHON "$SCRIPT_DIR/analysis/strategy_comparison.py" $ARGS
        ;;

    *)
        echo -e "${RED}Ungültige Auswahl: $m${NC}"
        ;;
    esac
}

# ─── Auswahl ausführen ────────────────────────────────────────────────────────

if [ "$MODE" == "0" ]; then
    echo -e "${YELLOW}▶ Alle 20 Analysen werden nacheinander ausgeführt.${NC}"
    echo "  Hinweis: Analysen die Backtest-Daten benötigen (2-8, 11-12, 14-20)"
    echo "  werden übersprungen falls artifacts/results/ leer ist."
    echo ""
    # Für alle Modes Standard-Werte verwenden
    export DNABOT_BATCH=1
    for i in $(seq 1 20); do
        echo ""
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}  Analyse $i / 20${NC}"
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        # Batch-Modus: Standard-Kapital/Risk ohne Abfrage
        case "$i" in
            1)  $PYTHON "$SCRIPT_DIR/walk_forward_test.py" \
                    --capital 100 $NO_TELEGRAM 2>/dev/null || true ;;
            2)  $PYTHON "$SCRIPT_DIR/analysis/fee_impact.py" \
                    --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            3)  $PYTHON "$SCRIPT_DIR/analysis/monte_carlo.py" \
                    --simulations 10000 --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            4)  $PYTHON "$SCRIPT_DIR/analysis/bootstrap_test.py" \
                    --min-samples 10 --alpha 0.05 $NO_TELEGRAM 2>/dev/null || true ;;
            5)  $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
                    --param rr --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            6)  $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
                    --param score --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            7)  $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
                    --param callback --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            8)  $PYTHON "$SCRIPT_DIR/analysis/sensitivity.py" \
                    --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            9)  $PYTHON "$SCRIPT_DIR/analysis/multitf_analysis.py" \
                    --window-hours 2 $NO_TELEGRAM 2>/dev/null || true ;;
            10) $PYTHON "$SCRIPT_DIR/analysis/genome_decay.py" \
                    $NO_TELEGRAM 2>/dev/null || true ;;
            11) $PYTHON "$SCRIPT_DIR/analysis/correlation.py" \
                    --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            12) $PYTHON "$SCRIPT_DIR/analysis/kelly_sizing.py" \
                    --capital 100 --risk 2.5 --half-kelly $NO_TELEGRAM 2>/dev/null || true ;;
            13) $PYTHON "$SCRIPT_DIR/analysis/regime_analysis.py" \
                    --min-samples 10 $NO_TELEGRAM 2>/dev/null || true ;;
            14) $PYTHON "$SCRIPT_DIR/analysis/seq_length_analysis.py" \
                    --capital 100 --risk 2.5 --lookback 2 $NO_TELEGRAM 2>/dev/null || true ;;
            15) $PYTHON "$SCRIPT_DIR/analysis/confluence.py" \
                    --capital 100 --risk 2.5 --window-hours 4 $NO_TELEGRAM 2>/dev/null || true ;;
            16) $PYTHON "$SCRIPT_DIR/analysis/volatility_filter.py" \
                    --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            17) $PYTHON "$SCRIPT_DIR/analysis/time_analysis.py" \
                    $NO_TELEGRAM 2>/dev/null || true ;;
            18) $PYTHON "$SCRIPT_DIR/analysis/regime_adaptive.py" \
                    --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            19) $PYTHON "$SCRIPT_DIR/analysis/drawdown_duration.py" \
                    --capital 100 --risk 2.5 $NO_TELEGRAM 2>/dev/null || true ;;
            20) $PYTHON "$SCRIPT_DIR/analysis/strategy_comparison.py" \
                    --capital 100 --risk 2.5 --max-dd 30 $NO_TELEGRAM 2>/dev/null || true ;;
        esac
    done
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Alle Analysen abgeschlossen.${NC}"
    echo -e "${GREEN}  Charts gespeichert in: docs/                  ${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
else
    run_mode "$MODE"
fi

deactivate
