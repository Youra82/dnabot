#!/bin/bash
# run_analysis.sh — dnabot Wissenschaftliche Analysen (momentum_exit)
#
# Ausführung:
#   ./run_analysis.sh
#   ./run_analysis.sh --no-telegram    (kein Telegram, nur lokale Charts)

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
    VENV_ACTIVATE="$SCRIPT_DIR/.venv/bin/activate"
elif [ -f "$SCRIPT_DIR/.venv/Scripts/python.exe" ]; then
    PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
    VENV_ACTIVATE="$SCRIPT_DIR/.venv/Scripts/activate"
else
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
export PYTHONIOENCODING=utf-8
NO_TELEGRAM=""
for arg in "$@"; do
    [[ "$arg" == "--no-telegram" ]] && NO_TELEGRAM="--no-telegram"
done

source "$VENV_ACTIVATE"
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH}"

echo ""
echo "======================================================="
echo -e "  ${BOLD}dnabot — Wissenschaftliche Analysen (momentum_exit)${NC}"
echo "======================================================="
echo ""
echo "   1) Walk-Forward Lookback-Analyse   (schreibt backtest_lookback_weeks nach settings.json)"
echo "   2) Monte Carlo Simulation          (Ruin-Risiko, Konfidenzintervall)"
echo "   3) Tageszeit-Analyse               (Win-Rate nach Uhrzeit/Wochentag)"
echo "   4) Regime Performance Analyse      (ADX/ATR-Regime vs. Trade-Ergebnis)"
echo ""
read -p "Auswahl (1-4): " MODE
MODE="${MODE//[$'\r\n ']/}"
echo ""

ask_capital() {
    read -p "Startkapital in USDT [Standard: 1000]: " CAP
    CAP="${CAP//[$'\r\n ']/}"
    if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=1000; fi
    echo "$CAP"
}

ask_risk() {
    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi
    echo "$RISK"
}

case "$MODE" in

# ── 1: Walk-Forward Lookback-Analyse ────────────────────────────────────────
1)
    echo -e "${GREEN}▶ Walk-Forward Lookback-Analyse${NC}"
    echo "  Ermittelt den optimalen Lookback-Zeitraum für den Auto-Optimizer und"
    echo "  trägt ihn automatisch in optimization_settings.backtest_lookback_weeks ein."
    echo ""
    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    CAP=$(ask_capital)
    read -p "OOS-Testzeitraum auf die letzten N Wochen eingrenzen [leer=voller Zeitraum]: " OOS_W
    OOS_W="${OOS_W//[$'\r\n ']/}"
    read -p "Persistenz verlangen (2 aufeinanderfolgende gute Perioden statt nur 1)? (j/n) [Standard: n]: " PERSIST
    PERSIST="${PERSIST//[$'\r\n ']/}"
    read -p "Automatisch in settings.json eintragen? (j/n) [Standard: j]: " DO_WRITE
    DO_WRITE="${DO_WRITE//[$'\r\n ']/}"
    ARGS="--capital $CAP $NO_TELEGRAM"
    [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]] && ARGS="$ARGS --risk $RISK"
    [[ "$OOS_W" =~ ^[0-9]+$ ]] && ARGS="$ARGS --oos-weeks $OOS_W"
    [[ "$PERSIST" == "j" || "$PERSIST" == "J" || "$PERSIST" == "y" || "$PERSIST" == "Y" ]] && ARGS="$ARGS --persistence"
    [[ "$DO_WRITE" == "n" || "$DO_WRITE" == "N" ]] && ARGS="$ARGS --no-write"
    "$PYTHON" "$SCRIPT_DIR/analysis/walkforward_momentum_exit.py" $ARGS
    ;;

# ── 2: Monte Carlo Simulation ───────────────────────────────────────────────
2)
    echo -e "${GREEN}▶ Monte Carlo Simulation${NC}"
    echo "  10.000 zufällige Trade-Reihenfolgen → Konfidenzintervall & Ruin-Risiko."
    echo ""
    read -p "Anzahl Simulationen [Standard: 10000]: " SIMS
    SIMS="${SIMS//[$'\r\n ']/}"
    if ! [[ "$SIMS" =~ ^[0-9]+$ ]]; then SIMS=10000; fi
    CAP=$(ask_capital)
    RISK=$(ask_risk)
    "$PYTHON" "$SCRIPT_DIR/analysis/monte_carlo_momentum_exit.py" \
        --simulations "$SIMS" --capital "$CAP" --risk "$RISK" $NO_TELEGRAM
    ;;

# ── 3: Tageszeit-Analyse ────────────────────────────────────────────────────
3)
    echo -e "${GREEN}▶ Tageszeit-Analyse${NC}"
    echo "  Win-Rate/Volumen nach Uhrzeit (UTC) und Wochentag."
    echo ""
    "$PYTHON" "$SCRIPT_DIR/analysis/time_analysis_momentum_exit.py" $NO_TELEGRAM
    ;;

# ── 4: Regime Performance Analyse ───────────────────────────────────────────
4)
    echo -e "${GREEN}▶ Regime Performance Analyse${NC}"
    echo "  Berechnet ADX/ATR-Marktregime (TREND/RANGE/HIGH_VOL/NEUTRAL) unabhängig"
    echo "  aus frischen OHLCV-Daten und matcht sie gegen die Trade-Entry-Zeiten."
    echo "  momentum_exit filtert NICHT danach -- rein deskriptiv."
    echo ""
    read -p "Mindest-Samples pro Regime/Coin [Standard: 10]: " MINS
    MINS="${MINS//[$'\r\n ']/}"
    if ! [[ "$MINS" =~ ^[0-9]+$ ]]; then MINS=10; fi
    "$PYTHON" "$SCRIPT_DIR/analysis/regime_analysis_momentum_exit.py" --min-samples "$MINS" $NO_TELEGRAM
    ;;

*)
    echo -e "${RED}Ungültige Auswahl.${NC}"
    ;;
esac

echo ""
deactivate 2>/dev/null
