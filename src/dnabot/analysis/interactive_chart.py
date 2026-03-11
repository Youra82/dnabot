# src/dnabot/analysis/interactive_chart.py
# Interaktiver Candlestick-Chart mit Genome-Trade-Signalen
#
# Zeigt:
#   - OHLCV-Candlesticks
#   - Entry-Marker (▲ LONG grün / ▼ SHORT orange)
#   - Exit-Marker  (● WIN cyan / ◆ LOSS rot / ■ TIMEOUT grau)
#   - SL- und TP-Linien pro Trade
#   - Equity-Kurve (rechte Y-Achse)
#   - Genome-Sequenz als Hover-Text
#
# Output: HTML-Datei in /tmp/ (öffnet im Browser)

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
sys.path.append(PROJECT_ROOT)

from dnabot.genome.database import GenomeDB
from dnabot.analysis.backtester import run_backtest

logger = logging.getLogger(__name__)

DB_PATH     = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')


# ─────────────────────────────────────────────────────────────────────────────
# Symbol-Auswahl
# ─────────────────────────────────────────────────────────────────────────────

def _load_backtest_pnl() -> dict:
    """Lädt PnL% aus gespeicherten Backtest-JSONs. Key: (market, timeframe)."""
    pnl_map = {}
    if not os.path.isdir(RESULTS_DIR):
        return pnl_map
    for fname in os.listdir(RESULTS_DIR):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                d = json.load(f)
            key = (d['market'], d['timeframe'])
            pnl_map[key] = d.get('stats', {}).get('total_pnl_pct', None)
        except Exception:
            pass
    return pnl_map


def select_pairs() -> list[tuple[str, str]]:
    """
    Zeigt alle Pairs aus den Backtest-Ergebnissen mit PnL%.
    Erlaubt Einzel- und Mehrfach-Auswahl (z.B. '1' oder '1,3,5').
    """
    pnl_map = _load_backtest_pnl()
    pairs = sorted(pnl_map.keys(), key=lambda x: (x[0], x[1]))

    if not pairs:
        print("Keine Backtest-Ergebnisse gefunden. Zuerst Mode 1 ausführen.")
        return []

    w = 70
    print("\n" + "=" * w)
    print("  Verfügbare Pairs:")
    print("=" * w)
    for i, (sym, tf) in enumerate(pairs, 1):
        pnl = pnl_map.get((sym, tf))
        pnl_str = f"  [+{pnl:.1f}%]" if pnl and pnl > 0 else (f"  [{pnl:.1f}%]" if pnl else "")
        safe = sym.replace('/', '').replace(':', '')
        print(f"  {i:2d}) {safe}_{tf}{pnl_str}")
    print("=" * w)

    print("\n  Wähle Pair(s):")
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    raw = input("\n  Auswahl: ").strip()

    selected = []
    for token in raw.replace(',', ' ').split():
        try:
            idx = int(token)
            if 1 <= idx <= len(pairs):
                if pairs[idx - 1] not in selected:
                    selected.append(pairs[idx - 1])
        except ValueError:
            pass

    if not selected:
        print("Ungültige Auswahl.")
    return selected


# ─────────────────────────────────────────────────────────────────────────────
# Chart-Erstellung
# ─────────────────────────────────────────────────────────────────────────────

def create_chart(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    trades: list[dict],
    stats: dict,
    start_capital: float,
) -> object | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        logger.error("plotly nicht installiert. Bitte: pip install plotly")
        return None

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # ── Candlesticks ────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['open'], high=df['high'],
        low=df['low'], close=df['close'],
        name='OHLC',
        increasing_line_color='#16a34a',
        decreasing_line_color='#dc2626',
    ), secondary_y=False)

    # ── Trade-Marker & SL/TP-Linien ─────────────────────────────────────────
    entry_long_x,  entry_long_y,  entry_long_txt  = [], [], []
    entry_short_x, entry_short_y, entry_short_txt = [], [], []
    exit_win_x,    exit_win_y    = [], []
    exit_loss_x,   exit_loss_y   = [], []
    exit_to_x,     exit_to_y     = [], []

    for t in trades:
        et = pd.to_datetime(t['entry_time'])
        xt = pd.to_datetime(t['exit_time'])
        seq  = t.get('genome_id', '')[:8]
        wr   = f"{t.get('genome_winrate', 0):.1%}"
        sc   = f"{t.get('genome_score', 0):.3f}"
        tip  = f"Seq: {seq}<br>Score: {sc} | WR: {wr}<br>SL: {t['sl_price']:.4f} | TP: {t['tp_price']:.4f}"

        if t['direction'] == 'LONG':
            entry_long_x.append(et)
            entry_long_y.append(t['entry_price'])
            entry_long_txt.append(tip)
        else:
            entry_short_x.append(et)
            entry_short_y.append(t['entry_price'])
            entry_short_txt.append(tip)

        if t['outcome'] == 'WIN':
            exit_win_x.append(xt);  exit_win_y.append(t['exit_price'])
        elif t['outcome'] == 'LOSS':
            exit_loss_x.append(xt); exit_loss_y.append(t['exit_price'])
        else:
            exit_to_x.append(xt);   exit_to_y.append(t['exit_price'])

        # SL-Linie
        fig.add_shape(
            type='line',
            x0=et, x1=xt,
            y0=t['sl_price'], y1=t['sl_price'],
            line=dict(color='rgba(239,68,68,0.5)', width=1, dash='dot'),
        )
        # TP-Linie
        fig.add_shape(
            type='line',
            x0=et, x1=xt,
            y0=t['tp_price'], y1=t['tp_price'],
            line=dict(color='rgba(34,197,94,0.5)', width=1, dash='dot'),
        )

    # Entry Long
    if entry_long_x:
        fig.add_trace(go.Scatter(
            x=entry_long_x, y=entry_long_y, mode='markers',
            marker=dict(color='#16a34a', symbol='triangle-up', size=14,
                        line=dict(width=1, color='#0f5132')),
            name='Entry Long', text=entry_long_txt, hovertemplate='%{text}<extra>Entry Long</extra>',
        ), secondary_y=False)

    # Entry Short
    if entry_short_x:
        fig.add_trace(go.Scatter(
            x=entry_short_x, y=entry_short_y, mode='markers',
            marker=dict(color='#f59e0b', symbol='triangle-down', size=14,
                        line=dict(width=1, color='#92400e')),
            name='Entry Short', text=entry_short_txt, hovertemplate='%{text}<extra>Entry Short</extra>',
        ), secondary_y=False)

    # Exit WIN
    if exit_win_x:
        fig.add_trace(go.Scatter(
            x=exit_win_x, y=exit_win_y, mode='markers',
            marker=dict(color='#22d3ee', symbol='circle', size=11,
                        line=dict(width=1, color='#0e7490')),
            name='Exit TP ✓',
        ), secondary_y=False)

    # Exit LOSS
    if exit_loss_x:
        fig.add_trace(go.Scatter(
            x=exit_loss_x, y=exit_loss_y, mode='markers',
            marker=dict(color='#ef4444', symbol='x', size=11,
                        line=dict(width=2, color='#7f1d1d')),
            name='Exit SL ✗',
        ), secondary_y=False)

    # Exit TIMEOUT
    if exit_to_x:
        fig.add_trace(go.Scatter(
            x=exit_to_x, y=exit_to_y, mode='markers',
            marker=dict(color='#9ca3af', symbol='square', size=9),
            name='Exit Timeout',
        ), secondary_y=False)

    # ── Equity-Kurve ────────────────────────────────────────────────────────
    if trades:
        eq_times = [pd.to_datetime(t['entry_time']) for t in trades]
        eq_vals  = []
        equity   = start_capital
        for t in trades:
            sl_pct = t.get('sl_pct', 0)
            if sl_pct > 0:
                risk_amt = equity * 0.01  # 1% Risiko
                pos_size = risk_amt / (sl_pct / 100)
                equity  += pos_size * (t['pnl_pct'] / 100)
            eq_vals.append(equity)

        fig.add_trace(go.Scatter(
            x=eq_times, y=eq_vals,
            name='Equity',
            line=dict(color='#2563eb', width=2),
            opacity=0.75,
        ), secondary_y=True)

    # ── Layout ──────────────────────────────────────────────────────────────
    pnl_pct = stats.get('total_pnl_pct', 0)
    wr      = stats.get('win_rate', 0)
    dd      = stats.get('max_drawdown_pct', 0)
    n       = stats.get('total_trades', 0)

    title = (
        f"{symbol} {timeframe} — dnabot Genome | "
        f"Trades: {n} | WR: {wr:.1%} | "
        f"PnL: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% | "
        f"MaxDD: {dd:.1f}%"
    )

    fig.update_layout(
        title=dict(text=title, font=dict(size=13), x=0.5, xanchor='center'),
        height=750,
        hovermode='x unified',
        template='plotly_white',
        dragmode='zoom',
        xaxis=dict(rangeslider=dict(visible=True), fixedrange=False),
        yaxis=dict(fixedrange=False),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='center', x=0.5),
    )
    fig.update_yaxes(title_text='Preis (USDT)', secondary_y=False)
    fig.update_yaxes(title_text='Equity (USDT)', secondary_y=True)

    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run_interactive_chart(settings: dict, secrets: dict):
    from dnabot.utils.exchange import Exchange
    from scan_and_learn import resolve_history_days

    print("\n" + "=" * 60)
    print("  INTERAKTIVE CHARTS")
    print("=" * 60)

    # Pairs auswählen (aus Backtest-Ergebnissen, mit PnL%)
    selected_pairs = select_pairs()
    if not selected_pairs:
        return

    # Chart-Optionen
    print()
    start_raw = input("Startdatum (JJJJ-MM-TT) [leer=beliebig]: ").strip()
    end_raw   = input("Enddatum   (JJJJ-MM-TT) [leer=heute]: ").strip()

    cap_raw = input("Startkapital in USDT [Standard: 1000]: ").strip()
    start_capital = float(cap_raw) if cap_raw.replace('.', '').isdigit() else 1000.0

    tg_raw = input("Per Telegram senden? (j/n) [Standard: n]: ").strip().lower()
    send_tg = tg_raw in ('j', 'y', 'yes')

    # Exchange
    accounts = secrets.get('dnabot', [])
    if not accounts:
        print("Kein 'dnabot'-Account in secret.json.")
        return
    exchange = Exchange(accounts[0])

    scan_cfg   = settings.get('scan_settings', {})
    genome_cfg = settings.get('genome_settings', {})
    risk_cfg   = settings.get('risk_settings', {})
    params = {
        'genome': {
            'min_score':        genome_cfg.get('min_score', 0.08),
            'min_winrate':      genome_cfg.get('min_winrate', 0.45),
            'sequence_lengths': genome_cfg.get('sequence_lengths', [4, 5, 6]),
        },
        'risk': {'rr_ratio': risk_cfg.get('rr_ratio', 2.0)},
    }

    generated = []

    for symbol, timeframe in selected_pairs:
        print(f"\n--- {symbol} ({timeframe}) ---")
        history_days = resolve_history_days(timeframe, scan_cfg.get('history_days'))

        print(f"  Lade {history_days} Tage History...")
        fetch_end   = datetime.now(timezone.utc)
        fetch_start = fetch_end - timedelta(days=history_days)
        df = exchange.fetch_historical_ohlcv(
            symbol, timeframe,
            fetch_start.strftime('%Y-%m-%d'),
            fetch_end.strftime('%Y-%m-%d'),
        )
        if df is None or df.empty:
            print(f"  Keine Daten — übersprungen.")
            continue
        print(f"  {len(df)} Kerzen geladen.")

        # Backtest auf vollem DataFrame
        db = GenomeDB(DB_PATH)
        print("  Führe Backtest durch...")
        results = run_backtest(
            df=df, market=symbol, timeframe=timeframe, db=db,
            params=params, start_capital=start_capital,
            risk_per_trade_pct=risk_cfg.get('risk_per_entry_pct', 1.0),
        )
        db.close()

        trades = results.get('trades', [])
        stats  = results.get('stats', {})
        print(f"  {stats.get('total_trades', 0)} Trades | "
              f"WR: {stats.get('win_rate', 0):.1%} | "
              f"PnL: {stats.get('total_pnl_pct', 0):+.1f}%")

        # Datum-Filter auf Trades und DataFrame
        df_chart     = df.copy()
        trades_chart = trades
        if start_raw:
            try:
                sd = pd.Timestamp(start_raw, tz='UTC')
                df_chart     = df_chart[df_chart.index >= sd]
                trades_chart = [t for t in trades_chart
                                if str(t.get('entry_time', '')) >= start_raw]
            except Exception:
                pass
        if end_raw:
            try:
                ed = pd.Timestamp(end_raw + ' 23:59:59', tz='UTC')
                df_chart     = df_chart[df_chart.index <= ed]
                trades_chart = [t for t in trades_chart
                                if str(t.get('entry_time', '')) <= end_raw + ' 23:59:59']
            except Exception:
                pass

        print("  Erstelle Chart...")
        fig = create_chart(symbol, timeframe, df_chart, trades_chart, stats, start_capital)
        if fig is None:
            continue

        safe_name   = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
        output_file = f"/tmp/dnabot_{safe_name}.html"
        fig.write_html(output_file)
        print(f"  ✅ Chart gespeichert: {output_file}")
        generated.append((symbol, timeframe, output_file))

    print(f"\n✅ {len(generated)} Chart(s) generiert!")

    # Telegram
    if send_tg and generated:
        tg = secrets.get('telegram', {})
        if tg.get('bot_token') and tg.get('chat_id'):
            try:
                from dnabot.utils.telegram import send_document
                for sym, tf, path in generated:
                    send_document(tg['bot_token'], tg['chat_id'], path,
                                  caption=f"dnabot Chart: {sym} {tf}")
                    print(f"  ✅ Telegram: {sym} {tf} gesendet.")
            except Exception as e:
                print(f"  Telegram-Fehler: {e}")
        else:
            print("  Telegram nicht konfiguriert (bot_token/chat_id fehlt).")
