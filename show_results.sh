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
echo "  1) Einzel-Backtest               (jedes Pair wird simuliert)"
echo "  2) Genome Bibliothek             (Top-Patterns + Stats aus der DB)"
echo "  3) Automatische Portfolio-Opt.   (Bot wählt das beste Team)"
echo "  4) Interaktive Charts            (Candlestick + Entry/Exit-Marker)"
read -p "Auswahl (1-4) [Standard: 2]: " MODE

if [[ ! "$MODE" =~ ^[1-4]?$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Standard (2).${NC}"
    MODE=2
fi
MODE=${MODE:-2}

# ─────────────────────────────────────────
# Mode 1: Einzel-Backtest
# ─────────────────────────────────────────
if [ "$MODE" == "1" ]; then
    echo ""
    read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=alle aus DB]: " COINS_INPUT
    COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
    read -p "Timeframe(s) eingeben (z.B. 4h 6h 2h) [leer=alle aus DB]: " TF_INPUT
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

# ─────────────────────────────────────────
# Mode 3: Automatische Portfolio-Optimierung
# ─────────────────────────────────────────
elif [ "$MODE" == "3" ]; then
    echo ""
    read -p "Gewünschter maximaler Drawdown in % [Standard: 30]: " MAX_DD
    MAX_DD="${MAX_DD//[$'\r\n ']/}"
    if ! [[ "$MAX_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then MAX_DD=30; fi

    echo ""
    echo "--- Bitte Konfiguration festlegen ---"
    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"
    if ! [[ "$START_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then START_DATE="2023-01-01"; fi

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ] && [[ "$END_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    python3 run_portfolio_optimizer.py \
        --capital "$CAPITAL" \
        --risk "$RISK" \
        --max-dd "$MAX_DD" \
        $DATE_ARGS

# ─────────────────────────────────────────
# Mode 4: Interaktive Charts
# ─────────────────────────────────────────
elif [ "$MODE" == "4" ]; then
    echo ""
    python3 src/dnabot/analysis/show_results.py --mode 4

# ─────────────────────────────────────────
# Mode 2: Genome Bibliothek → --mode 1
# ─────────────────────────────────────────
else
    python3 src/dnabot/analysis/show_results.py --mode 1
fi

deactivate
