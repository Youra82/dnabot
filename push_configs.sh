#!/bin/bash
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$SCRIPT_DIR"

echo ""
echo -e "${YELLOW}========== DNABOT SETTINGS PUSHEN ==========${NC}"
echo ""

# Prüfe ob settings.json existiert
if [ ! -f "settings.json" ]; then
    echo -e "${RED}❌ settings.json nicht gefunden.${NC}"
    exit 1
fi

# Aktive Strategien anzeigen
echo "Aktive Strategien in settings.json:"
python3 -c "
import json
with open('settings.json') as f:
    s = json.load(f)
strategies = s.get('live_trading_settings', {}).get('active_strategies', [])
risk = s.get('risk_settings', {}).get('risk_per_entry_pct', '?')
print(f'  Risiko/Trade: {risk}%')
for st in strategies:
    sym = st.get('symbol', '?')
    tf  = st.get('timeframe', '?')
    act = '✓' if st.get('active') else '✗'
    print(f'  [{act}] {sym} ({tf})')
" 2>/dev/null || echo "  (Konnte settings.json nicht parsen)"
echo ""

# Änderungen prüfen
git add settings.json
STAGED=$(git diff --cached --name-only)

if [ -z "$STAGED" ]; then
    echo -e "${YELLOW}ℹ  Keine Änderungen — settings.json ist bereits aktuell im Repo.${NC}"
    exit 0
fi

# Commit
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
git commit -m "Update: settings.json aktualisiert ($TIMESTAMP)"

# Push
echo ""
echo -e "${YELLOW}Pushe auf origin/main...${NC}"
git push origin HEAD:main

if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✅ settings.json erfolgreich gepusht!${NC}"
else
    echo ""
    echo -e "${YELLOW}Remote hat neuere Commits — führe Rebase durch...${NC}"
    git pull origin main --rebase
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Rebase fehlgeschlagen. Bitte manuell lösen.${NC}"
        exit 1
    fi
    git push origin HEAD:main
    if [ $? -eq 0 ]; then
        echo ""
        echo -e "${GREEN}✅ settings.json erfolgreich gepusht!${NC}"
    else
        echo -e "${RED}❌ Push nach Rebase fehlgeschlagen.${NC}"
        exit 1
    fi
fi
