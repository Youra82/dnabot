# src/dnabot/utils/strategy_overrides.py
# Geteilte Aufloesung von Per-Strategy-Overrides aus active_strategies.
#
# Zentralisiert (statt in run.py und Backtest-Skripten getrennt implementiert),
# damit Backtest und Live-Strategie exakt dieselben risk_overrides/
# momentum_exit_overrides sehen fuer dasselbe (symbol, timeframe).


def find_strategy_overrides(symbol: str, timeframe: str, settings: dict) -> dict:
    """
    Sucht per-Strategy-Overrides in active_strategies.
    'risk_overrides' ueberschreibt globale risk_settings (Fallback-Werte,
    falls kein aktives Risiko-Gen in der DB existiert -- siehe
    strategy/momentum_exit_logic.py). 'momentum_exit_overrides' ueberschreibt
    momentum_exit_settings global (z.B. enabled/seq_len-Fallback).

    Beispiel in settings.json:
        { "symbol": "BTC/USDT:USDT", "timeframe": "6h", "active": true,
          "risk_overrides": { "rr_ratio": 1.5, "risk_per_entry_pct": 1.0,
                               "trailing_callback_rate_pct": 0.5 },
          "momentum_exit_overrides": { "enabled": true, "seq_len": 5 } }
    """
    for strategy in settings.get('live_trading_settings', {}).get('active_strategies', []):
        if strategy.get('symbol') == symbol and strategy.get('timeframe') == timeframe:
            return {
                'risk': strategy.get('risk_overrides', {}),
                'momentum_exit': strategy.get('momentum_exit_overrides', {}),
            }
    return {'risk': {}, 'momentum_exit': {}}
