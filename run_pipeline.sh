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
    echo -e "${GREEN}✔ Coins und Timeframes werden aus scan_settings (falls gesetzt) bzw. active_strategies übernommen.${NC}"
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
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    read -p "Startkapital in USDT [Standard: 1000]: " CAP_INPUT
    CAP_INPUT="${CAP_INPUT//[$'\r\n ']/}"
    if [[ "$CAP_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=$CAP_INPUT; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK_INPUT
    RISK_INPUT="${RISK_INPUT//[$'\r\n ']/}"
    if [[ "$RISK_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=$RISK_INPUT; fi
fi

# ── 4b. Alphabet-Optimizer? ──────────────────────────────────────────────────
# Laeuft VOR der Discovery (Schritt 1) -- jeder Trial macht seinen eigenen
# vollstaendigen Discovery+Backtest-Durchlauf mit einem Kandidaten-Alphabet,
# eine vorherige Default-Alphabet-Discovery waere sonst verschwendete Arbeit
# (database.py erkennt Alphabet-Wechsel zwar und rescanned automatisch neu,
# aber dann eben zweimal statt einmal). Uebernimmt bestaetigte Pairs (per
# In-Sample/Out-of-Sample-Split, siehe alphabet_optimizer.py) automatisch in
# settings.json::genome_settings.alphabet_by_pair -- die folgende Discovery
# nutzt das dann direkt (encoder.py::resolve_alphabet()).
echo ""
echo -e "${YELLOW}Alphabet-Optimizer pro Pair (analysis/alphabet_optimizer.py)${NC}"
echo "  Sucht per Optuna (mit echtem In-Sample/Out-of-Sample-Split) ein eigenes"
echo "  Encoder-Alphabet pro Coin/Timeframe -- z.B. wenn mit dem Standard-Alphabet"
echo "  kaum/keine Genome aktiviert werden. Uebernimmt nur Pairs, die sich Out-of-"
echo "  Sample bestaetigen, automatisch in settings.json."
echo "  Läuft VOR der Discovery. Je Pair mehrere Minuten (je nach Trials) --"
echo "  bei vielen Pairs (--all-scan-pairs) fuer Overnight-Laeufe gedacht."
read -p "Starten? (j/n) [Standard: n]: " RUN_ALPHABET
RUN_ALPHABET="${RUN_ALPHABET//[$'\r\n ']/}"

ALPHABET_TRIALS=20
if [[ "$RUN_ALPHABET" == "j" || "$RUN_ALPHABET" == "J" || "$RUN_ALPHABET" == "y" || "$RUN_ALPHABET" == "Y" ]]; then
    read -p "Optuna-Trials pro Pair [Standard: 20]: " ALPHA_TRIALS_INPUT
    ALPHA_TRIALS_INPUT="${ALPHA_TRIALS_INPUT//[$'\r\n ']/}"
    if [[ "$ALPHA_TRIALS_INPUT" =~ ^[0-9]+$ ]]; then ALPHABET_TRIALS=$ALPHA_TRIALS_INPUT; fi
fi

# ── 5. min_samples-Optuna-Sweep? ────────────────────────────────────────────
# Laeuft NACH Discovery (braucht die Genome-Occurrences), aber VOR dem
# Backtest (damit der Backtest die optimierten Werte nutzt, nicht die alten
# Default-Werte). Nicht parallel zur Discovery -- zwei Prozesse, die
# gleichzeitig in dieselbe genome.db schreiben/lesen, koennen sich blockieren
# (SQLite-Locking) und der Sweep wuerde fuer noch nicht fertig gescannte
# Pairs unvollstaendige Ergebnisse liefern.
echo ""
echo -e "${YELLOW}Optuna-Sweep fuer min_samples_to_activate pro Timeframe (analysis/min_samples_sweep.py)${NC}"
echo "  Sucht nach der Discovery den PnL-besten min_samples-Wert je Timeframe,"
echo "  uebernimmt ihn in settings.json und laesst den Evolver damit neu laufen --"
echo "  bevor Backtest und Ergebnisse folgen."
echo "  Kann je nach Pool-Groesse mehrere Stunden dauern -- fuer Overnight-Laeufe gedacht."
read -p "Starten? (j/n) [Standard: n]: " RUN_SWEEP
RUN_SWEEP="${RUN_SWEEP//[$'\r\n ']/}"

SWEEP_TRIALS=20
if [[ "$RUN_SWEEP" == "j" || "$RUN_SWEEP" == "J" || "$RUN_SWEEP" == "y" || "$RUN_SWEEP" == "Y" ]]; then
    read -p "Optuna-Trials pro Timeframe [Standard: 20]: " TRIALS_INPUT
    TRIALS_INPUT="${TRIALS_INPUT//[$'\r\n ']/}"
    if [[ "$TRIALS_INPUT" =~ ^[0-9]+$ ]]; then SWEEP_TRIALS=$TRIALS_INPUT; fi
fi

# ── Pipeline starten ─────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Pipeline startet..."
echo "======================================================="
echo ""

# Schritt 0 (falls gewaehlt): Alphabet-Optimizer -- VOR der Discovery, damit
# diese direkt mit dem optimierten/bestaetigten Alphabet laeuft statt mit dem
# Default und spaeter neu scannen zu muessen. Nutzt dieselben
# DNABOT_OVERRIDE_COINS/_TFS wie Schritt 1 (bereits oben exportiert, falls
# gesetzt) -- kein eigener --symbol/--timeframe-Loop noetig, das Skript
# loest die Pairs selbst genauso auf.
if [[ "$RUN_ALPHABET" == "j" || "$RUN_ALPHABET" == "J" || "$RUN_ALPHABET" == "y" || "$RUN_ALPHABET" == "Y" ]]; then
    echo -e "${YELLOW}[Schritt 0] Alphabet-Optimizer (Optuna, IS/OOS-Split)...${NC}"
    $PYTHON "$SCRIPT_DIR/analysis/alphabet_optimizer.py" \
        --n-trials "$ALPHABET_TRIALS" --auto-apply
    echo ""
fi

# Schritt 1: Discovery + Evolver
# Coin/TF-Overrides via Python-Helfer in Scan-Pairs umwandeln
SCAN_ARGS=""

if [ -n "${DNABOT_OVERRIDE_COINS:-}" ] || [ -n "${DNABOT_OVERRIDE_TFS:-}" ]; then
    # Pair-Liste ueber ein echtes Skript statt eines inline Python-Heredocs --
    # letzteres war auf mindestens einem Zielsystem reproduzierbar leer
    # (0 statt der erwarteten Paare), obwohl identische Logik als eigenstaendige
    # .py-Datei und lokal immer korrekt lief (vermutlich Heredoc-Terminator-/
    # Zeilenenden-Empfindlichkeit) -- siehe analysis/resolve_scan_pairs.py.
    PAIRS=$($PYTHON "$SCRIPT_DIR/analysis/resolve_scan_pairs.py")

    if [ -z "$PAIRS" ]; then
        echo -e "${RED}FEHLER: Konnte aus der Eingabe keine Scan-Paare erzeugen ${NC}"
        echo -e "${RED}(Coins='${DNABOT_OVERRIDE_COINS:-}' Timeframes='${DNABOT_OVERRIDE_TFS:-}'). Abbruch.${NC}"
        deactivate
        exit 1
    fi

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

# Zusatzschritt: min_samples-Optuna-Sweep + Uebernahme (LAEUFT VOR dem
# Backtest, nicht danach -- der Backtest soll die optimierten Werte schon
# nutzen, nicht mit den alten Default-Werten laufen und dann ungenutzt
# verpuffen).
if [[ "$RUN_SWEEP" == "j" || "$RUN_SWEEP" == "J" || "$RUN_SWEEP" == "y" || "$RUN_SWEEP" == "Y" ]]; then
    echo "======================================================="
    echo -e "  ${YELLOW}Zusatzschritt: min_samples-Optuna-Sweep${NC}"
    echo "  ${SWEEP_TRIALS} Trials pro Timeframe -- das kann laenger dauern."
    echo "======================================================="
    $PYTHON "$SCRIPT_DIR/analysis/min_samples_sweep.py" --n-trials "$SWEEP_TRIALS"

    echo ""
    echo -e "${CYAN}  Uebernehme Sweep-Ergebnisse in settings.json (scan_settings.min_samples_by_timeframe)...${NC}"
    $PYTHON "$SCRIPT_DIR/analysis/apply_min_samples_sweep.py"

    echo ""
    echo -e "${CYAN}  Evolver neu mit den optimierten min_samples-Werten...${NC}"
    if [ -n "${PAIRS:-}" ]; then
        echo "$PAIRS" | while IFS=' ' read -r sym tf; do
            $PYTHON "$SCRIPT_DIR/scan_and_learn.py" --symbol "$sym" --timeframe "$tf" $HISTORY_ARG
        done
    else
        $PYTHON "$SCRIPT_DIR/scan_and_learn.py" $HISTORY_ARG
    fi
    echo ""
fi

# Schritt 2: Backtest
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    echo -e "${YELLOW}[Schritt 2/3] Backtest...${NC}"
    if [ -n "${PAIRS:-}" ]; then
        echo "$PAIRS" | while IFS=' ' read -r sym tf; do
            echo -e "${CYAN}  Backtest: $sym ($tf)${NC}"
            $PYTHON "$SCRIPT_DIR/run_backtest.py" \
                --symbol "$sym" --timeframe "$tf" \
                --capital "$CAPITAL" --risk "$RISK"
        done
    else
        $PYTHON "$SCRIPT_DIR/run_backtest.py" \
            --capital "$CAPITAL" --risk "$RISK"
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
