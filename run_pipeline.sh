#!/bin/bash
# run_pipeline.sh — Interaktive dnabot Pipeline (momentum_exit / Risiko-Gene)
#
# Schritt 1: Optionen abfragen
# Schritt 2: risk_genome_discover.py       → Risiko-Gen-Discovery + Evolver (IS/OOS-gated)
# Schritt 3: run_momentum_exit_pipeline.sh → Backtest + Gebühren-Check (optional)
# Schritt 4: analysis/show_risk_genes.py   → Zusammenfassung

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Venv finden (Unix-Layout ODER Windows-Layout, z.B. Git Bash unter Windows) ──
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
# Windows-Konsole: cp1252-Default bricht bei UTF-8-Sonderzeichen in Log-
# Meldungen (Pfeile, Kastenzeichen) -- nicht fatal, aber stoerende
# "Logging error"-Tracebacks. Betrifft Unix-Terminals nicht, dort ist es ein No-Op.
export PYTHONIOENCODING=utf-8
if [ -f "$VENV_ACTIVATE" ]; then
    source "$VENV_ACTIVATE"
fi
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert (${PYTHON}).${NC}"

# ── Header ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "     dnabot — momentum_exit Risiko-Gen-Discovery"
echo "======================================================="
echo ""

# ── 1. Alte Risiko-Gen-Datenbank löschen? ────────────────────────────────────
DB_PATH="$SCRIPT_DIR/artifacts/db/risk_genome.db"
read -p "Alte Risiko-Gen-Datenbank vor dem Start löschen (Neustart)? (j/n) [Standard: n]: " RESET_DB
RESET_DB="${RESET_DB//[$'\r\n ']/}"
if [[ "$RESET_DB" == "j" || "$RESET_DB" == "J" || "$RESET_DB" == "y" || "$RESET_DB" == "Y" ]]; then
    echo ""
    echo -e "${YELLOW}Kompletter Neustart -- entferne alle bisher gelernten Risiko-Gene:${NC}"
    shopt -s nullglob
    DB_FILES=("$SCRIPT_DIR"/artifacts/db/risk_genome.db*)
    shopt -u nullglob
    if [ ${#DB_FILES[@]} -gt 0 ]; then
        rm -f "${DB_FILES[@]}"
        echo -e "  ${GREEN}✔ gelöscht:${NC} artifacts/db/risk_genome.db (+ WAL/SHM)"
    else
        echo -e "  ${CYAN}·  nicht vorhanden (übersprungen):${NC} artifacts/db/risk_genome.db"
    fi
    shopt -s nullglob
    RESULT_JSON_FILES=("$SCRIPT_DIR"/artifacts/results/backtest_*_momentum_exit.json)
    shopt -u nullglob
    if [ ${#RESULT_JSON_FILES[@]} -gt 0 ]; then
        rm -f "${RESULT_JSON_FILES[@]}"
        echo -e "  ${GREEN}✔ gelöscht:${NC} artifacts/results/backtest_*_momentum_exit.json (${#RESULT_JSON_FILES[@]} Datei(en))"
    else
        echo -e "  ${CYAN}·  nicht vorhanden (übersprungen):${NC} artifacts/results/backtest_*_momentum_exit.json"
    fi
    echo -e "${GREEN}✔ Neustart abgeschlossen — alle bisherigen Risiko-Gene entfernt.${NC}"
else
    echo -e "${GREEN}✔ Bestehende Risiko-Gen-Datenbank wird beibehalten (Self-Learning bleibt erhalten).${NC}"
fi

# ── 2. Coins / Timeframes ────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Coins und Timeframes:${NC}"
echo "  Leer lassen → automatisch alle strategy_type=\"momentum_exit\"-Paare"
echo "  aus active_strategies in settings.json übernehmen"
echo ""
read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=auto]: " COINS_INPUT
read -p "Timeframe(s) eingeben (z.B. 6h 4h) [leer=auto]: " TF_INPUT

COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
TF_INPUT="${TF_INPUT//[$'\r\n']/}"

# Paarliste bauen: explizite Coins x Timeframes (Cartesian), sonst leer =
# risk_genome_discover.py laeuft ohne Argumente (verarbeitet automatisch
# alle momentum_exit-Paare aus active_strategies).
PAIRS=""
if [ -n "$COINS_INPUT" ] || [ -n "$TF_INPUT" ]; then
    COINS_LIST=(${COINS_INPUT:-__AUTO__})
    TFS_LIST=(${TF_INPUT:-6h})
    if [ -z "$COINS_INPUT" ]; then
        echo -e "${RED}FEHLER: Timeframe(s) angegeben, aber keine Coins. Beide zusammen oder beide leer lassen.${NC}"
        deactivate 2>/dev/null
        exit 1
    fi
    for coin in "${COINS_LIST[@]}"; do
        if [[ "$coin" == *"/"* ]]; then
            symbol="$coin"
        else
            symbol="$(echo "$coin" | tr '[:lower:]' '[:upper:]')/USDT:USDT"
        fi
        for tf in "${TFS_LIST[@]}"; do
            PAIRS="$PAIRS$symbol $tf"$'\n'
        done
    done
    PAIRS="${PAIRS%$'\n'}"
    echo -e "${CYAN}Paare:${NC}"
    echo "$PAIRS" | while read -r sym tf; do
        echo "  → $sym ($tf)"
    done
else
    echo -e "${GREEN}✔ Alle momentum_exit-Paare aus active_strategies werden verarbeitet.${NC}"
fi

# ── 3. Backtest nach Discovery? ──────────────────────────────────────────────
echo ""
read -p "Backtest + Gebühren-Check nach der Discovery durchführen? (j/n) [Standard: j]: " RUN_BT
RUN_BT="${RUN_BT//[$'\r\n ']/}"
RUN_BT="${RUN_BT:-j}"

# ── Pipeline starten ─────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Pipeline startet..."
echo "======================================================="
echo ""

# Schritt 1: Risiko-Gen-Discovery (IS/OOS-gated, siehe risk_genome_discover.py)
echo -e "${YELLOW}[Schritt 1/2] Risiko-Gen-Discovery...${NC}"
if [ -n "$PAIRS" ]; then
    TOTAL_PAIRS=$(echo "$PAIRS" | wc -l)
    PAIR_IDX=0
    echo "$PAIRS" | while IFS=' ' read -r sym tf; do
        PAIR_IDX=$((PAIR_IDX + 1))
        echo ""
        echo -e "${CYAN}  [$PAIR_IDX/$TOTAL_PAIRS] Discovery: $sym ($tf)${NC}"
        "$PYTHON" "$SCRIPT_DIR/risk_genome_discover.py" --symbol "$sym" --timeframe "$tf"
    done
else
    "$PYTHON" "$SCRIPT_DIR/risk_genome_discover.py"
fi

echo ""

# Schritt 2: Backtest + Gebühren-Check über die konfigurierten momentum_exit-
# Strategien aus active_strategies (run_momentum_exit_pipeline.sh liest
# GENAU deren settings.json-Parameter, kein separater Pair-Loop hier noetig).
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    echo -e "${YELLOW}[Schritt 2/2] Backtest + Gebühren-Check...${NC}"
    "$SCRIPT_DIR/run_momentum_exit_pipeline.sh"
    echo ""
fi

# Ergebnisse
echo -e "${YELLOW}Ergebnisse...${NC}"
"$PYTHON" "$SCRIPT_DIR/analysis/show_risk_genes.py"

echo ""
echo "======================================================="
echo -e "  ${GREEN}Pipeline abgeschlossen!${NC}"
echo ""
echo "  Nächste Schritte:"
echo "    1. Ergebnisse prüfen:      ./show_results.sh"
echo "    2. Strategien aktivieren:  settings.json → \"active\": true"
echo "    3. Cronjob einrichten:     crontab -e"
echo "======================================================="

deactivate 2>/dev/null
