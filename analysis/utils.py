"""analysis/utils.py — Gemeinsame Hilfsfunktionen für alle Analysen."""

import os, sys, json
from datetime import datetime, timezone

PROJECT_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
DB_PATH       = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
DOCS_DIR      = os.path.join(PROJECT_ROOT, 'docs')

RR_RATIO          = 2.0
MAX_NOTIONAL_USDT = 200_000.0

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
NC = '\033[0m'


def load_active_pairs():
    """Gibt die aktiven (market, timeframe) Paare aus settings.json zurück."""
    try:
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
        strats = s.get('live_trading_settings', {}).get('active_strategies', [])
        return {(st['symbol'], st['timeframe']) for st in strats if st.get('active', True)}
    except Exception:
        return set()


def load_trades():
    """Lädt Trades aus artifacts/results/*.json — gefiltert auf active_strategies."""
    active_pairs = load_active_pairs()

    results = []
    if not os.path.isdir(RESULTS_DIR):
        return results
    for fname in sorted(os.listdir(RESULTS_DIR)):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                data = json.load(f)
        except Exception:
            continue

        if active_pairs and (data['market'], data['timeframe']) not in active_pairs:
            continue

        parsed = []
        for t in data.get('trades', []):
            try:
                dt = datetime.fromisoformat(str(t.get('entry_time', '')))
                t['entry_dt'] = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                t['market']    = data['market']
                t['timeframe'] = data['timeframe']
                t['coin']      = data['market'].split('/')[0].upper()
                parsed.append(t)
            except Exception:
                continue
        if parsed:
            results.append({'market': data['market'], 'timeframe': data['timeframe'],
                             'coin': data['market'].split('/')[0].upper(), 'trades': parsed})
    return results


def all_trades_flat(pair_results=None):
    """Gibt alle Trades als flache Liste zurück, chronologisch sortiert."""
    if pair_results is None:
        pair_results = load_trades()
    trades = [t for r in pair_results for t in r['trades']]
    trades.sort(key=lambda t: t.get('entry_dt', datetime.min.replace(tzinfo=timezone.utc)))
    return trades


def load_genome_db():
    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))
    from dnabot.genome.database import GenomeDB
    return GenomeDB(DB_PATH)


def load_settings():
    with open(SETTINGS_PATH) as f:
        return json.load(f)


def get_telegram():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
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
    """Einheitliches Dark-Theme für alle Charts."""
    for ax in axes:
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.title.set_color('white')


def save_send(fig, name, caption='', no_telegram=False):
    """Speichert Chart lokal + in docs/ und sendet via Telegram."""
    import matplotlib.pyplot as plt
    path = f'/tmp/dnabot_{name}.png'
    docs = os.path.join(DOCS_DIR, f'{name}_latest.png')
    os.makedirs(DOCS_DIR, exist_ok=True)
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


def simulate(trades, capital, risk_pct, rr=None, leverage=1):
    """Vollständige Portfolio-Simulation."""
    if rr is None:
        rr = RR_RATIO
    equity = capital
    peak   = equity
    max_dd = 0.0
    wins   = 0
    for t in trades:
        sl_pct       = max(t.get('sl_pct', 1.0), 0.01)
        leverage_cap = equity * max(leverage, 1) * (sl_pct / 100.0)
        risk_amount  = min(equity * (risk_pct / 100.0), leverage_cap, MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome     = t.get('outcome', 'LOSS')
        if outcome == 'WIN':
            pnl = risk_amount * rr; wins += 1
        elif outcome == 'LOSS':
            pnl = -risk_amount
        else:
            pnl = risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        equity += pnl
        if equity > peak: peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd: max_dd = dd
    n = len(trades)
    pnl_pct = (equity - capital) / capital * 100.0 if capital > 0 else 0.0
    wr = wins / n if n > 0 else 0.0
    calmar = pnl_pct / max_dd if max_dd > 0 else pnl_pct
    return {'equity': equity, 'pnl_pct': pnl_pct, 'max_dd': max_dd,
            'calmar': calmar, 'wr': wr, 'n': n}


COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed',
          '#0891b2', '#db2777', '#059669', '#ea580c', '#8b5cf6']
