#!/bin/bash
# run_walkforward.sh — Walk-Forward Lookback Analyse
#
# Untersucht welcher Lookback-Zeitraum für den wöchentlichen Auto-Optimizer
# am besten Out-of-Sample performt. Sendet Ergebnis-Chart via Telegram.
#
# Voraussetzung: Backtest-Dateien in artifacts/results/ (Mode 1 in show_results.sh)
# Tipp: Für mehr Test-Wochen zuerst: ./show_results.sh → Mode 1 mit 2025-01-01

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi

source "$SCRIPT_DIR/.venv/bin/activate"

echo ""
echo "======================================================="
echo "  dnabot — Walk-Forward Lookback Analyse"
echo "======================================================="
echo ""
echo "  Benötigt: Backtest-Dateien in artifacts/results/"
echo "  Tipp:     Mehr Wochen = zuverlässigeres Ergebnis."
echo "            ./show_results.sh → Mode 1 mit 2025-01-01"
echo ""

read -p "Risiko pro Trade in % [Standard: aus settings.json]: " RISK_INPUT
RISK_INPUT="${RISK_INPUT//[$'\r\n ']/}"

read -p "Startkapital in USDT [Standard: 100]: " CAPITAL_INPUT
CAPITAL_INPUT="${CAPITAL_INPUT//[$'\r\n ']/}"
if ! [[ "$CAPITAL_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL_INPUT=100; fi

read -p "Min. Trades pro Pair im Lookback-Fenster [Standard: 10]: " MIN_TRADES_INPUT
MIN_TRADES_INPUT="${MIN_TRADES_INPUT//[$'\r\n ']/}"
if ! [[ "$MIN_TRADES_INPUT" =~ ^[0-9]+$ ]]; then MIN_TRADES_INPUT=10; fi

ARGS="--capital $CAPITAL_INPUT --min-trades $MIN_TRADES_INPUT"
if [[ "$RISK_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    ARGS="$ARGS --risk $RISK_INPUT"
fi

echo ""
$PYTHON "$SCRIPT_DIR/walk_forward_test.py" $ARGS

deactivate
