# src/dnabot/utils/strategy_overrides.py
# Geteilte Aufloesung von Per-Strategy-Overrides aus active_strategies.
#
# Zentralisiert (statt in run.py und run_backtest.py getrennt implementiert),
# damit der Backtest exakt dieselben risk_overrides/genome_overrides sieht
# wie die Live-Strategie fuer dasselbe (symbol, timeframe) -- sonst validiert
# der Backtest eine Config, die live gar nicht verwendet wird (z.B.
# use_kelly_sizing nur live aktiv, im Backtest unsichtbar).


def find_strategy_overrides(symbol: str, timeframe: str, settings: dict) -> dict:
    """
    Sucht per-Strategy-Overrides in active_strategies.
    Felder 'risk_overrides' und 'genome_overrides' überschreiben globale Werte.
    'strategy_type' waehlt den Signal-Mechanismus ('genome' [Standard] oder
    'momentum_exit', siehe strategy/momentum_exit_logic.py + Fund AQ in
    research_dnabot_direction_calibration.md). 'momentum_exit_overrides'
    ueberschreibt momentum_exit_settings global.

    Beispiel in settings.json:
        { "symbol": "ETH/USDT:USDT", "timeframe": "1h",
          "risk_overrides":   { "leverage": 3, "risk_per_entry_pct": 0.5 },
          "genome_overrides": { "min_score": 0.12 } }
        { "symbol": "BTC/USDT:USDT", "timeframe": "6h",
          "strategy_type": "momentum_exit",
          "risk_overrides": { "rr_ratio": 1.5, "risk_per_entry_pct": 1.0,
                               "trailing_callback_rate_pct": 0.5 },
          "momentum_exit_overrides": { "seq_len": 5 } }
    """
    for strategy in settings.get('live_trading_settings', {}).get('active_strategies', []):
        if strategy.get('symbol') == symbol and strategy.get('timeframe') == timeframe:
            return {
                'risk':   strategy.get('risk_overrides', {}),
                'genome': strategy.get('genome_overrides', {}),
                'strategy_type': strategy.get('strategy_type', 'genome'),
                'momentum_exit': strategy.get('momentum_exit_overrides', {}),
            }
    return {'risk': {}, 'genome': {}, 'strategy_type': 'genome', 'momentum_exit': {}}
