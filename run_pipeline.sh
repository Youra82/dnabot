#!/bin/bash
# run_pipeline.sh — Vollständige dnabot Pipeline
#
# Schritt 1: scan_and_learn.py  → Genome-Discovery + Evolver
# Schritt 2: Backtest (optional) → Validierung der aktiven Genome
# Schritt 3: show_results.py    → Zusammenfassung

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo "FEHLER: Python-Interpreter nicht gefunden: $PYTHON"
    echo "       Erst install.sh ausführen!"
    exit 1
fi

echo "============================================================"
echo "  dnabot Pipeline — Adaptive Market Genome System"
echo "============================================================"
echo ""

# Optionale Argumente an scan_and_learn.py weiterleiten
ARGS="$@"

echo "[Step 1/3] Genome Discovery + Evolver..."
$PYTHON "$SCRIPT_DIR/scan_and_learn.py" $ARGS
echo ""

echo "[Step 2/3] Ergebnisse anzeigen..."
$PYTHON "$SCRIPT_DIR/src/dnabot/analysis/show_results.py"
echo ""

echo "============================================================"
echo "  Pipeline abgeschlossen!"
echo "  Nächster Schritt: active=true in settings.json setzen"
echo "  und Cronjob für master_runner.py einrichten."
echo "============================================================"
