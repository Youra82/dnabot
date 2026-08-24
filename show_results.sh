#!/bin/bash
# show_results.sh — zeigt die Risiko-Gen-Bibliothek der momentum_exit-
# Strategie (aktive + Kandidaten-Gene pro Pair, siehe genome/risk_genome_db.py).
# Fuer Backtests/Fee-Impact siehe run_momentum_exit_pipeline.sh.
RED='\033[0;31m'
NC='\033[0m'

# Plattformuebergreifend (Windows .venv/Scripts UND Unix .venv/bin).
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

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    PYTHON=python
fi

$PYTHON analysis/show_risk_genes.py

deactivate
