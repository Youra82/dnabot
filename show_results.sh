#!/bin/bash
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Plattformuebergreifend (Windows .venv/Scripts UND Unix .venv/bin) --
# gleiches Muster wie run_pipeline.sh, unveraendertes Verhalten auf Linux.
if [ -f ".venv/bin/activate" ]; then
    VENV_PATH=".venv/bin/activate"
elif [ -f ".venv/Scripts/activate" ]; then
    VENV_PATH=".venv/Scripts/activate"
else
    echo -e "${RED}Fehler: .venv nicht gefunden. Erst install.sh ausführen.${NC}"
    exit 1
fi
export PYTHONIOENCODING=utf-8

source "$VENV_PATH"

# Windows-venvs liefern kein 'python3'-Kommando (nur 'python') -- nach der
# Aktivierung pruefen, welches tatsaechlich verfuegbar ist, statt 'python3'
# hart zu verdrahten (unveraendertes Verhalten auf Linux, wo beides existiert).
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    PYTHON=python
fi

echo ""
echo -e "${YELLOW}Wähle einen Analyse-Modus:${NC}"
echo "  1) Einzel-Backtest               (jedes Pair wird simuliert, Genome-System)"
echo "  2) Manuelle Portfolio-Simulation (du wählst die Pairs, Genome-System)"
echo "  3) Automatische Portfolio-Opt.   (Bot wählt das beste Team, Genome-System)"
echo "  4) Genome Bibliothek             (Top-Patterns + Stats aus der DB)"
echo "  5) Interaktive Charts            (Candlestick + Entry/Exit-Marker)"
echo "  6) Risiko-Gene (momentum_exit)   (aktive + Kandidaten-Gene pro Pair)"
read -p "Auswahl (1-6) [Standard: 4]: " MODE

if [[ ! "$MODE" =~ ^[1-6]?$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Standard (4).${NC}"
    MODE=4
fi
MODE=${MODE:-4}

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
    START_DATE=$(date -d "${START_DATE:-2023-01-01}" +%Y-%m-%d 2>/dev/null || echo "2023-01-01")

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"
    [ -n "$END_DATE" ] && END_DATE=$(date -d "$END_DATE" +%Y-%m-%d 2>/dev/null || echo "")

    DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ] && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    if [ -z "$COINS_INPUT" ] && [ -z "$TF_INPUT" ]; then
        $PYTHON run_backtest.py --capital "$CAPITAL" --risk "$RISK" --all-from-db $DATE_ARGS
    else
        $PYTHON run_backtest.py --capital "$CAPITAL" --risk "$RISK" $DATE_ARGS
    fi

    unset DNABOT_OVERRIDE_COINS DNABOT_OVERRIDE_TFS

# ─────────────────────────────────────────
# Mode 2: Manuelle Portfolio-Simulation
# ─────────────────────────────────────────
elif [ "$MODE" == "2" ]; then
    echo ""
    echo "--- Bitte Konfiguration festlegen ---"
    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"
    START_DATE=$(date -d "${START_DATE:-2023-01-01}" +%Y-%m-%d 2>/dev/null || echo "2023-01-01")

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"
    [ -n "$END_DATE" ] && END_DATE=$(date -d "$END_DATE" +%Y-%m-%d 2>/dev/null || echo "")

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ] && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    $PYTHON run_manual_portfolio.py \
        --capital "$CAPITAL" \
        --risk "$RISK" \
        $DATE_ARGS

# ─────────────────────────────────────────
# Mode 3: Automatische Portfolio-Optimierung
# ─────────────────────────────────────────
elif [ "$MODE" == "3" ]; then
    echo ""
    read -p "Gewünschter maximaler Drawdown in % [Standard: 30]: " MAX_DD
    MAX_DD="${MAX_DD//[$'\r\n ']/}"
    if ! [[ "$MAX_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then MAX_DD=30; fi

    # Default-Startdatum berechnen: backtest_lookback_weeks (rollend) bevorzugt, sonst backtest_start_date
    DEFAULT_START=$($PYTHON -c "
import json, sys
from datetime import datetime, timedelta, timezone
try:
    s = json.load(open('settings.json'))
    opt = s.get('optimization_settings', {})
    weeks = opt.get('backtest_lookback_weeks')
    if weeks:
        print((datetime.now(timezone.utc) - timedelta(weeks=int(weeks))).strftime('%Y-%m-%d'))
    else:
        print(opt.get('backtest_start_date', '2023-01-01'))
except Exception:
    print('2023-01-01')
" 2>/dev/null || echo "2023-01-01")

    echo ""
    echo "--- Bitte Konfiguration festlegen ---"
    read -p "Startdatum (JJJJ-MM-TT) [Standard: $DEFAULT_START]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"
    START_DATE=$(date -d "${START_DATE:-$DEFAULT_START}" +%Y-%m-%d 2>/dev/null || echo "$DEFAULT_START")

    read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"
    [ -n "$END_DATE" ] && END_DATE=$(date -d "$END_DATE" +%Y-%m-%d 2>/dev/null || echo "")

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    read -p "Persistenz verlangen (2 aufeinanderfolgende gute Perioden statt nur 1)? (j/n) [Standard: aus settings.json]: " PERSIST_INPUT
    PERSIST_INPUT="${PERSIST_INPUT//[$'\r\n ']/}"

    DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ] && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"
    if [[ "$PERSIST_INPUT" == "j" || "$PERSIST_INPUT" == "J" || "$PERSIST_INPUT" == "y" || "$PERSIST_INPUT" == "Y" ]]; then
        DATE_ARGS="$DATE_ARGS --persistence"
    fi

    echo ""
    $PYTHON run_portfolio_optimizer.py \
        --capital "$CAPITAL" \
        --risk "$RISK" \
        --max-dd "$MAX_DD" \
        $DATE_ARGS

# ─────────────────────────────────────────
# Mode 5: Interaktive Charts
# ─────────────────────────────────────────
elif [ "$MODE" == "5" ]; then
    echo ""
    $PYTHON src/dnabot/analysis/show_results.py --mode 4

# ─────────────────────────────────────────
# Mode 6: Risiko-Gene (momentum_exit)
# ─────────────────────────────────────────
elif [ "$MODE" == "6" ]; then
    echo ""
    $PYTHON analysis/show_risk_genes.py

# ─────────────────────────────────────────
# Mode 4: Genome Bibliothek → --mode 1
# ─────────────────────────────────────────
else
    $PYTHON src/dnabot/analysis/show_results.py --mode 1
fi

deactivate
