"""Lokale Tests fuer die Teamhistorie (keine Bitget-Aufrufe)."""
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from dnabot.utils.team_history import load_history, record_team, strategy_key, team_at


def _t(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_strategy_key():
    assert strategy_key('ENA/USDT:USDT', '15m') == 'ENAUSDTUSDT_15m'


def test_records_only_changes_and_answers_point_in_time(tmp_path):
    path = str(tmp_path / 'team_history.jsonl')
    week1 = ['ENAUSDTUSDT_15m', 'ETHUSDTUSDT_4h']
    week2 = ['ENAUSDTUSDT_15m', 'AVAXUSDTUSDT_30m']
    assert record_team(week1, 'master_runner', path, now=_t('2026-10-14 22:30:00'))
    assert not record_team(list(reversed(week1)), 'master_runner', path, now=_t('2026-10-14 22:45:00'))
    assert record_team(week2, 'master_runner', path, now=_t('2026-10-19 15:15:00'))
    assert len(load_history(path)) == 2

    assert team_at('2026-10-14 22:00', path) is None
    assert set(team_at('2026-10-18 20:00', path)) == set(week1)
    assert set(team_at('2026-10-19 15:15', path)) == set(week2)
    assert set(team_at('2026-10-20 14:00+00:00', path)) == set(week2)


def test_settings_entry_change_creates_new_entry(tmp_path, monkeypatch):
    """dnabot: Pruefsumme = Strategie-Eintrag in settings.json (Overrides)."""
    import json
    import dnabot.utils.team_history as th
    settings = tmp_path / 'settings.json'
    entry = {'symbol': 'DOGE/USDT:USDT', 'timeframe': '6h', 'active': True,
             'risk_overrides': {'rr_ratio': 1.5}}
    settings.write_text(json.dumps({'live_trading_settings': {'active_strategies': [entry]}}), encoding='utf-8')
    monkeypatch.setattr(th, 'SETTINGS_FILE', str(settings))
    path = str(tmp_path / 'h.jsonl')
    assert record_team(['DOGEUSDTUSDT_6h'], 'master_runner', path)
    assert not record_team(['DOGEUSDTUSDT_6h'], 'master_runner', path)
    entry['risk_overrides']['rr_ratio'] = 2.0
    settings.write_text(json.dumps({'live_trading_settings': {'active_strategies': [entry]}}), encoding='utf-8')
    assert record_team(['DOGEUSDTUSDT_6h'], 'master_runner', path)


def test_weekly_selection_keeps_team_is_recorded(tmp_path):
    path = str(tmp_path / 'h.jsonl')
    team = ['ENAUSDTUSDT_15m']
    assert record_team(team, 'master_runner', path, now=_t('2026-10-19 15:15:00'))
    assert record_team(team, 'wochenauswahl: beibehalten', path, now=_t('2026-10-26 15:00:00'), force=True)
    assert not record_team(team, 'master_runner', path, now=_t('2026-10-26 15:15:00'))
    assert [e['source'] for e in load_history(path)] == ['master_runner', 'wochenauswahl: beibehalten']


def test_seed_from_settings_and_last_selection(tmp_path, monkeypatch):
    """Fehlt die Historie, gilt das aktuelle Team ab der letzten Wochenauswahl (Ortszeit-Datei)."""
    import json
    import dnabot.utils.team_history as th
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'live_trading_settings': {'active_strategies': [
        {'symbol': 'HBAR/USDT:USDT', 'timeframe': '4h', 'active': True},
        {'symbol': 'ADA/USDT:USDT', 'timeframe': '2h', 'active': True, 'manage_only': True},
        {'symbol': 'XVG/USDT:USDT', 'timeframe': '2h', 'active': False}]}}), encoding='utf-8')
    last = tmp_path / '.last_optimization_run'
    last.write_text('2026-10-18T15:30:00', encoding='utf-8')
    hist = str(tmp_path / 'db' / 'team_history.jsonl')
    monkeypatch.setattr(th, 'SETTINGS_FILE', str(settings))
    monkeypatch.setattr(th, 'LAST_SELECTION_FILE', str(last))
    monkeypatch.setattr(th, 'HISTORY_FILE', hist)
    h = th.load_history(hist)
    assert len(h) == 1 and list(h[0]['team']) == ['HBARUSDTUSDT_4h']
    expected = datetime.fromisoformat('2026-10-18T15:30:00').astimezone().astimezone(timezone.utc)
    assert pd.Timestamp(h[0]['valid_from']) == pd.Timestamp(expected)


def test_no_seed_without_last_selection(tmp_path, monkeypatch):
    import json
    import dnabot.utils.team_history as th
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'live_trading_settings': {'active_strategies': [
        {'symbol': 'HBAR/USDT:USDT', 'timeframe': '4h', 'active': True}]}}), encoding='utf-8')
    hist = str(tmp_path / 'db' / 'team_history.jsonl')
    monkeypatch.setattr(th, 'SETTINGS_FILE', str(settings))
    monkeypatch.setattr(th, 'LAST_SELECTION_FILE', str(tmp_path / 'fehlt'))
    monkeypatch.setattr(th, 'HISTORY_FILE', hist)
    assert th.load_history(hist) == []
