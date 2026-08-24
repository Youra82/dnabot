#!/bin/bash
# run_momentum_exit_pipeline.sh -- backtestet ALLE momentum_exit-Strategien
# aus settings.json::active_strategies, mit GENAU den dort konfigurierten
# Parametern (nicht hartkodiert) -- Konsistenz-Check: ist das, was live
# konfiguriert ist, dasselbe, was validiert wurde (Fund AQ/AR)?
set -e
export PYTHONIOENCODING=utf-8
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
cd "$SCRIPT_DIR"

STRATS=$("$PYTHON" - << 'PYEOF'
import json
with open('settings.json') as f:
    settings = json.load(f)
strategies = settings['live_trading_settings']['active_strategies']
mom_strategies = [s for s in strategies if s.get('strategy_type') == 'momentum_exit']
for s in mom_strategies:
    ro = s.get('risk_overrides', {})
    mo = s.get('momentum_exit_overrides', {})
    print(f"{s['symbol']}|{s['timeframe']}|{ro.get('rr_ratio',2.0)}|{ro.get('risk_per_entry_pct',1.0)}|{ro.get('trailing_callback_rate_pct',1.0)}|{mo.get('seq_len',5)}")
PYEOF
)

echo "$STRATS" | while IFS='|' read -r sym tf rr risk trail seqlen; do
    [ -z "$sym" ] && continue
    echo "=== $sym $tf (rr=$rr risk=$risk trail=$trail seq_len=$seqlen, aus settings.json) ==="
    "$PYTHON" backtest_momentum_exit.py --symbol "$sym" --timeframe "$tf" \
        --capital 1000 --risk "$risk" --rr-ratio "$rr" \
        --trailing-callback-pct "$trail" --seq-len "$seqlen" --oos-weeks 26 \
        2>&1 | grep -E "BACKTEST:|Trades gesamt|Win-Rate|Total PnL|Max Drawdown|Profit Factor"
done

echo ""
echo "=== Gebuehren-Impact-Analyse (nur momentum_exit, kein Pool mit altem Genom-System) ==="
"$PYTHON" analysis/fee_impact_momentum_exit.py --capital 1000 --risk 1.0

echo "=== PIPELINE FERTIG ==="
