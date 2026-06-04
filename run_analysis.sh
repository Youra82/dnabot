#!/bin/bash
# run_analysis.sh — dnabot Wissenschaftliche Analysen
#
# Alle Analysen unter einem Befehl. Interaktive Auswahl.
# Ergebnisse werden via Telegram gesendet.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'
BOLD='\033[1m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

echo ""
echo "======================================================="
echo -e "  ${BOLD}dnabot — Wissenschaftliche Analysen${NC}"
echo "======================================================="
echo ""
echo -e "  ${CYAN}── Priorität 1: Fundament ─────────────────────────${NC}"
echo "   1) Walk-Forward Lookback-Analyse"
echo "   2) Slippage & Fee Impact"
echo "   3) Monte Carlo Simulation"
echo -e "   4) Bootstrap Signifikanztest          ${DIM}(in Entwicklung)${NC}"
echo ""
echo -e "  ${CYAN}── Priorität 2: Gewinnoptimierung ──────────────────${NC}"
echo "   5) RR-Ratio Optimierung                (Walk-Forward)"
echo "   6) Score Threshold Sweep               (Walk-Forward)"
echo "   7) Trailing Callback Optimierung       (Walk-Forward)"
echo -e "   8) Parameter Sensitivity Analysis     ${DIM}(in Entwicklung)${NC}"
echo ""
echo -e "  ${CYAN}── Priorität 3: Systemverbesserung ─────────────────${NC}"
echo -e "   9) Multi-TF Confirmation              ${DIM}(in Entwicklung)${NC}"
echo -e "  10) Genome Decay Analysis              ${DIM}(in Entwicklung)${NC}"
echo -e "  11) Anti-Korrelations-Portfolio        ${DIM}(in Entwicklung)${NC}"
echo -e "  12) Kelly Position Sizing              ${DIM}(in Entwicklung)${NC}"
echo ""
echo -e "  ${CYAN}── Priorität 4-6: Weitere ──────────────────────────${NC}"
echo -e "  13) Regime Performance Analysis        ${DIM}(in Entwicklung)${NC}"
echo -e "  14) Sequenzlängen-Analyse              ${DIM}(in Entwicklung)${NC}"
echo -e "  15) Confluence Score                   ${DIM}(in Entwicklung)${NC}"
echo -e "  16) Volatilitäts-Filter Optimierung    ${DIM}(in Entwicklung)${NC}"
echo -e "  17) Tageszeit-Analyse                  ${DIM}(in Entwicklung)${NC}"
echo -e "  18) Regime-adaptive Parameter          ${DIM}(in Entwicklung)${NC}"
echo -e "  19) Drawdown Duration Analysis         ${DIM}(in Entwicklung)${NC}"
echo ""
read -p "Auswahl (1-19): " MODE
MODE="${MODE//[$'\r\n ']/}"
echo ""

case "$MODE" in
    1)
        echo -e "${GREEN}▶ Walk-Forward Lookback-Analyse${NC}"
        read -p "Risiko pro Trade in % [Standard: aus settings.json]: " RISK
        RISK="${RISK//[$'\r\n ']/}"
        read -p "Startkapital in USDT [Standard: 100]: " CAP
        CAP="${CAP//[$'\r\n ']/}"
        if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
        ARGS="--capital $CAP"
        [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]] && ARGS="$ARGS --risk $RISK"
        $PYTHON "$SCRIPT_DIR/walk_forward_test.py" $ARGS
        ;;
    2)
        echo -e "${GREEN}▶ Slippage & Fee Impact${NC}"
        read -p "Startkapital in USDT [Standard: 100]: " CAP
        CAP="${CAP//[$'\r\n ']/}"
        if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
        read -p "Risiko pro Trade in % [Standard: 2.5]: " RISK
        RISK="${RISK//[$'\r\n ']/}"
        if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=2.5; fi
        $PYTHON "$SCRIPT_DIR/analysis/fee_impact.py" --capital "$CAP" --risk "$RISK"
        ;;
    3)
        echo -e "${GREEN}▶ Monte Carlo Simulation${NC}"
        read -p "Anzahl Simulationen [Standard: 10000]: " SIMS
        SIMS="${SIMS//[$'\r\n ']/}"
        if ! [[ "$SIMS" =~ ^[0-9]+$ ]]; then SIMS=10000; fi
        read -p "Startkapital in USDT [Standard: 100]: " CAP
        CAP="${CAP//[$'\r\n ']/}"
        if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
        read -p "Risiko pro Trade in % [Standard: 2.5]: " RISK
        RISK="${RISK//[$'\r\n ']/}"
        if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=2.5; fi
        $PYTHON "$SCRIPT_DIR/analysis/monte_carlo.py" \
            --simulations "$SIMS" --capital "$CAP" --risk "$RISK"
        ;;
    5|6|7)
        echo -e "${GREEN}▶ Parameter Walk-Forward Optimierung${NC}"
        if [ "$MODE" == "5" ]; then PARAM="rr";       LABEL="RR-Ratio"; fi
        if [ "$MODE" == "6" ]; then PARAM="score";    LABEL="Score Threshold"; fi
        if [ "$MODE" == "7" ]; then PARAM="callback"; LABEL="Trailing Callback %"; fi
        echo -e "  Parameter: ${CYAN}$LABEL${NC}"
        read -p "Startkapital in USDT [Standard: 100]: " CAP
        CAP="${CAP//[$'\r\n ']/}"
        if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
        read -p "Risiko pro Trade in % [Standard: 2.5]: " RISK
        RISK="${RISK//[$'\r\n ']/}"
        if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=2.5; fi
        $PYTHON "$SCRIPT_DIR/analysis/param_optimizer.py" \
            --param "$PARAM" --capital "$CAP" --risk "$RISK"
        ;;
    4|8|9|10|11|12|13|14|15|16|17|18|19)
        echo -e "${YELLOW}⏳ Diese Analyse ist noch in Entwicklung.${NC}"
        echo "   Bald verfügbar — stay tuned!"
        ;;
    *)
        echo -e "${RED}Ungültige Auswahl.${NC}"
        ;;
esac

deactivate
