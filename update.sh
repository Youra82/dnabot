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

# settings.json sichern -- ist in Git getrackt, git reset --hard wirft sonst
# jede lokale Laufzeit-Optimierung weg: per min_samples_sweep.py getunte
# min_samples_by_timeframe und die vom Portfolio-Optimizer gewaehlten
# active_strategies -- landet nur hier, nie im Repo committed.
if [ -f "settings.json" ]; then
    cp settings.json settings.json.bak
    echo "settings.json gesichert."
fi

# alphabet_overrides.json sichern -- gleicher Grund wie settings.json:
# vom Alphabet-Optimizer bestaetigte alphabet_by_pair/rr_ratio_by_pair pro
# Pair (siehe genome/alphabet_store.py) sind lokale Laufzeit-Optimierung,
# kein im Repo committeter Stand.
if [ -f "alphabet_overrides.json" ]; then
    cp alphabet_overrides.json alphabet_overrides.json.bak
    echo "alphabet_overrides.json gesichert."
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

# settings.json wiederherstellen
if [ -f "settings.json.bak" ]; then
    cp settings.json.bak settings.json
    rm settings.json.bak
    echo "settings.json wiederhergestellt."
fi

# alphabet_overrides.json wiederherstellen
if [ -f "alphabet_overrides.json.bak" ]; then
    cp alphabet_overrides.json.bak alphabet_overrides.json
    rm alphabet_overrides.json.bak
    echo "alphabet_overrides.json wiederhergestellt."
fi

# Cache bereinigen
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Skripte ausführbar machen
chmod +x *.sh

echo "Update abgeschlossen!"
