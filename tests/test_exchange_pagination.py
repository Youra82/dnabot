# tests/test_exchange_pagination.py
# Regressionstest fuer den Vorwaerts-Paginierungs-Off-by-one in
# Exchange.fetch_historical_ohlcv() (Bitgets `since` ist exklusiv -- ein
# voller Timeframe-Schritt als naechster Cursor ueberspringt die
# unmittelbar folgende Kerze). fetch_recent_ohlcv() hatte denselben Bug
# bereits gefixt; dieser Test haelt den Fix fuer fetch_historical_ohlcv fest.
import os
import sys
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange


def _make_exchange_with_mock():
    ex = Exchange.__new__(Exchange)  # __init__ umgehen -- kein echter Netzwerk-Call
    ex.account = {}
    ex.markets = {'BTC/USDT:USDT': {}}
    ex.exchange = MagicMock()
    ex.exchange.rateLimit = 0
    return ex


def test_fetch_historical_ohlcv_advances_cursor_by_one_ms_not_full_timeframe():
    ex = _make_exchange_with_mock()
    ex.exchange.parse_timeframe.return_value = 60  # 1m in Sekunden
    ex.exchange.parse8601.side_effect = lambda s: {
        '2024-01-01T00:00:00Z': 0,
        '2024-01-01T23:59:59Z': 180_000,
    }[s]
    ex.exchange.milliseconds.return_value = 10 ** 15  # weit in der Zukunft -> "Gegenwart erreicht"-Zweig greift nicht

    page1 = [[0, 1, 1, 1, 1, 1], [60_000, 1, 1, 1, 1, 1]]
    page2 = [[120_000, 1, 1, 1, 1, 1], [180_000, 1, 1, 1, 1, 1]]
    ex.exchange.fetch_ohlcv.side_effect = [page1, page2]

    df = ex.fetch_historical_ohlcv('BTC/USDT:USDT', '1m', '2024-01-01', '2024-01-01')

    calls = ex.exchange.fetch_ohlcv.call_args_list
    assert len(calls) == 2
    # 2. Aufruf muss mit since=60001 (letzte Kerze + 1ms) erfolgen, nicht 120000
    # (letzte Kerze + tf_ms) -- sonst wird die Kerze bei ts=120000 uebersprungen.
    second_call_since = calls[1].args[2]
    assert second_call_since == 60_001

    # Keine Luecke: alle 4 Kerzen (0, 60000, 120000, 180000) muessen vorhanden sein.
    assert len(df) == 4
