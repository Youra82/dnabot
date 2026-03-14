# src/dnabot/analysis/interactive_chart.py
# Interaktiver Candlestick-Chart mit Genome-Trade-Signalen
#
# Zeigt:
#   - OHLCV-Candlesticks + Regime-Hintergrund (TREND/RANGE/HIGH_VOL/NEUTRAL)
#   - Entry-Marker (▲ LONG grün / ▼ SHORT orange)
#   - Exit-Marker  (● WIN cyan / ✗ LOSS rot / ■ TIMEOUT grau)
#   - SL- und TP-Linien pro Trade
#   - Equity-Kurve (rechte Y-Achse)
#   - Genome-Sequenz als Hover-Text
#
# Bot-spezifische Panels:
#   - Volume
#   - ATR + ATR-MA (Volatilitätsspikes als HIGH_VOL erkennbar)
#   - ADX + Trend/Range-Schwellen
#   - Genome Score (Signalqualität pro Entry)
#   - Body/ATR Ratio (Encoder-Perspektive)
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
    print("  Verfügbare Pairs:  (PnL = gespeicherter Backtest, voller Zeitraum)")
    print("=" * w)
    for i, (sym, tf) in enumerate(pairs, 1):
        pnl = pnl_map.get((sym, tf))
        pnl_str = f"  [+{pnl:.1f}%]" if pnl and pnl > 0 else (f"  [{pnl:.1f}%]" if pnl is not None else "")
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
# Indikator-Berechnung (dnabot-spezifisch)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_dna_panels(df: pd.DataFrame) -> tuple:
    """
    Berechnet alle dnabot-spezifischen Panel-Indikatoren:
      - ATR (14) und ATR-MA (50)
      - ADX (14)
      - Body/ATR Ratio (was der Encoder sieht)
      - Per-Kerzen-Regime (TREND / RANGE / HIGH_VOL / NEUTRAL)

    Gibt zurück: (atr, atr_ma, adx, body_ratio, regimes)
    """
    import ta

    atr = ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'],
        window=14, fillna=True,
    ).average_true_range()

    atr_ma = atr.rolling(window=50, min_periods=10).mean().fillna(atr)
    atr_ratio = (atr / atr_ma.replace(0, float('nan'))).fillna(1.0)

    adx = ta.trend.ADXIndicator(
        high=df['high'], low=df['low'], close=df['close'],
        window=14, fillna=True,
    ).adx()

    body = abs(df['close'] - df['open'])
    body_ratio = (body / atr.replace(0, float('nan'))).fillna(0.0).clip(upper=3.0)

    # Per-Kerzen-Regime
    regimes = []
    for i in range(len(df)):
        ar  = float(atr_ratio.iloc[i])
        adx_v = float(adx.iloc[i])
        if ar >= 1.5:
            regimes.append('HIGH_VOL')
        elif adx_v >= 25.0:
            regimes.append('TREND')
        elif adx_v <= 20.0:
            regimes.append('RANGE')
        else:
            regimes.append('NEUTRAL')

    return atr, atr_ma, adx, body_ratio, regimes


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
    risk_pct: float = 1.0,
    rr_ratio: float = 2.0,
) -> object | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        logger.error("plotly nicht installiert. Bitte: pip install plotly")
        return None

    # ── Indikatoren berechnen ────────────────────────────────────────────────
    atr, atr_ma, adx, body_ratio, regimes = _compute_dna_panels(df)

    # ── Subplots: 6 Panels ───────────────────────────────────────────────────
    fig = make_subplots(
        rows=6, cols=1,
        shared_xaxes=True,
        specs=[
            [{'secondary_y': True}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
        ],
        vertical_spacing=0.022,
        row_heights=[0.38, 0.10, 0.13, 0.13, 0.13, 0.13],
        subplot_titles=[
            '',
            'Volumen',
            'ATR  |  Volatilitäts-Ratio',
            'ADX  (Trendstärke)',
            'Genome Score  (Signalqualität)',
            'Body/ATR Ratio  (Encoder-Perspektive)',
        ],
    )

    # ── Regime-Hintergrund (Panel 1: Candlestick) ────────────────────────────
    _regime_fill = {
        'TREND':    'rgba(38,166,154,0.28)',
        'RANGE':    'rgba(255,167,38,0.22)',
        'HIGH_VOL': 'rgba(239,83,80,0.30)',
        'NEUTRAL':  None,
    }
    prev_reg, blk_start = None, None
    for ts_idx, reg in zip(df.index, regimes):
        if reg != prev_reg:
            if prev_reg and _regime_fill.get(prev_reg) and blk_start is not None:
                fig.add_vrect(
                    x0=blk_start, x1=ts_idx,
                    fillcolor=_regime_fill[prev_reg],
                    layer='below', line_width=0, row=1, col=1,
                )
            blk_start, prev_reg = ts_idx, reg
    if prev_reg and _regime_fill.get(prev_reg) and blk_start is not None:
        fig.add_vrect(
            x0=blk_start, x1=df.index[-1],
            fillcolor=_regime_fill[prev_reg],
            layer='below', line_width=0, row=1, col=1,
        )

    # ── Panel 1: Candlesticks ────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['open'], high=df['high'],
        low=df['low'],   close=df['close'],
        name='OHLC',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        showlegend=True,
    ), row=1, col=1, secondary_y=False)

    # ── Trade-Marker & SL/TP-Linien ─────────────────────────────────────────
    entry_long_x,  entry_long_y,  entry_long_txt  = [], [], []
    entry_short_x, entry_short_y, entry_short_txt = [], [], []
    exit_win_x,    exit_win_y    = [], []
    exit_loss_x,   exit_loss_y   = [], []
    exit_to_x,     exit_to_y     = [], []

    for t in trades:
        et  = pd.to_datetime(t['entry_time'])
        xt  = pd.to_datetime(t['exit_time'])
        seq = t.get('genome_id', '')[:8]
        wr  = f"{t.get('genome_winrate', 0):.1%}"
        sc  = f"{t.get('genome_score', 0):.3f}"
        tip = (
            f"Seq: {seq}<br>Score: {sc} | WR: {wr}<br>"
            f"SL: {t['sl_price']:.4f} | TP: {t['tp_price']:.4f}"
        )

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
            type='line', x0=et, x1=xt,
            y0=t['sl_price'], y1=t['sl_price'],
            line=dict(color='rgba(239,68,68,0.45)', width=1, dash='dot'),
        )
        # TP-Linie
        fig.add_shape(
            type='line', x0=et, x1=xt,
            y0=t['tp_price'], y1=t['tp_price'],
            line=dict(color='rgba(34,197,94,0.45)', width=1, dash='dot'),
        )

    if entry_long_x:
        fig.add_trace(go.Scatter(
            x=entry_long_x, y=entry_long_y, mode='markers',
            marker=dict(color='#26a69a', symbol='triangle-up', size=14,
                        line=dict(width=1, color='#ffffff')),
            name='Entry Long',
            text=entry_long_txt,
            hovertemplate='%{text}<extra>Entry Long</extra>',
        ), row=1, col=1, secondary_y=False)

    if entry_short_x:
        fig.add_trace(go.Scatter(
            x=entry_short_x, y=entry_short_y, mode='markers',
            marker=dict(color='#ffa726', symbol='triangle-down', size=14,
                        line=dict(width=1, color='#ffffff')),
            name='Entry Short',
            text=entry_short_txt,
            hovertemplate='%{text}<extra>Entry Short</extra>',
        ), row=1, col=1, secondary_y=False)

    if exit_win_x:
        fig.add_trace(go.Scatter(
            x=exit_win_x, y=exit_win_y, mode='markers',
            marker=dict(color='#00bcd4', symbol='circle', size=11,
                        line=dict(width=1, color='#ffffff')),
            name='Exit TP ✓',
        ), row=1, col=1, secondary_y=False)

    if exit_loss_x:
        fig.add_trace(go.Scatter(
            x=exit_loss_x, y=exit_loss_y, mode='markers',
            marker=dict(color='#ef5350', symbol='x', size=11,
                        line=dict(width=2, color='#ef5350')),
            name='Exit SL ✗',
        ), row=1, col=1, secondary_y=False)

    if exit_to_x:
        fig.add_trace(go.Scatter(
            x=exit_to_x, y=exit_to_y, mode='markers',
            marker=dict(color='#9e9e9e', symbol='square', size=9),
            name='Exit Timeout',
        ), row=1, col=1, secondary_y=False)

    # Dummy-Traces für Regime-Legende
    for label, color in [
        ('Trend',    '#26a69a'),
        ('Range',    '#ffa726'),
        ('High Vol', '#ef5350'),
    ]:
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode='markers',
            marker=dict(symbol='square', size=10, color=color),
            name=label, showlegend=True,
        ), row=1, col=1, secondary_y=False)

    # ── Equity-Kurve (rechte Y-Achse) ───────────────────────────────────────
    sorted_trades = sorted(trades, key=lambda t: str(t.get('entry_time', '')))
    equity   = start_capital
    peak     = equity
    chart_dd = 0.0
    eq_times = [df.index[0]]
    eq_vals  = [start_capital]
    wins_vis = 0

    for t in sorted_trades:
        risk_amount = equity * (risk_pct / 100.0)
        outcome     = t.get('outcome', 'LOSS')

        if outcome == 'WIN':
            equity += risk_amount * rr_ratio
            wins_vis += 1
        elif outcome == 'LOSS':
            equity -= risk_amount
        else:  # TIMEOUT
            sl_pct_t = max(t.get('sl_pct', 1.0), 0.01)
            equity  += risk_amount * (t.get('pnl_pct', 0.0) / sl_pct_t)

        if equity > peak:
            peak = equity
        if peak > 0:
            dd_now = (peak - equity) / peak * 100.0
            if dd_now > chart_dd:
                chart_dd = dd_now

        eq_times.append(pd.to_datetime(t['entry_time']))
        eq_vals.append(equity)

    if len(eq_vals) > 1:
        fig.add_trace(go.Scatter(
            x=eq_times, y=eq_vals,
            name='Equity',
            line=dict(color='#5c9bd6', width=1.5),
            hovertemplate='Equity: %{y:.2f} USDT<extra></extra>',
        ), row=1, col=1, secondary_y=True)

    # ── Panel 2: Volumen ─────────────────────────────────────────────────────
    if 'volume' in df.columns:
        vol_colors = ['#26a69a' if c >= o else '#ef5350'
                      for c, o in zip(df['close'], df['open'])]
        fig.add_trace(go.Bar(
            x=df.index, y=df['volume'],
            marker_color=vol_colors,
            name='Volumen', showlegend=False, opacity=0.65,
            hovertemplate='Vol: %{y:,.0f}<extra></extra>',
        ), row=2, col=1)

    # ── Panel 3: ATR + ATR-MA ────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=atr_ma,
        mode='lines', line=dict(color='rgba(255,167,38,0.5)', width=1.2, dash='dot'),
        name='ATR-MA(50)', showlegend=False,
        hovertemplate='ATR-MA: %{y:.4f}<extra></extra>',
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=df.index, y=atr,
        mode='lines', line=dict(color='#42a5f5', width=1.5),
        fill='tonexty', fillcolor='rgba(66,165,245,0.08)',
        name='ATR(14)', showlegend=False,
        hovertemplate='ATR: %{y:.4f}<extra></extra>',
    ), row=3, col=1)
    # HIGH_VOL-Marker auf ATR-Panel (wo ATR/ATR-MA >= 1.5)
    hv_mask = [r == 'HIGH_VOL' for r in regimes]
    hv_times = df.index[hv_mask]
    hv_atr   = atr[hv_mask]
    if len(hv_times) > 0:
        fig.add_trace(go.Scatter(
            x=hv_times, y=hv_atr,
            mode='markers',
            marker=dict(symbol='circle', size=5, color='#ef5350', opacity=0.7),
            showlegend=False,
            hovertemplate='HIGH_VOL<extra></extra>',
        ), row=3, col=1)
    # Signal-Punkte auf ATR
    if trades:
        sig_times = [pd.to_datetime(t['entry_time']) for t in trades]
        sig_atr = []
        for ts in sig_times:
            try:
                sig_atr.append(float(atr.asof(ts)))
            except Exception:
                sig_atr.append(float(atr.mean()))
        fig.add_trace(go.Scatter(
            x=sig_times, y=sig_atr, mode='markers',
            marker=dict(symbol='circle-open', size=9, color='#42a5f5',
                        line=dict(width=2)),
            showlegend=False,
            hovertemplate='Signal<br>%{x}<extra></extra>',
        ), row=3, col=1)

    # ── Panel 4: ADX + Schwellen ─────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=adx,
        mode='lines', line=dict(color='#ce93d8', width=1.5),
        fill='tozeroy', fillcolor='rgba(206,147,216,0.08)',
        name='ADX(14)', showlegend=False,
        hovertemplate='ADX: %{y:.2f}<extra></extra>',
    ), row=4, col=1)
    # Trend-Schwelle (25)
    fig.add_hline(y=25.0, line_dash='dot', line_color='rgba(38,166,154,0.55)',
                  row=4, col=1)
    # Range-Schwelle (20)
    fig.add_hline(y=20.0, line_dash='dot', line_color='rgba(255,167,38,0.55)',
                  row=4, col=1)
    # Signal-Punkte auf ADX
    if trades:
        sig_adx = []
        for ts in sig_times:
            try:
                sig_adx.append(float(adx.asof(ts)))
            except Exception:
                sig_adx.append(float(adx.mean()))
        fig.add_trace(go.Scatter(
            x=sig_times, y=sig_adx, mode='markers',
            marker=dict(symbol='circle-open', size=9, color='#ce93d8',
                        line=dict(width=2)),
            showlegend=False,
            hovertemplate='Signal<br>ADX: %{y:.2f}<extra></extra>',
        ), row=4, col=1)

    # ── Panel 5: Genome Score ────────────────────────────────────────────────
    if trades:
        score_times  = [pd.to_datetime(t['entry_time']) for t in trades]
        score_vals   = [t.get('genome_score', 0.0) for t in trades]
        score_colors = ['#26a69a' if t['direction'] == 'LONG' else '#ffa726'
                        for t in trades]
        outcome_txt  = [
            f"Score: {t.get('genome_score', 0):.4f}<br>"
            f"WR: {t.get('genome_winrate', 0):.1%}<br>"
            f"Dir: {t['direction']} | {t['outcome']}"
            for t in trades
        ]
        fig.add_trace(go.Bar(
            x=score_times, y=score_vals,
            marker_color=score_colors,
            opacity=0.75,
            name='Genome Score', showlegend=False,
            text=outcome_txt,
            hovertemplate='%{text}<extra></extra>',
        ), row=5, col=1)
        # Referenzlinie: Mindest-Score (Mittelwert)
        if score_vals:
            fig.add_hline(y=float(pd.Series(score_vals).mean()),
                          line_dash='dot',
                          line_color='rgba(255,255,255,0.3)',
                          row=5, col=1)
    else:
        # Leeres Panel mit Hinweis
        fig.add_trace(go.Scatter(
            x=df.index, y=[0] * len(df),
            mode='lines', line=dict(color='rgba(0,0,0,0)'),
            showlegend=False,
        ), row=5, col=1)

    # ── Panel 6: Body/ATR Ratio ──────────────────────────────────────────────
    body_colors = ['#26a69a' if c >= o else '#ef5350'
                   for c, o in zip(df['close'], df['open'])]
    fig.add_trace(go.Bar(
        x=df.index, y=body_ratio,
        marker_color=body_colors,
        opacity=0.65,
        name='Body/ATR', showlegend=False,
        hovertemplate='Body/ATR: %{y:.3f}<extra></extra>',
    ), row=6, col=1)
    # Schwellen: klein (<0.30), mittel (<0.80), groß (>0.80)
    for lvl, col in [(0.30, 'rgba(255,167,38,0.4)'), (0.80, 'rgba(38,166,154,0.4)')]:
        fig.add_hline(y=lvl, line_dash='dot', line_color=col, row=6, col=1)

    # ── Stats aus sichtbaren Trades ──────────────────────────────────────────
    n       = len(sorted_trades)
    wr      = wins_vis / n if n > 0 else 0.0
    pnl_pct = (equity - start_capital) / start_capital * 100.0 if start_capital > 0 else 0.0

    title = (
        f"{symbol} {timeframe} — dnabot Genome | "
        f"Trades: {n} | WR: {wr:.1%} | "
        f"PnL: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% | "
        f"MaxDD: {chart_dd:.1f}%"
    )

    # ── Layout ───────────────────────────────────────────────────────────────
    fig.update_layout(
        title=dict(text=title, font=dict(size=13), x=0.5, xanchor='center'),
        height=1150,
        hovermode='x unified',
        template='plotly_dark',
        dragmode='zoom',
        xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', yanchor='bottom', y=1.01,
                    xanchor='center', x=0.5, font=dict(size=11)),
        margin=dict(l=60, r=70, t=80, b=40),
        barmode='overlay',
        yaxis2=dict(title='Equity (USDT)', showgrid=False,
                    tickfont=dict(color='#5c9bd6'),
                    title_font=dict(color='#5c9bd6')),
    )

    fig.update_yaxes(title_text='Preis',     row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text='Vol',       row=2, col=1)
    fig.update_yaxes(title_text='ATR',       row=3, col=1)
    fig.update_yaxes(title_text='ADX',       row=4, col=1, tickformat='.1f')
    fig.update_yaxes(title_text='Score',     row=5, col=1, tickformat='.4f')
    fig.update_yaxes(title_text='B/ATR',     row=6, col=1, tickformat='.2f')

    # X-Achsen: Rangeslider nur auf unterster deaktivieren
    for row in range(1, 7):
        fig.update_xaxes(rangeslider_visible=False, row=row, col=1)

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

    risk_raw = input("Risiko pro Trade in % [Standard: 1.0]: ").strip()
    try:
        chart_risk_pct = float(risk_raw) if risk_raw else None
    except ValueError:
        chart_risk_pct = None

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
        effective_risk = chart_risk_pct if chart_risk_pct is not None else risk_cfg.get('risk_per_entry_pct', 1.0)
        results = run_backtest(
            df=df, market=symbol, timeframe=timeframe, db=db,
            params=params, start_capital=start_capital,
            risk_per_trade_pct=effective_risk,
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
        fig = create_chart(
            symbol, timeframe, df_chart, trades_chart, stats, start_capital,
            risk_pct=effective_risk,
            rr_ratio=risk_cfg.get('rr_ratio', 2.0),
        )
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
