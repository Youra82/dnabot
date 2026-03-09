# src/dnabot/genome/regime.py
# Market Regime Detection
#
# Erkennt den aktuellen Marktmodus und verhindert, dass der Bot
# in unpassenden Marktphasen handelt.
#
# Regime:
#   TREND     — klare Richtung (ADX > 25), gut für Momentum-Genome
#   RANGE     — Seitwärtsmarkt (ADX < 20), gut für Reversal-Genome
#   HIGH_VOL  — unkontrollierte Volatilität (ATR-Spike), Handel vermeiden
#   NEUTRAL   — Übergangsphase, vorsichtiger Handel möglich
#
# Warum das wichtig ist:
#   Ein Pattern das im Trend funktioniert, kann im Range-Markt
#   zu 70% verlieren — und umgekehrt. Der Regime-Filter ist die
#   wirksamste Einzelmaßnahme gegen Fehlsignale.

import pandas as pd
import logging
import ta

logger = logging.getLogger(__name__)

# Regime-Konstanten
REGIME_TREND    = "TREND"
REGIME_RANGE    = "RANGE"
REGIME_HIGH_VOL = "HIGH_VOL"
REGIME_NEUTRAL  = "NEUTRAL"

ALL_REGIMES = [REGIME_TREND, REGIME_RANGE, REGIME_HIGH_VOL, REGIME_NEUTRAL]


def detect_regime(
    df: pd.DataFrame,
    adx_period: int = 14,
    atr_period: int = 14,
    atr_ma_period: int = 50,
    adx_trend_threshold: float = 25.0,
    adx_range_threshold: float = 20.0,
    atr_spike_factor: float = 1.5,
) -> str:
    """
    Erkennt den aktuellen Marktmodus aus OHLCV-Daten.

    Logik:
      1. ATR-Spike prüfen: Aktuelle ATR deutlich über 50-Perioden-Durchschnitt
         → HIGH_VOL (Chaos, kein Trading)
      2. ADX-Trend prüfen: Trendstärke hoch
         → TREND
      3. ADX-Range prüfen: Trendstärke niedrig
         → RANGE
      4. Sonst: NEUTRAL

    Args:
        df: OHLCV DataFrame (mind. 60 Kerzen empfohlen)
        adx_trend_threshold: ADX über diesem Wert = Trend
        adx_range_threshold: ADX unter diesem Wert = Range
        atr_spike_factor: ATR-Faktor über dem MA = HIGH_VOL

    Returns:
        Regime-String: "TREND" | "RANGE" | "HIGH_VOL" | "NEUTRAL"
    """
    if len(df) < atr_ma_period + 5:
        logger.warning(f"Zu wenig Kerzen für Regime-Erkennung ({len(df)}). Nutze NEUTRAL.")
        return REGIME_NEUTRAL

    try:
        # ATR berechnen
        atr_indicator = ta.volatility.AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'],
            window=atr_period, fillna=True
        )
        atr_series = atr_indicator.average_true_range()
        current_atr = float(atr_series.iloc[-1])
        atr_ma = float(atr_series.rolling(window=atr_ma_period, min_periods=10).mean().iloc[-1])

        # ADX berechnen
        adx_indicator = ta.trend.ADXIndicator(
            high=df['high'], low=df['low'], close=df['close'],
            window=adx_period, fillna=True
        )
        current_adx = float(adx_indicator.adx().iloc[-1])

        # Regime bestimmen
        atr_ratio = current_atr / atr_ma if atr_ma > 0 else 1.0

        if atr_ratio >= atr_spike_factor:
            regime = REGIME_HIGH_VOL
        elif current_adx >= adx_trend_threshold:
            regime = REGIME_TREND
        elif current_adx <= adx_range_threshold:
            regime = REGIME_RANGE
        else:
            regime = REGIME_NEUTRAL

        logger.debug(
            f"Regime: {regime} | ADX={current_adx:.1f} | "
            f"ATR={current_atr:.4f} | ATR-MA={atr_ma:.4f} | ATR-Ratio={atr_ratio:.2f}"
        )
        return regime

    except Exception as e:
        logger.error(f"Fehler bei Regime-Erkennung: {e}. Fallback: NEUTRAL.")
        return REGIME_NEUTRAL


def is_regime_allowed(current_regime: str, allowed_regimes: list[str]) -> bool:
    """
    Prüft ob im aktuellen Regime gehandelt werden darf.

    HIGH_VOL wird IMMER blockiert, unabhängig von allowed_regimes.
    """
    if current_regime == REGIME_HIGH_VOL:
        return False
    return current_regime in allowed_regimes
