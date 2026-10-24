"""Lokale Tests fuer die Log-Auswertung im Tagescheck (keine Bitget-Aufrufe)."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import pandas as pd

import daily_live_vs_backtest_check as mod

LOCAL_TZ = datetime.now().astimezone().tzinfo
DECISION = pd.Timestamp('2026-10-22 12:00', tz='UTC')  # Kerzenschluss


def _lines(entries):
    out = []
    for minutes, level, msg in entries:
        ts = (DECISION + pd.Timedelta(minutes=minutes)).tz_convert(LOCAL_TZ)
        out.append(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},123 - {level} - {msg}\n")
    return ''.join(out)


def test_no_signal_and_side_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    (tmp_path / 'dnabot_DOGEUSDTUSDT_6h.log').write_text(_lines([
        (0.5, 'INFO', 'Lade 200 Kerzen für DOGE/USDT:USDT (6h)...'),
        (0.6, 'INFO', 'Kein Momentum-Exit-Signal (deaktiviert, zu wenig Kerzen, kein Muster).')]), encoding='utf-8')
    assert mod.explain_from_log('DOGEUSDTUSDT_6h', DECISION, 'long').startswith('Live-Code sah kein Signal')
    (tmp_path / 'dnabot_SOLUSDTUSDT_6h.log').write_text(_lines([(0.5, 'INFO', 'Shorts deaktiviert.')]),
                                                     encoding='utf-8')
    assert mod.explain_from_log('SOLUSDTUSDT_6h', DECISION, 'short').startswith('Shorts deaktiviert')


def test_exit_lock_and_min_notional(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    (tmp_path / 'dnabot_XRPUSDTUSDT_6h.log').write_text(_lines([
        (0.5, 'INFO', 'Signalkerze 2026-10-22 06:00 begann vor dem letzten Exit (2026-10-22 08:30) -- kein Entry.')]),
        encoding='utf-8')
    assert mod.explain_from_log('XRPUSDTUSDT_6h', DECISION).startswith('Signal vor dem letzten Exit')
    (tmp_path / 'dnabot_ADAUSDTUSDT_6h.log').write_text(_lines([
        (0.5, 'WARNING', 'Notional 3.10 USDT unter Minimum 5 USDT. Überspringe.')]), encoding='utf-8')
    assert mod.explain_from_log('ADAUSDTUSDT_6h', DECISION).startswith('Order unter Bitget-Minimum')


def test_bot_not_running_and_missing_log(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    (tmp_path / 'dnabot_LINKUSDTUSDT_6h.log').write_text(_lines([(-600, 'INFO', 'Kein Signal → kein Trade.')]),
                                                      encoding='utf-8')
    assert mod.explain_from_log('LINKUSDTUSDT_6h', DECISION).startswith('Bot lief in dem Zeitraum nicht')
    assert mod.explain_from_log('ETHUSDTUSDT_6h', DECISION) == 'kein Bot-Log vorhanden'


def test_active_periods_follow_team_history():
    history = [
        {'valid_from': '2026-10-14T13:00:00+00:00', 'team': {'XRPUSDTUSDT_6h': None, 'SOLUSDTUSDT_6h': None}},
        {'valid_from': '2026-10-21T13:00:00+00:00', 'team': {'XRPUSDTUSDT_6h': None}},
    ]
    ts = pd.Timestamp
    assert mod.active_periods(history, 'SOLUSDTUSDT_6h') == [
        (ts('2026-10-14T13:00:00+00:00'), ts('2026-10-21T13:00:00+00:00'))]


def test_position_without_stop_loss_is_reported():
    pos = [{'symbol': 'XRPUSDT', 'holdSide': 'short', 'markPrice': '1.2781'},
           {'symbol': 'ARBUSDT', 'holdSide': 'long', 'markPrice': '0.168'}]
    plans = [  # XRP: nur Trailing (unter dem Kurs, kein SL); ARB: SL an der Einstiegs-Order
        {'symbol': 'XRPUSDT', 'planType': 'track_plan', 'tradeSide': 'close', 'posSide': 'short', 'triggerPrice': '1.22'},
        {'symbol': 'ARBUSDT', 'planType': 'profit_loss', 'tradeSide': 'close', 'posSide': 'long',
         'triggerPrice': '0.108', 'stopLossTriggerPrice': '0.108'},
    ]
    flags = mod.positions_without_sl(pos, plans)
    assert len(flags) == 1 and 'XRP short OHNE Stop-Loss' in flags[0]
    plans.append({'symbol': 'XRPUSDT', 'planType': 'normal_plan', 'tradeSide': 'close', 'posSide': 'short',
                  'triggerPrice': '1.41'})
    assert mod.positions_without_sl(pos, plans) == []
