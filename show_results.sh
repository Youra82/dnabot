#!/bin/bash
# show_results.sh — Interaktives Analyse & Backtest-Menü (momentum_exit)
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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
export PYTHONIOENCODING=utf-8
if [ -f "$VENV_ACTIVATE" ]; then
    source "$VENV_ACTIVATE"
fi
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert (${PYTHON}).${NC}"

echo ""
echo -e "${YELLOW}Wähle einen Analyse-Modus:${NC}"
echo "  1) Einzel-Backtest               (jedes Pair wird simuliert, momentum_exit)"
echo "  2) Manuelle Portfolio-Simulation (du wählst die Pairs, gemeinsamer Kapital-Pool)"
echo "  3) Automatische Portfolio-Opt.   (Bot wählt das beste Team)"
echo "  4) Risiko-Gene Bibliothek        (aktive + Kandidaten-Gene pro Pair)"
echo "  5) Interaktive Charts            (Candlestick + Entry/Exit-Marker)"
read -p "Auswahl (1-5) [Standard: 4]: " MODE

if [[ ! "$MODE" =~ ^[1-5]?$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Standard (4).${NC}"
    MODE=4
fi
MODE=${MODE:-4}

# ─────────────────────────────────────────
# Mode 1: Einzel-Backtest
# ─────────────────────────────────────────
if [ "$MODE" == "1" ]; then
    echo ""
    read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=alle momentum_exit-Paare aus active_strategies]: " COINS_INPUT
    COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
    read -p "Timeframe(s) eingeben (z.B. 6h 4h) [leer=wie active_strategies]: " TF_INPUT
    TF_INPUT="${TF_INPUT//[$'\r\n']/}"

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    read -p "OOS-Fenster in Wochen [Standard: 26]: " OOS_WEEKS
    OOS_WEEKS="${OOS_WEEKS//[$'\r\n ']/}"
    if ! [[ "$OOS_WEEKS" =~ ^[0-9]+$ ]]; then OOS_WEEKS=26; fi

    echo ""
    if [ -z "$COINS_INPUT" ] && [ -z "$TF_INPUT" ]; then
        # Wie active_strategies konfiguriert: jedes momentum_exit-Pair mit
        # GENAU dessen settings.json-Parametern (risk_overrides), wie
        # run_momentum_exit_pipeline.sh.
        ./run_momentum_exit_pipeline.sh
    else
        if [ -z "$COINS_INPUT" ]; then
            echo -e "${RED}FEHLER: Timeframe(s) angegeben, aber keine Coins. Beide zusammen oder beide leer lassen.${NC}"
            deactivate 2>/dev/null
            exit 1
        fi
        read -p "RR-Ratio [Standard: 1.5]: " RR
        RR="${RR//[$'\r\n ']/}"; RR="${RR:-1.5}"
        read -p "Trailing-Callback in % [Standard: 0.5]: " TRAIL
        TRAIL="${TRAIL//[$'\r\n ']/}"; TRAIL="${TRAIL:-0.5}"
        read -p "seq_len (SL-Fenster in Kerzen) [Standard: 5]: " SEQLEN
        SEQLEN="${SEQLEN//[$'\r\n ']/}"; SEQLEN="${SEQLEN:-5}"

        TFS_LIST=(${TF_INPUT:-6h})
        for coin in $COINS_INPUT; do
            if [[ "$coin" == *"/"* ]]; then
                symbol="$coin"
            else
                symbol="$(echo "$coin" | tr '[:lower:]' '[:upper:]')/USDT:USDT"
            fi
            for tf in "${TFS_LIST[@]}"; do
                echo -e "${CYAN}=== $symbol $tf (rr=$RR trail=$TRAIL seq_len=$SEQLEN, manuell) ===${NC}"
                "$PYTHON" backtest_momentum_exit.py --symbol "$symbol" --timeframe "$tf" \
                    --capital "$CAPITAL" --risk "$RISK" --rr-ratio "$RR" \
                    --trailing-callback-pct "$TRAIL" --seq-len "$SEQLEN" --oos-weeks "$OOS_WEEKS"
            done
        done
    fi

# ─────────────────────────────────────────
# Mode 2: Manuelle Portfolio-Simulation
# ─────────────────────────────────────────
elif [ "$MODE" == "2" ]; then
    echo ""
    read -p "Startdatum (JJJJ-MM-TT) [Standard: alle Daten]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"

    read -p "Enddatum (JJJJ-MM-TT) [Standard: heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"

    read -p "Startkapital in USDT [Standard: 1000]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=1000; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    DATE_ARGS=""
    [ -n "$START_DATE" ] && DATE_ARGS="$DATE_ARGS --start-date $START_DATE"
    [ -n "$END_DATE" ]   && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    "$PYTHON" run_manual_portfolio_momentum_exit.py \
        --capital "$CAPITAL" \
        --risk "$RISK" \
        $DATE_ARGS

# ─────────────────────────────────────────
# Mode 3: Automatische Portfolio-Optimierung
# ─────────────────────────────────────────
elif [ "$MODE" == "3" ]; then
    echo ""
    echo -e "${YELLOW}Noch nicht verfügbar für momentum_exit — kommt in einem separaten Schritt.${NC}"
    echo "Bis dahin: Mode 2 (Manuelle Portfolio-Simulation) für Pair-Auswahl per Hand nutzen."

# ─────────────────────────────────────────
# Mode 5: Interaktive Charts
# ─────────────────────────────────────────
elif [ "$MODE" == "5" ]; then
    echo ""
    echo -e "${YELLOW}Noch nicht verfügbar für momentum_exit — kommt in einem separaten Schritt.${NC}"

# ─────────────────────────────────────────
# Mode 4: Risiko-Gene Bibliothek
# ─────────────────────────────────────────
else
    "$PYTHON" analysis/show_risk_genes.py
fi

deactivate 2>/dev/null
