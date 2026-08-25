#!/bin/bash
# show_results.sh — zeigt die Risiko-Gen-Bibliothek der momentum_exit-
# Strategie (aktive + Kandidaten-Gene pro Pair, siehe genome/risk_genome_db.py).
# Fuer Backtests/Gebühren-Check siehe run_momentum_exit_pipeline.sh bzw.
# ./run_pipeline.sh (fragt danach interaktiv).

GREEN='\033[0;32m'
RED='\033[0;31m'
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
export PYTHONIOENCODING=utf-8
if [ -f "$VENV_ACTIVATE" ]; then
    source "$VENV_ACTIVATE"
fi
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert (${PYTHON}).${NC}"

echo ""
echo "======================================================="
echo "     dnabot — Risiko-Gen-Report (momentum_exit)"
echo "======================================================="

"$PYTHON" "$SCRIPT_DIR/analysis/show_risk_genes.py"

deactivate 2>/dev/null
