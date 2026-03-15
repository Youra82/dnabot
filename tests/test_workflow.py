# tests/test_workflow.py
"""
dnabot Live-Workflow-Test

Testet den kompletten Handelszyklus auf Bitget mit PEPE (kleines Minimum).
Benoetigt secret.json mit gueltigen API-Keys.
"""

import pytest
import os
import sys
import json
import logging
import time
import tempfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.exchange import Exchange
from dnabot.utils.trade_manager import (
    get_tracker_file_path,
    read_tracker,
    record_trade_result,
    should_skip_trading,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope='module')
def test_setup():
    print('\n--- dnabot Live-Workflow-Test ---')

    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        pytest.skip('secret.json nicht gefunden. Ueberspringe Live-Test.')

    with open(secret_path, 'r') as f:
        secrets = json.load(f)

    accounts = secrets.get('dnabot', [])
    if not accounts:
        pytest.skip("Keine 'dnabot'-Accounts in secret.json. Ueberspringe Live-Test.")

    try:
        exchange = Exchange(accounts[0])
        if not exchange.markets:
            pytest.fail('Exchange konnte nicht initialisiert werden.')
    except Exception as e:
        pytest.fail(f'Exchange-Fehler: {e}')

    test_logger = logging.getLogger('test-dnabot')
    test_logger.setLevel(logging.INFO)
    if not test_logger.handlers:
        test_logger.addHandler(logging.StreamHandler(sys.stdout))

    symbol      = 'PEPE/USDT:USDT'
    timeframe   = '4h'
    leverage    = 5
    margin_mode = 'isolated'

    # Ausgangszustand bereinigen
    print(f'[Setup] Bereinige Ausgangszustand fuer {symbol}...')
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(1)
        positions = exchange.fetch_open_positions(symbol)
        if positions:
            pos  = positions[0]
            side = 'sell' if pos['side'] == 'long' else 'buy'
            amt  = float(pos.get('contracts') or pos.get('contractSize', 0))
            if amt > 0:
                exchange.place_market_order(symbol, side, amt, reduce=True)
                time.sleep(3)
        print('[Setup] Ausgangszustand ist sauber.')
    except Exception as e:
        pytest.fail(f'Fehler beim Setup-Bereinigen: {e}')

    yield exchange, symbol, timeframe, leverage, margin_mode, test_logger

    # Teardown
    print('\n[Teardown] Raeume nach dem Test auf...')
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
        exchange.cancel_all_orders_for_symbol(symbol)
        print('[Teardown] Abgeschlossen.')
    except Exception as e:
        print(f'FEHLER beim Teardown: {e}')


# ============================================================
# Unit Tests (kein API-Zugriff noetig)
# ============================================================

def test_risk_based_contracts_calculation():
    """Prueft risiko-basierte Positionsgroesse: risk_amount / SL-Abstand"""
    balance      = 1000.0
    risk_pct     = 1.0          # 1% Risiko
    entry_price  = 0.000012     # PEPE Beispielpreis
    sl_pct       = 2.0          # SL 2% unter Entry

    sl_price       = entry_price * (1 - sl_pct / 100)
    sl_distance    = entry_price - sl_price
    risk_amount    = balance * (risk_pct / 100)
    contracts      = risk_amount / sl_distance

    expected = (balance * risk_pct / 100) / (entry_price * sl_pct / 100)
    assert abs(contracts - expected) < 1e-2, f'Erwartet ~{expected:.2f}, got {contracts:.2f}'

    notional = contracts * entry_price
    max_loss = abs(contracts * sl_distance)
    assert abs(max_loss - risk_amount) < 0.01, f'Max-Verlust {max_loss:.4f} != Risikobetrag {risk_amount:.4f}'

    print(f'Risk-Sizing korrekt: {contracts:.0f} PEPE | Notional: {notional:.2f} USDT | Max-Verlust: {max_loss:.4f} USDT')


def test_tracker_read_write():
    """Prueft Tracker-Datei Lesen/Schreiben/Standardwerte"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker_path = os.path.join(tmpdir, 'test_tracker.json')

        # Lesen ohne Datei → Standardwerte
        import dnabot.utils.trade_manager as tm
        orig_dir = tm.TRACKER_DIR
        tm.TRACKER_DIR = tmpdir

        tracker = read_tracker(tracker_path)
        assert tracker.get('status') == 'idle'
        assert tracker.get('consecutive_losses', 0) == 0

        # Gewinn aufzeichnen
        record_trade_result(tracker_path, 'win', logging.getLogger('test'))
        tracker = read_tracker(tracker_path)
        assert tracker.get('total_wins', 0) == 1
        assert tracker.get('consecutive_losses', 0) == 0

        # Verlust aufzeichnen
        record_trade_result(tracker_path, 'loss', logging.getLogger('test'))
        tracker = read_tracker(tracker_path)
        assert tracker.get('total_losses', 0) == 1
        assert tracker.get('consecutive_losses', 0) == 1

        tm.TRACKER_DIR = orig_dir

    print('Tracker Lesen/Schreiben/Aufzeichnen: OK')


def test_should_skip_trading():
    """Prueft Trading-Pause bei zu vielen Verlusten"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker_path = os.path.join(tmpdir, 'skip_test.json')

        import dnabot.utils.trade_manager as tm
        orig_dir = tm.TRACKER_DIR
        tm.TRACKER_DIR = tmpdir

        logger = logging.getLogger('test')

        # Normal: kein Skip
        skip, reason = should_skip_trading(tracker_path)
        assert not skip, f'Sollte nicht skippen, aber: {reason}'

        # 5 Verluste in Folge → Skip
        for _ in range(5):
            record_trade_result(tracker_path, 'loss', logger)

        skip, reason = should_skip_trading(tracker_path)
        assert skip, 'Sollte nach 5 Verlusten in Folge skippen'
        print(f'Skip-Logik korrekt: {reason}')

        tm.TRACKER_DIR = orig_dir


# ============================================================
# Live Test (erfordert secret.json)
# ============================================================

def test_full_dnabot_workflow_on_bitget(test_setup):
    """Vollstaendiger Live-Test: Market-Entry + SL/TP + Schliessen auf Bitget (PEPE)"""
    exchange, symbol, timeframe, leverage, margin_mode, logger = test_setup

    bal = exchange.fetch_balance_usdt()
    print(f'\nVerfuegbares Guthaben: {bal:.4f} USDT')

    if bal < 5.0:
        pytest.skip(f'Zu wenig Guthaben ({bal:.2f} USDT < 5 USDT) fuer Live-Test.')

    # --- Margin + Hebel setzen ---
    exchange.set_margin_mode(symbol, margin_mode)
    time.sleep(0.3)
    exchange.set_leverage(symbol, leverage, margin_mode)
    time.sleep(0.3)

    # --- Preis und Positionsgroesse ---
    min_amount = exchange.fetch_min_amount_tradable(symbol)
    ticker     = exchange.exchange.fetch_ticker(symbol)
    price      = float(ticker['last'])

    # Risk-basierte Sizing: 5% Risiko mit 5% SL (damit Notional gross genug)
    risk_pct      = 5.0
    sl_pct_price  = 5.0
    sl_price      = price * (1 - sl_pct_price / 100)
    tp_price      = price * (1 + sl_pct_price * 2 / 100)  # 2:1 R:R
    sl_distance   = price - sl_price
    risk_amount   = bal * (risk_pct / 100)
    contracts     = max(risk_amount / sl_distance, min_amount)
    notional      = contracts * price

    print(f'[Schritt 1/3] Entry: LONG {contracts:.0f} PEPE @ ~{price:.8f} | Notional: {notional:.2f} USDT')
    print(f'              SL={sl_price:.8f} | TP={tp_price:.8f} | Risiko: {risk_pct}%')

    if notional < 5.0:
        pytest.skip(f'Notional {notional:.2f} USDT zu klein (< 5 USDT). Mehr Kapital benoetigt.')

    # --- Entry Market-Order (wie dnabot nach Umbau) ---
    try:
        entry_order = exchange.place_market_order(symbol, 'buy', contracts)
    except Exception as e:
        pytest.fail(f'Entry fehlgeschlagen: {e}')

    entry_price = float(entry_order.get('average') or entry_order.get('price') or price)
    filled      = float(entry_order.get('filled')  or entry_order.get('amount') or contracts)
    print(f'Entry ausgefuehrt: {filled:.0f} PEPE @ {entry_price:.8f}')
    time.sleep(2)

    # SL/TP anhand tatsaechlichem Entry-Preis neu berechnen
    sl_price = entry_price * (1 - sl_pct_price / 100)
    tp_price = entry_price * (1 + sl_pct_price * 2 / 100)

    # --- SL + TP setzen ---
    print(f'[Schritt 2/3] SL={sl_price:.8f} | TP={tp_price:.8f}')
    try:
        exchange.place_trigger_market_order(symbol, 'sell', filled, sl_price, reduce=True)
        time.sleep(0.5)
        exchange.place_trigger_market_order(symbol, 'sell', filled, tp_price, reduce=True)
    except Exception as e:
        pytest.fail(f'SL/TP-Platzierung fehlgeschlagen: {e}')

    time.sleep(3)

    # --- Position pruefen ---
    positions = exchange.fetch_open_positions(symbol)
    assert positions, f'Position nicht gefunden nach Entry (Guthaben war {bal:.2f} USDT)'
    print(f'Position offen: {positions[0]["side"].upper()} | Kontrakte: {positions[0].get("contracts")}')

    # --- Position sauber schliessen ---
    print('[Schritt 3/3] Schliesse Position...')
    exchange.cancel_all_orders_for_symbol(symbol)
    time.sleep(2)

    pos       = positions[0]
    amt       = abs(float(pos.get('contracts') or pos.get('contractSize', 0)))
    close_ord = exchange.place_market_order(symbol, 'sell', amt, reduce=True)
    assert close_ord, 'Schliessen fehlgeschlagen!'
    time.sleep(4)

    exchange.cancel_all_orders_for_symbol(symbol)
    time.sleep(2)

    # --- Finale Checks ---
    final_pos    = exchange.fetch_open_positions(symbol)
    final_orders = exchange.exchange.fetch_open_orders(
        symbol, params={'stop': True, 'productType': 'USDT-FUTURES'}
    )

    assert len(final_pos) == 0,    f'Position sollte geschlossen sein, aber noch offen: {len(final_pos)}'
    assert len(final_orders) == 0, f'Trigger-Orders sollten leer sein: {len(final_orders)}'

    print('\n--- WORKFLOW-TEST ERFOLGREICH ---')
