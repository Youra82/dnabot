#!/bin/bash
# update.sh — Update des dnabot vom Git (titanbot-Stil)
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Starte dnabot Update..."

# secret.json sichern (wird von git reset nicht zurückgesetzt)
if [ -f "secret.json" ]; then
    cp secret.json secret.json.bak
    echo "secret.json gesichert."
fi

# Git update
git fetch origin
git reset --hard origin/main

# secret.json wiederherstellen
if [ -f "secret.json.bak" ]; then
    cp secret.json.bak secret.json
    rm secret.json.bak
    echo "secret.json wiederhergestellt."
fi

# Cache bereinigen
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Skripte ausführbar machen
chmod +x *.sh

echo "Update abgeschlossen!"
