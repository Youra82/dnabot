"""analysis/momentum_exit_utils.py — Gemeinsame Hilfsfunktionen fuer die
momentum_exit-Analysen (Wiederbelebung von analysis/utils.py, Genome-System,
beim Cleanup 2026-08-24 entfernt)."""

import os
import sys
import json
from datetime import datetime, timezone

PROJECT_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
DOCS_DIR      = os.path.join(PROJECT_ROOT, 'docs')
TMP_DIR       = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')

MAX_NOTIONAL_USDT = 200_000.0
FEE_PCT_PER_SIDE  = 0.06   # Bitget-Taker

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
B  = '\033[1;37m'
NC = '\033[0m'

COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed',
          '#0891b2', '#db2777', '#059669', '#ea580c', '#8b5cf6']


def _clean_timeframe(tf: str) -> str:
    return tf[:-len('_momentum_exit')] if tf.endswith('_momentum_exit') else tf


def load_active_pairs():
    """Gibt die aktiven momentum_exit (market, timeframe)-Paare aus settings.json zurueck."""
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            s = json.load(f)
        strats = s.get('live_trading_settings', {}).get('active_strategies', [])
        return {(st['symbol'], st['timeframe']) for st in strats
                if st.get('active', True) and st.get('strategy_type') == 'momentum_exit'}
    except Exception:
        return set()


def load_trades(only_active=False):
    """Laedt alle backtest_*_momentum_exit.json-Ergebnisse. only_active=True
    filtert auf die aktuell in active_strategies konfigurierten Paare."""
    active_pairs = load_active_pairs() if only_active else set()

    results = []
    if not os.path.isdir(RESULTS_DIR):
        return results
    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('_momentum_exit.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname), encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue

        market = data['market']
        tf     = _clean_timeframe(data['timeframe'])
        if only_active and (market, tf) not in active_pairs:
            continue

        parsed = []
        for t in data.get('trades', []):
            try:
                dt = datetime.fromisoformat(str(t.get('entry_time', '')))
                t = dict(t)
                t['entry_dt']  = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                t['market']    = market
                t['timeframe'] = tf
                t['coin']      = market.split('/')[0].upper()
                parsed.append(t)
            except Exception:
                continue
        if parsed:
            results.append({'market': market, 'timeframe': tf,
                             'coin': market.split('/')[0].upper(), 'trades': parsed})
    return results


def all_trades_flat(pair_results=None, only_active=False):
    """Gibt alle Trades als flache Liste zurueck, chronologisch sortiert."""
    if pair_results is None:
        pair_results = load_trades(only_active=only_active)
    trades = [t for r in pair_results for t in r['trades']]
    trades.sort(key=lambda t: t.get('entry_dt', datetime.min.replace(tzinfo=timezone.utc)))
    return trades


def load_settings():
    with open(SETTINGS_PATH, encoding='utf-8') as f:
        return json.load(f)


def get_telegram():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), encoding='utf-8') as f:
            s = json.load(f)
        acc     = s.get('dnabot', [{}])[0]
        token   = acc.get('telegram_bot_token', '') or s.get('telegram', {}).get('bot_token', '')
        chat_id = acc.get('telegram_chat_id', '')   or s.get('telegram', {}).get('chat_id', '')
        return (token, chat_id) if token and chat_id else (None, None)
    except Exception:
        return None, None


def send_photo(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                          data={'chat_id': chat_id, 'caption': caption},
                          files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def style_axes(*axes):
    """Einheitliches Dark-Theme fuer alle Charts."""
    for ax in axes:
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.title.set_color('white')


def save_send(fig, name, caption='', no_telegram=False):
    """Speichert Chart lokal (artifacts/tmp) + in docs/ und sendet via Telegram."""
    import matplotlib.pyplot as plt
    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f'dnabot_momentum_exit_{name}.png')
    docs = os.path.join(DOCS_DIR, f'momentum_exit_{name}_latest.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    fig.savefig(docs, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close(fig)
    print(f"  {G}✓ Chart: {path}{NC}")
    if not no_telegram:
        token, chat_id = get_telegram()
        if token:
            send_photo(token, chat_id, path, caption)
            print(f"  {G}✓ Via Telegram gesendet.{NC}")
        else:
            print(f"  {Y}Telegram nicht konfiguriert.{NC}")
    return path


def simulate(trades, capital, risk_pct, leverage=1, fee_pct=FEE_PCT_PER_SIDE):
    """Vollstaendige Portfolio-Simulation (gemeinsamer Kapital-Pool,
    kompoundiert, gebuehrenbewusst -- identisches Modell wie run_portfolio_
    optimizer_momentum_exit.py::simulate_portfolio())."""
    equity = capital
    peak   = equity
    max_dd = 0.0
    wins   = 0
    for t in trades:
        sl_pct       = max(t.get('sl_pct', 1.0), 0.01)
        leverage_cap = equity * max(leverage, 1) * (sl_pct / 100.0)
        risk_amount  = min(equity * (risk_pct / 100.0), leverage_cap, MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome      = t.get('outcome', 'LOSS')
        if outcome == 'WIN':
            wins += 1
        if outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        if fee_pct:
            position_size = risk_amount / (sl_pct / 100.0)
            pnl -= position_size * (fee_pct / 100.0) * 2.0
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    n = len(trades)
    pnl_pct = (equity - capital) / capital * 100.0 if capital > 0 else 0.0
    wr = wins / n if n > 0 else 0.0
    calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
    return {'equity': equity, 'pnl_pct': pnl_pct, 'max_dd': max_dd,
            'calmar': calmar, 'wr': wr, 'n': n}
