# src/dnabot/genome/daily_bias.py
# Tagestrend-Filter: EINE geteilte Implementierung fuer Backtest UND Live.
#
# Warum ein eigenes Modul statt getrennter Logik in backtester.py und
# genome_logic.py: Live-Bot und Backtest muessen exakt dasselbe tun, sonst
# validiert der Backtest ein Verhalten, das live gar nicht existiert (siehe
# Projekthistorie: mehrere vorherige Bugs kamen genau aus dieser Art von
# Live/Backtest-Divergenz, z.B. discovery.py's fruehere pfadunabhaengige
# Winrate-Berechnung). Eine Funktion, zwei Aufrufer.
#
# Regel: LAUFENDER Bias der AKTUELLEN (heutigen, noch nicht abgeschlossenen)
# Tageskerze -- nicht der vorherige, bereits geschlossene Tag. Rot bisher
# (aktueller Preis < heutiger Tages-Open) -> nur Shorts zugelassen, Longs
# werden verworfen. Gruen bisher (Preis >= Tages-Open) -> nur Longs, Shorts
# werden verworfen. Aktualisiert sich also mit jeder neuen Kerze waehrend
# der Tag laeuft.
#
# Kein Look-Ahead trotz "aktueller" Kerze: der Tages-Open ist ab der ERSTEN
# Kerze des Kalendertags bekannt (Handelspreis, kein Blick in die Zukunft),
# der Vergleich nutzt pro Bar nur den zu diesem Zeitpunkt bereits bekannten
# aktuellen Close -- beides kausal verfuegbare Information.

import numpy as np
import pandas as pd


class DailyBias:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


def compute_daily_bias_series(df: pd.DataFrame) -> pd.Series:
    """
    Laufender Intraday-Bias pro Bar: Farbe der AKTUELLEN Tageskerze bis zu
    diesem Zeitpunkt (heutiger Open vs. aktueller Close). Kein Look-Ahead --
    der Tages-Open einer Bar ist immer der Open der ERSTEN Kerze desselben
    Kalendertags, die entweder vor dieser Bar liegt oder diese Bar selbst ist.

    Rueckgabe: pd.Series (gleicher Index wie df) mit BULLISH/BEARISH pro Bar.
    Leere Series wenn df leer ist.
    """
    if df.empty:
        return pd.Series(dtype=object)
    day_open = df.groupby(df.index.date)['open'].transform('first')
    bias = np.where(df['close'] >= day_open, DailyBias.BULLISH, DailyBias.BEARISH)
    return pd.Series(bias, index=df.index)


def get_current_daily_bias(df: pd.DataFrame):
    """Live-Variante: Bias der aktuellen, noch laufenden Tageskerze (letzte
    Bar von df). None wenn df leer."""
    if df.empty:
        return None
    return compute_daily_bias_series(df).iloc[-1]


def daily_bias_blocks(bias, direction: str) -> bool:
    """True wenn `direction` (LONG/SHORT) dem aktuellen Tagestrend-Bias
    widerspricht und deshalb blockiert werden soll."""
    if bias is None:
        return False
    if bias == DailyBias.BEARISH and direction.upper() == 'LONG':
        return True
    if bias == DailyBias.BULLISH and direction.upper() == 'SHORT':
        return True
    return False
