# tests/test_workflow.py
import pytest
import os
import sys
import json
import logging
import time
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.utils.trade_manager import place_entry_orders, cancel_entry_orders


@pytest.fixture(scope='module')
def test_setup():
    print('\n--- Starte umfassenden LIVE dnabot-Workflow-Test (PEPE) ---')
    print('\n[Setup] Bereite Testumgebung vor...')

    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        pytest.skip('secret.json nicht gefunden. Ueberspringe Live-Workflow-Test.')

    with open(secret_path, 'r') as f:
        secrets = json.load(f)

    if not secrets.get('dnabot'):
        pytest.skip("Es wird mindestens ein Account unter 'dnabot' in secret.json benoetigt.")

    try:
        exchange = Exchange(secrets['dnabot'][0])
        if not exchange.markets:
            pytest.fail('Exchange konnte nicht initialisiert werden (Maerkte nicht geladen).')
    except Exception as e:
        pytest.fail(f'Exchange konnte nicht initialisiert werden: {e}')

    symbol    = 'PEPE/USDT:USDT'
    timeframe = '4h'

    params = {
        'market': {'symbol': symbol, 'timeframe': timeframe},
        'risk': {
            'leverage':           5,
            'margin_mode':        'isolated',
            'risk_per_entry_pct': 0.1,   # SEHR KLEINES Risiko fuer den Test!
            'rr_ratio':           2.0,
        },
        'behavior': {'use_longs': True, 'use_shorts': True},
    }

    telegram_config = secrets.get('telegram', {})

    test_logger = logging.getLogger('test-dnabot')
    test_logger.setLevel(logging.INFO)
    if not test_logger.handlers:
        test_logger.addHandler(logging.StreamHandler(sys.stdout))

    print(f'-> Fuehre initiales Aufraeumen fuer {symbol} durch...')
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(2)
        positions = exchange.fetch_open_positions(symbol)
        if positions:
            pos  = positions[0]
            side = 'sell' if pos['side'] == 'long' else 'buy'
            amt  = float(pos.get('contracts') or pos.get('contractSize', 0))
            if amt > 0:
                exchange.place_market_order(symbol, side, amt, reduce=True)
                time.sleep(3)
        print('-> Ausgangszustand ist sauber.')
    except Exception as e:
        pytest.fail(f'Fehler beim initialen Aufraeumen: {e}')

    yield exchange, params, telegram_config, symbol, test_logger

    print('\n[Teardown] Raeume nach dem Test auf...')
    try:
        print('-> 1. Loesche offene Trigger Orders...')
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(2)

        print('-> 2. Pruefe auf offene Positionen...')
        positions = exchange.fetch_open_positions(symbol)
        if positions:
            print('-> Position nach Test noch offen. Schliesse sie...')
            pos  = positions[0]
            side = 'sell' if pos['side'] == 'long' else 'buy'
            amt  = float(pos.get('contracts') or pos.get('contractSize', 0))
            exchange.place_market_order(symbol, side, amt, reduce=True)
            time.sleep(3)
        else:
            print('-> Keine offene Position gefunden.')

        print('-> 3. Loesche verbleibende Trigger Orders (Sicherheitsnetz)...')
        exchange.cancel_all_orders_for_symbol(symbol)
        print('-> Aufraeumen abgeschlossen.')
    except Exception as e:
        print(f'FEHLER beim Aufraeumen nach dem Test: {e}')


def test_full_dnabot_workflow_on_bitget(test_setup):
    exchange, params, telegram_config, symbol, logger = test_setup

    bal = exchange.fetch_balance_usdt()
    print(f'\n--- Verfuegbares Guthaben fuer Test: {bal:.4f} USDT ---')

    if bal < 5.0:
        pytest.skip(f'Zu wenig Guthaben ({bal:.2f} USDT < 5 USDT) fuer Live-Test.')

    simulated_balance = 50.0  # Fixer Testwert – unabhaengig vom echten Kontostand

    # Aktuellen Preis holen und daraus ein realistisches Genome-Signal bauen
    ticker      = exchange.exchange.fetch_ticker(symbol)
    price       = float(ticker['last'])
    sl_pct      = 0.8   # Klein genug damit Notional > 5 USDT (50 * 0.1% / 0.8% = 6.25 USDT)
    sl_price    = price * (1 - sl_pct / 100)
    tp_price    = price * (1 + sl_pct * 2 / 100)  # 2:1 R:R

    mock_signal = {
        'side':               'long',
        'entry_price':        price,
        'sl_price':           sl_price,
        'tp_price':           tp_price,
        'sl_pct':             sl_pct,
        'genome_id':          'TEST-GENOME',
        'sequence':           'B3H-UH-D2L-BH',
        'score':              0.15,
        'winrate':            0.55,
        'total_occurrences':  42,
        'seq_length':         4,
        'avg_move_pct':       1.2,
        'regime':             'TREND',
    }

    tracker_path = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker', 'test_PEPEUSDTUSDT_4h.json')
    os.makedirs(os.path.dirname(tracker_path), exist_ok=True)

    # Sicherstellen dass isolated gesetzt ist BEVOR place_entry_orders aufgerufen wird
    print(f'-> Setze Margin-Modus: isolated | Leverage: 5x')
    exchange.set_margin_mode(symbol, 'isolated')
    time.sleep(0.5)
    exchange.set_leverage(symbol, 5, 'isolated')
    time.sleep(0.5)

    print(f'\n[Schritt 1/3] Mocke Genome-Signal und oeffne Position...')
    print(f'-> Signal: LONG PEPE @ {price:.8f} | SL={sl_price:.8f} | TP={tp_price:.8f}')

    with patch('dnabot.utils.trade_manager.should_skip_trading', return_value=(False, '')):
        place_entry_orders(
            exchange=exchange,
            genome_signal=mock_signal,
            params=params,
            balance=simulated_balance,
            tracker_path=tracker_path,
            telegram_config=telegram_config,
            logger=logger,
        )

    print('-> Warte 5s auf Order-Ausfuehrung...')
    time.sleep(5)

    print('\n[Schritt 2/3] Ueberpruefe Position und Orders...')
    positions = exchange.fetch_open_positions(symbol)

    if not positions:
        pytest.fail(f'FEHLER: Position nicht eroeffnet. Guthaben war {bal:.2f} USDT.')

    assert len(positions) == 1
    pos_info = positions[0]
    print(f'-> Position erfolgreich eroeffnet: {pos_info["side"].upper()} {pos_info.get("contracts")} PEPE')

    trigger_orders = exchange.fetch_open_trigger_orders(symbol)
    if len(trigger_orders) == 0:
        print('WARNUNG: Keine Trigger-Orders im API-Return gefunden (kann bei PEPE vorkommen).')
    else:
        print(f'-> Trigger-Orders gefunden: {len(trigger_orders)}')

    print('\n[Schritt 3/3] Schliesse die Position und raeume auf...')

    print('-> Loesche Trigger-Orders VOR dem Schliessen...')
    exchange.cancel_all_orders_for_symbol(symbol)
    time.sleep(3)

    amt          = abs(float(pos_info.get('contracts') or pos_info.get('contractSize', 0)))
    side_to_close = 'sell' if pos_info.get('side', '').lower() == 'long' else 'buy'

    if amt > 0:
        print(f'-> Schliesse Position ({amt} PEPE)...')
        close_order = exchange.place_market_order(symbol, side_to_close, amt, reduce=True)
        assert close_order, 'FEHLER: Konnte Position nicht schliessen!'
        print('-> Position erfolgreich geschlossen.')
        time.sleep(4)

    print('-> Loesche verbleibende Trigger-Orders NACH dem Schliessen...')
    exchange.cancel_all_orders_for_symbol(symbol)
    time.sleep(2)

    # Finale Pruefung
    final_positions = exchange.fetch_open_positions(symbol)
    final_orders    = exchange.fetch_open_trigger_orders(symbol)

    if len(final_orders) > 0:
        print(f'WARNUNG: Noch {len(final_orders)} Trigger-Orders offen. Versuche erneutes Loeschen...')
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(2)
        final_orders = exchange.fetch_open_trigger_orders(symbol)

    assert len(final_positions) == 0, 'FEHLER: Position sollte geschlossen sein.'
    assert len(final_orders) == 0,    f'FEHLER: Trigger-Orders nicht sauber geloescht! ({len(final_orders)} verbleibend)'

    print('\n--- UMFASSENDER WORKFLOW-TEST ERFOLGREICH! ---')
