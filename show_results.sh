#!/bin/bash
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

VENV_PATH=".venv/bin/activate"

if [ ! -f "$VENV_PATH" ]; then
    echo -e "${RED}Fehler: .venv nicht gefunden. Erst install.sh ausführen.${NC}"
    exit 1
fi

source "$VENV_PATH"

echo ""
echo -e "${YELLOW}Wähle einen Analyse-Modus:${NC}"
echo "  1) Einzel-Backtest       (jedes aktive Pair wird simuliert)"
echo "  2) Genome Bibliothek     (Top-Patterns + Stats aus der DB)"
echo "  3) Regime-Analyse        (Welches Regime funktioniert wo)"
echo "  4) Interaktive Charts    (Candlestick + Entry/Exit-Marker + Equity)"
read -p "Auswahl (1-4) [Standard: 2]: " MODE

if [[ ! "$MODE" =~ ^[1-4]?$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Standard (2).${NC}"
    MODE=2
fi
MODE=${MODE:-2}

if [ "$MODE" == "1" ]; then
    echo ""
    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    echo ""
    python3 run_backtest.py --capital "$CAPITAL" --risk "$RISK"

elif [ "$MODE" == "4" ]; then
    echo ""
    python3 src/dnabot/analysis/show_results.py --mode 4

else
    # Modus 2 → --mode 1 (Genome Bibliothek)
    # Modus 3 → --mode 2 (Regime-Analyse)
    python3 src/dnabot/analysis/show_results.py --mode "$((MODE - 1))"
fi

deactivate
