#!/bin/bash
# run_pipeline.sh — Interaktive dnabot Pipeline
#
# Schritt 1: Optionen abfragen
# Schritt 2: scan_and_learn.py  → Genome-Discovery + Evolver
# Schritt 3: run_backtest.py    → Validierung der aktiven Genome
# Schritt 4: show_results.py    → Zusammenfassung

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
VENV_PATH="$SCRIPT_DIR/.venv/bin/activate"

# ── Venv prüfen ─────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$VENV_PATH"
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

# ── Header ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "       dnabot — Adaptive Market Genome System"
echo "======================================================="
echo ""

# ── 1. Alte DB löschen? ──────────────────────────────────────────────────────
DB_PATH="$SCRIPT_DIR/artifacts/db/genome.db"
if [ -f "$DB_PATH" ]; then
    read -p "Alte Genome-Datenbank vor dem Start löschen (Neustart)? (j/n) [Standard: n]: " RESET_DB
    RESET_DB="${RESET_DB//[$'\r\n ']/}"
    if [[ "$RESET_DB" == "j" || "$RESET_DB" == "J" || "$RESET_DB" == "y" || "$RESET_DB" == "Y" ]]; then
        rm -f "$DB_PATH"
        echo -e "${GREEN}✔ Alte Genome-DB gelöscht — Neustart.${NC}"
    else
        echo -e "${GREEN}✔ Bestehende Genome-DB wird beibehalten.${NC}"
    fi
else
    echo -e "${CYAN}ℹ  Keine bestehende Genome-DB gefunden — wird neu erstellt.${NC}"
fi

# ── 2. Coins / Timeframes ────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Coins und Timeframes:${NC}"
echo "  Leer lassen → automatisch aus active_strategies in settings.json übernehmen"
echo ""
read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=auto]: " COINS_INPUT
read -p "Timeframe(s) eingeben (z.B. 4h 1h) [leer=auto]: " TF_INPUT

COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
TF_INPUT="${TF_INPUT//[$'\r\n']/}"

# Coins und Timeframes in Symbol-Format umwandeln
SYMBOL_ARGS=""
TF_ARGS=""

if [ -n "$COINS_INPUT" ] && [ -n "$TF_INPUT" ]; then
    # Beide explizit gesetzt — wir übergeben via settings-override nicht möglich,
    # also setzen wir --symbol und --timeframe für einzelne Läufe (erster Coin + TF)
    # Bei mehreren: Pipeline-Skript baut Paarliste via Python
    echo -e "${CYAN}ℹ  Explizite Auswahl: Coins=$COINS_INPUT | Timeframes=$TF_INPUT${NC}"
    # Wir schreiben temporäre Overrides als Env-Variablen für scan_and_learn
    export DNABOT_OVERRIDE_COINS="$COINS_INPUT"
    export DNABOT_OVERRIDE_TFS="$TF_INPUT"
elif [ -n "$COINS_INPUT" ]; then
    export DNABOT_OVERRIDE_COINS="$COINS_INPUT"
    echo -e "${CYAN}ℹ  Coins: $COINS_INPUT | Timeframes: aus active_strategies${NC}"
elif [ -n "$TF_INPUT" ]; then
    export DNABOT_OVERRIDE_TFS="$TF_INPUT"
    echo -e "${CYAN}ℹ  Coins: aus active_strategies | Timeframes: $TF_INPUT${NC}"
else
    echo -e "${GREEN}✔ Coins und Timeframes werden aus active_strategies übernommen.${NC}"
fi

# ── 3. History-Tage ──────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}--- Empfehlung: Optimaler Rückblick-Zeitraum ---${NC}"
printf "  %-12s  %s\n" "Zeitfenster" "Empfohlener Rückblick (Tage)"
printf "  %-12s  %s\n" "──────────" "──────────────────────────"
printf "  %-12s  %s\n" "5m, 15m"    "60 - 180 Tage"
printf "  %-12s  %s\n" "30m, 1h"    "180 - 365 Tage"
printf "  %-12s  %s\n" "2h, 4h"     "365 - 730 Tage"
printf "  %-12s  %s\n" "6h, 1d"     "730 - 1095 Tage"
echo ""
read -p "History-Tage (oder 'a' für Automatik nach Timeframe) [Standard: a]: " HISTORY_INPUT
HISTORY_INPUT="${HISTORY_INPUT//[$'\r\n ']/}"

HISTORY_ARG=""
if [[ "$HISTORY_INPUT" =~ ^[0-9]+$ ]]; then
    HISTORY_ARG="--history-days $HISTORY_INPUT"
    echo -e "${CYAN}ℹ  Fester Rückblick: ${HISTORY_INPUT} Tage${NC}"
else
    echo -e "${GREEN}✔ Automatischer Rückblick nach Timeframe.${NC}"
fi

# ── 4. Backtest nach Discovery? ───────────────────────────────────────────────
echo ""
read -p "Backtest nach Discovery durchführen? (j/n) [Standard: j]: " RUN_BT
RUN_BT="${RUN_BT//[$'\r\n ']/}"
RUN_BT="${RUN_BT:-j}"

CAPITAL=1000
RISK=1.0
BT_START_DATE_ARG=""
BT_END_DATE_ARG=""
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    read -p "Startkapital in USDT [Standard: 1000]: " CAP_INPUT
    CAP_INPUT="${CAP_INPUT//[$'\r\n ']/}"
    if [[ "$CAP_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=$CAP_INPUT; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK_INPUT
    RISK_INPUT="${RISK_INPUT//[$'\r\n ']/}"
    if [[ "$RISK_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=$RISK_INPUT; fi

    # 70/30 Split?
    echo ""
    echo -e "${YELLOW}--- Train/Test Split ---${NC}"
    echo "  70/30: Genome-Discovery auf 70% der Daten, Backtest auf letzten 30%"
    echo "  Nein:  Backtest auf denselben Daten (In-Sample, optimistischer)"
    echo ""
    read -p "70/30 Out-of-Sample Split verwenden? (j/n) [Standard: j]: " USE_SPLIT
    USE_SPLIT="${USE_SPLIT//[$'\r\n ']/}"
    USE_SPLIT="${USE_SPLIT:-j}"

    if [[ "$USE_SPLIT" == "j" || "$USE_SPLIT" == "J" || "$USE_SPLIT" == "y" || "$USE_SPLIT" == "Y" ]]; then
        # History-Tage ermitteln (aus HISTORY_INPUT oder Automatik für 2h)
        TOTAL_DAYS=730
        if [[ "$HISTORY_INPUT" =~ ^[0-9]+$ ]]; then
            TOTAL_DAYS=$HISTORY_INPUT
        fi
        TRAIN_DAYS=$(( TOTAL_DAYS * 70 / 100 ))
        TEST_DAYS=$(( TOTAL_DAYS - TRAIN_DAYS ))
        SPLIT_DATE=$(date -d "$TEST_DAYS days ago" +%F)
        TODAY=$(date +%F)
        BT_START_DATE_ARG="--start-date $SPLIT_DATE"
        BT_END_DATE_ARG="--end-date $TODAY"
        echo -e "${CYAN}ℹ  Training:  letzte ${TRAIN_DAYS} Tage (bis $SPLIT_DATE)${NC}"
        echo -e "${CYAN}ℹ  Backtest:  letzte ${TEST_DAYS} Tage ($SPLIT_DATE → $TODAY)${NC}"
        # History auf 70% begrenzen für scan_and_learn
        HISTORY_ARG="--history-days $TRAIN_DAYS"
    fi
fi

# ── Pipeline starten ─────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Pipeline startet..."
echo "======================================================="
echo ""

# Schritt 1: Discovery + Evolver
# Coin/TF-Overrides via Python-Helfer in Scan-Pairs umwandeln
SCAN_ARGS=""

if [ -n "${DNABOT_OVERRIDE_COINS:-}" ] || [ -n "${DNABOT_OVERRIDE_TFS:-}" ]; then
    # Generiere temporäre Pair-Liste via Python
    PAIRS=$($PYTHON - <<'PYEOF'
import os, sys, json

coins_raw = os.environ.get('DNABOT_OVERRIDE_COINS', '').strip()
tfs_raw   = os.environ.get('DNABOT_OVERRIDE_TFS', '').strip()

# Aus settings.json fallback holen
try:
    with open('settings.json') as f:
        s = json.load(f)
    active = s.get('live_trading_settings', {}).get('active_strategies', [])
    auto_coins = list(dict.fromkeys(x['symbol'] for x in active if x.get('symbol')))
    auto_tfs   = list(dict.fromkeys(x['timeframe'] for x in active if x.get('timeframe')))
except Exception:
    auto_coins = ['BTC/USDT:USDT']
    auto_tfs   = ['4h']

def to_symbol(coin):
    coin = coin.strip().upper()
    if '/' not in coin:
        return f"{coin}/USDT:USDT"
    return coin

if coins_raw:
    coins = [to_symbol(c) for c in coins_raw.split()]
else:
    coins = auto_coins

if tfs_raw:
    tfs = [t.strip() for t in tfs_raw.split()]
else:
    tfs = auto_tfs

# Kartesisches Produkt ausgeben
for sym in coins:
    for tf in tfs:
        print(f"{sym} {tf}")
PYEOF
    )

    echo -e "${CYAN}Scan-Paare:${NC}"
    echo "$PAIRS" | while read -r sym tf; do
        echo "  → $sym ($tf)"
    done
    echo ""

    echo -e "${YELLOW}[Schritt 1/3] Genome Discovery + Evolver...${NC}"
    echo "$PAIRS" | while IFS=' ' read -r sym tf; do
        echo ""
        echo -e "${CYAN}  Scanne: $sym ($tf)${NC}"
        $PYTHON "$SCRIPT_DIR/scan_and_learn.py" \
            --symbol "$sym" --timeframe "$tf" $HISTORY_ARG --no-evolve
    done
    # Evolver einmal separat (nutzt die vollen Daten)
    echo ""
    echo -e "${CYAN}  Evolver läuft...${NC}"
    echo "$PAIRS" | while IFS=' ' read -r sym tf; do
        $PYTHON "$SCRIPT_DIR/scan_and_learn.py" \
            --symbol "$sym" --timeframe "$tf" $HISTORY_ARG
    done
else
    echo -e "${YELLOW}[Schritt 1/3] Genome Discovery + Evolver...${NC}"
    $PYTHON "$SCRIPT_DIR/scan_and_learn.py" $HISTORY_ARG
fi

echo ""

# Schritt 2: Backtest
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    echo -e "${YELLOW}[Schritt 2/3] Backtest...${NC}"
    if [ -n "${PAIRS:-}" ]; then
        echo "$PAIRS" | while IFS=' ' read -r sym tf; do
            echo -e "${CYAN}  Backtest: $sym ($tf)${NC}"
            $PYTHON "$SCRIPT_DIR/run_backtest.py" \
                --symbol "$sym" --timeframe "$tf" \
                --capital "$CAPITAL" --risk "$RISK" \
                $BT_START_DATE_ARG $BT_END_DATE_ARG
        done
    else
        $PYTHON "$SCRIPT_DIR/run_backtest.py" \
            --capital "$CAPITAL" --risk "$RISK" \
            $BT_START_DATE_ARG $BT_END_DATE_ARG
    fi
    echo ""
fi

# Schritt 3: Ergebnisse
echo -e "${YELLOW}[Schritt 3/3] Ergebnisse...${NC}"
$PYTHON "$SCRIPT_DIR/src/dnabot/analysis/show_results.py" --mode 1

echo ""
echo "======================================================="
echo -e "  ${GREEN}Pipeline abgeschlossen!${NC}"
echo ""
echo "  Nächste Schritte:"
echo "    1. Ergebnisse prüfen:   ./show_results.sh"
echo "    2. Strategien aktivieren: settings.json → \"active\": true"
echo "    3. Cronjob einrichten:  crontab -e"
echo "======================================================="

deactivate
