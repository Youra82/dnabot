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
    read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=aus active_strategies]: " COINS_INPUT
    COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
    read -p "Timeframe(s) eingeben (z.B. 4h 6h 2h) [leer=aus active_strategies]: " TF_INPUT
    TF_INPUT="${TF_INPUT//[$'\r\n']/}"

    [ -n "$COINS_INPUT" ] && export DNABOT_OVERRIDE_COINS="$COINS_INPUT"
    [ -n "$TF_INPUT" ]    && export DNABOT_OVERRIDE_TFS="$TF_INPUT"

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"
    if ! [[ "$START_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then START_DATE="2023-01-01"; fi

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"

    DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ] && [[ "$END_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    if [ -z "$COINS_INPUT" ] && [ -z "$TF_INPUT" ]; then
        python3 run_backtest.py --capital "$CAPITAL" --risk "$RISK" --all-from-db $DATE_ARGS
    else
        python3 run_backtest.py --capital "$CAPITAL" --risk "$RISK" $DATE_ARGS
    fi

    unset DNABOT_OVERRIDE_COINS DNABOT_OVERRIDE_TFS

elif [ "$MODE" == "4" ]; then
    echo ""
    python3 src/dnabot/analysis/show_results.py --mode 4

else
    # Modus 2 → --mode 1 (Genome Bibliothek)
    # Modus 3 → --mode 2 (Regime-Analyse)
    python3 src/dnabot/analysis/show_results.py --mode "$((MODE - 1))"
fi

deactivate
