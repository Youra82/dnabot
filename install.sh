#!/bin/bash
# install.sh — Erstinstallation des dnabot auf dem VPS
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "  dnabot — Installation"
echo "============================================================"

# Python venv erstellen
if [ ! -d ".venv" ]; then
    echo "Erstelle virtuelle Umgebung..."
    python3 -m venv .venv
fi

echo "Installiere Abhängigkeiten..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Ordner anlegen
mkdir -p artifacts/db artifacts/tracker artifacts/results logs

# Skripte ausführbar machen
chmod +x *.sh

# secret.json prüfen
if [ ! -f "secret.json" ]; then
    cp secret.json.example secret.json
    echo ""
    echo "WICHTIG: secret.json wurde erstellt."
    echo "         Bitte mit echten API-Keys befüllen!"
fi

echo ""
echo "Installation abgeschlossen!"
echo ""
echo "Nächste Schritte:"
echo "  1. secret.json mit API-Keys befüllen"
echo "  2. settings.json konfigurieren (Symbole, Timeframes)"
echo "  3. ./run_pipeline.sh ausführen (Genome-Discovery)"
echo "  4. active=true in settings.json setzen"
echo "  5. Cronjob einrichten:"
echo "     */15 * * * * cd $SCRIPT_DIR && .venv/bin/python3 master_runner.py"
