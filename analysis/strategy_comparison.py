#!/usr/bin/env python3
"""
analysis/strategy_comparison.py — Wöchentliche Re-Optimierung vs. Alle Configs

Vergleicht zwei Ansätze auf demselben Zeitraum (kein Lookahead):

  A) Wöchentliche Re-Optimierung (Walk-Forward):
     - Trainings-Fenster (lookback_weeks): Beste Pair-Kombination via Greedy-Calmar wählen
     - Test-Fenster (nächste lookback_weeks): Mit dem ausgewählten Portfolio traden
     - Equity akkumuliert rollend über die gesamte Periode

  B) Alle Configs dauerhaft (Baseline):
     - Alle vorhandenen Backtest-Ergebnisse, kein Filtering, kein Re-Opt
     - Gemeinsamer Kapital-Pool, chronologisch simuliert

Ausgabe:
  - PNG-Chart via Telegram (Equity-Kurven + Drawdown-Panels)
  - Interaktiver HTML-Chart via Telegram (Plotly)
  - Zusammenfassungs-Nachricht via Telegram

Ausführung:
  python3 analysis/strategy_comparison.py
  python3 analysis/strategy_comparison.py --capital 100 --risk 1.0 --max-dd 30 --lookback 2
  python3 analysis/strategy_comparison.py --no-telegram
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
DOCS_DIR      = os.path.join(PROJECT_ROOT, 'docs')

RR_RATIO          = 2.0
MAX_NOTIONAL_USDT = 200_000.0

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
B  = '\033[1;37m'
NC = '\033[0m'


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _get_telegram():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            s = json.load(f)
        acc   = s.get('dnabot', [{}])[0]
        token = acc.get('telegram_bot_token', '') or s.get('telegram', {}).get('bot_token', '')
        cid   = acc.get('telegram_chat_id', '')   or s.get('telegram', {}).get('chat_id', '')
        return (token, cid) if token and cid else (None, None)
    except Exception:
        return None, None


def _send_photo(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                          data={'chat_id': chat_id, 'caption': caption},
                          files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def _send_document(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendDocument',
                          data={'chat_id': chat_id, 'caption': caption},
                          files={'document': (os.path.basename(path), f)}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler (Dokument): {e}")


def _send_message(token, chat_id, text):
    try:
        import requests
        requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                      data={'chat_id': chat_id, 'text': text}, timeout=10)
    except Exception as e:
        print(f"  Telegram Fehler (Nachricht): {e}")


# ─── Daten laden ──────────────────────────────────────────────────────────────

def _parse_dt(ts):
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def load_all_results():
    """Lädt ALLE Backtest-Ergebnisse — kein active_strategies Filter."""
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
        parsed = []
        for t in data.get('trades', []):
            dt = _parse_dt(t.get('entry_time', ''))
            if dt:
                t['entry_dt'] = dt
                parsed.append(t)
        if parsed:
            results.append({
                'market':    data['market'],
                'timeframe': data['timeframe'],
                'coin':      data['market'].split('/')[0].upper(),
                'trades':    parsed,
            })
    return results


def _filter_window(pair_results, start_dt, end_dt):
    """Gibt pair_results mit Trades nur im Fenster [start_dt, end_dt) zurück."""
    out = []
    for r in pair_results:
        trades = [t for t in r['trades'] if start_dt <= t['entry_dt'] < end_dt]
        if trades:
            out.append({**r, 'trades': trades})
    return out


# ─── Simulation ───────────────────────────────────────────────────────────────

def _step_simulate(trades_sorted, equity_start, risk_pct):
    """
    Simuliert eine Trade-Liste step-by-step.
    Gibt (final_equity, max_dd, n_wins, equity_steps) zurück.
    equity_steps = [(entry_dt, equity_after_trade), ...]
    """
    equity = equity_start
    peak   = equity
    max_dd = 0.0
    wins   = 0
    steps  = []
    for t in trades_sorted:
        sl_pct      = max(t.get('sl_pct', 1.0), 0.01)
        risk_amount = min(equity * (risk_pct / 100.0), MAX_NOTIONAL_USDT * (sl_pct / 100.0))
        outcome     = t.get('outcome', 'LOSS')
        if outcome == 'WIN':
            equity += risk_amount * RR_RATIO
            wins   += 1
        elif outcome == 'LOSS':
            equity -= risk_amount
        else:
            equity += risk_amount * (t.get('pnl_pct', 0.0) / sl_pct)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
        steps.append((t['entry_dt'], round(equity, 4)))
    return equity, max_dd, wins, steps


def _calmar(pnl_pct, max_dd):
    return pnl_pct / max_dd if max_dd > 0 else pnl_pct


# ─── Portfolio-Auswahl (Greedy Calmar) ────────────────────────────────────────

def _select_greedy(pair_results, ref_capital, risk_pct, max_dd_limit, min_trades):
    """
    Greedy-Auswahl: Beste Pair-Kombination nach Calmar-Ratio.
    Constraint: Max. 1 Timeframe pro Coin.
    ref_capital: nur für normierte Calmar-Berechnung (immer 100 USDT).
    """
    # Einzel-Calmar berechnen
    candidates = []
    for r in pair_results:
        if len(r['trades']) < min_trades:
            continue
        eq, dd, wins, _ = _step_simulate(
            sorted(r['trades'], key=lambda t: t['entry_dt']), 100.0, risk_pct
        )
        pnl = (eq - 100.0) / 100.0 * 100.0
        if pnl <= 0:
            continue
        calmar = _calmar(pnl, dd)
        candidates.append({**r, '_calmar': calmar, '_pnl': pnl, '_dd': dd})

    if not candidates:
        return []

    # Max 1 TF pro Coin
    coin_best = {}
    for c in candidates:
        coin = c['coin']
        if coin not in coin_best or c['_calmar'] > coin_best[coin]['_calmar']:
            coin_best[coin] = c
    eligible = sorted(coin_best.values(), key=lambda x: x['_calmar'], reverse=True)

    # Greedy hinzufügen solange MaxDD ≤ Limit und Calmar steigt
    team = [eligible[0]]
    team_trades = sorted(
        [t for r in team for t in r['trades']], key=lambda t: t['entry_dt']
    )
    team_eq, team_dd, _, _ = _step_simulate(team_trades, ref_capital, risk_pct)
    if team_dd > max_dd_limit:
        return []
    team_pnl = (team_eq - ref_capital) / ref_capital * 100.0
    team_calmar = _calmar(team_pnl, team_dd)

    for cand in eligible[1:]:
        candidate_trades = sorted(
            [t for r in team + [cand] for t in r['trades']], key=lambda t: t['entry_dt']
        )
        new_eq, new_dd, _, _ = _step_simulate(candidate_trades, ref_capital, risk_pct)
        if new_dd > max_dd_limit:
            continue
        new_pnl = (new_eq - ref_capital) / ref_capital * 100.0
        new_calmar = _calmar(new_pnl, new_dd)
        if new_calmar > team_calmar:
            team.append(cand)
            team_dd = new_dd
            team_calmar = new_calmar

    return team


# ─── Walk-Forward Simulation ──────────────────────────────────────────────────

def simulate_walkforward(all_results, capital, risk_pct, max_dd, lookback_weeks, min_trades,
                          start_dt, end_dt):
    """
    Rolling Walk-Forward:
      - Trainings-Fenster: letzte lookback_weeks Wochen → Portfolio auswählen
      - Test-Fenster: nächste lookback_weeks Wochen → mit Portfolio traden

    Gibt zurück:
      curve: [(datetime, equity), ...] — vollständige Equity-Kurve
      windows: [{'train_start', 'train_end', 'test_end', 'selected', 'n_trades', 'pnl_pct', 'max_dd'}, ...]
    """
    window_delta = timedelta(weeks=lookback_weeks)

    # Fenster erzeugen
    windows = []
    t = start_dt
    while t + window_delta * 2 <= end_dt:
        windows.append((t, t + window_delta, t + window_delta * 2))
        t += window_delta

    if not windows:
        return [(start_dt, capital)], []

    curve   = [(start_dt, capital)]
    wstats  = []
    equity  = capital

    for train_start, train_end, test_end in windows:
        train_results = _filter_window(all_results, train_start, train_end)
        test_results  = _filter_window(all_results, train_end, test_end)

        # Portfolio auf Trainings-Daten wählen
        selected = _select_greedy(train_results, 100.0, risk_pct, max_dd, min_trades)
        selected_keys = {(r['market'], r['timeframe']) for r in selected}

        # Test-Trades nur für ausgewählte Pairs
        if selected_keys:
            test_pairs = [r for r in test_results if (r['market'], r['timeframe']) in selected_keys]
        else:
            test_pairs = []

        test_trades = sorted(
            [t for r in test_pairs for t in r['trades']],
            key=lambda t: t['entry_dt']
        )

        equity_before = equity
        equity, window_dd, wins, steps = _step_simulate(test_trades, equity, risk_pct)
        curve.extend(steps)

        pnl_pct = (equity - equity_before) / equity_before * 100.0 if equity_before > 0 else 0.0
        wstats.append({
            'train_start':  train_start,
            'train_end':    train_end,
            'test_end':     test_end,
            'selected':     [f"{r['market'].split('/')[0]}/{r['timeframe']}" for r in selected],
            'n_trades':     len(test_trades),
            'pnl_pct':      pnl_pct,
            'max_dd':       window_dd,
            'wins':         wins,
        })

    return curve, wstats


# ─── All-Configs Simulation ───────────────────────────────────────────────────

def simulate_all_configs(all_results, capital, risk_pct, start_dt, end_dt):
    """
    Alle verfügbaren Configs ohne Filterung — gemeinsamer Kapital-Pool.
    Gibt (curve, stats) zurück.
    """
    window_results = _filter_window(all_results, start_dt, end_dt)
    all_trades = sorted(
        [t for r in window_results for t in r['trades']],
        key=lambda t: t['entry_dt']
    )
    curve = [(start_dt, capital)]
    if not all_trades:
        return curve, {'pnl_pct': 0.0, 'max_dd': 0.0, 'wr': 0.0, 'n': 0, 'equity': capital}

    final_eq, max_dd, wins, steps = _step_simulate(all_trades, capital, risk_pct)
    curve.extend(steps)

    n       = len(all_trades)
    pnl_pct = (final_eq - capital) / capital * 100.0 if capital > 0 else 0.0
    return curve, {
        'pnl_pct': pnl_pct,
        'max_dd':  max_dd,
        'wr':      wins / n if n > 0 else 0.0,
        'n':       n,
        'equity':  final_eq,
    }


# ─── Drawdown-Kurve berechnen ─────────────────────────────────────────────────

def _drawdown_series(curve):
    """Berechnet die laufende Drawdown-Serie aus einer Equity-Kurve [(dt, eq), ...]."""
    peak = curve[0][1]
    dds  = []
    for dt, eq in curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        dds.append((dt, dd))
    return dds


# ─── Matplotlib Chart ─────────────────────────────────────────────────────────

def generate_png_chart(curve_wf, curve_all, wstats, stats_all,
                        capital, risk_pct, lookback_weeks, no_telegram):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.patches import Patch
    except ImportError:
        print(f"{R}  matplotlib nicht installiert — PNG übersprungen.{NC}")
        return None

    dd_wf  = _drawdown_series(curve_wf)
    dd_all = _drawdown_series(curve_all)

    times_wf  = [p[0] for p in curve_wf]
    eq_wf     = [p[1] for p in curve_wf]
    times_all = [p[0] for p in curve_all]
    eq_all    = [p[1] for p in curve_all]
    times_dd_wf  = [p[0] for p in dd_wf]
    dd_wf_vals   = [p[1] for p in dd_wf]
    times_dd_all = [p[0] for p in dd_all]
    dd_all_vals  = [p[1] for p in dd_all]

    pnl_wf  = (eq_wf[-1] - capital) / capital * 100.0 if capital > 0 else 0.0
    max_dd_wf = max(dd_wf_vals) if dd_wf_vals else 0.0

    n_wf    = sum(w['n_trades'] for w in wstats)
    wins_wf = sum(w['wins'] for w in wstats)
    wr_wf   = wins_wf / n_wf if n_wf > 0 else 0.0

    DARK_BG   = '#0f172a'
    PANEL_BG  = '#1e293b'
    COLOR_WF  = '#2563eb'   # blau
    COLOR_ALL = '#f97316'   # orange
    GRID_COL  = '#334155'
    TEXT_COL  = '#94a3b8'
    WHITE     = '#f1f5f9'

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True,
                                    gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.06})
    fig.patch.set_facecolor(DARK_BG)

    for ax in (ax1, ax2):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TEXT_COL, labelsize=9)
        ax.spines[:].set_color(GRID_COL)
        ax.grid(True, alpha=0.2, color=GRID_COL, linewidth=0.7)
        ax.xaxis.label.set_color(TEXT_COL)
        ax.yaxis.label.set_color(TEXT_COL)

    # ── Panel 1: Equity-Kurven ────────────────────────────────────────────────
    ax1.plot(times_all, eq_all, color=COLOR_ALL, linewidth=1.6, alpha=0.85,
             label=f'Alle Configs  |  PnL: {("+" if stats_all["pnl_pct"]>=0 else "")}{stats_all["pnl_pct"]:.1f}%  '
                   f'|  MaxDD: {stats_all["max_dd"]:.1f}%  |  WR: {stats_all["wr"]:.1%}  |  n={stats_all["n"]}')
    ax1.plot(times_wf, eq_wf, color=COLOR_WF, linewidth=2.2,
             label=f'WF Re-Opt ({lookback_weeks}w)  |  PnL: {("+" if pnl_wf>=0 else "")}{pnl_wf:.1f}%  '
                   f'|  MaxDD: {max_dd_wf:.1f}%  |  WR: {wr_wf:.1%}  |  n={n_wf}')
    ax1.axhline(y=capital, color='#475569', linewidth=0.9, linestyle='--', alpha=0.6)

    # Fenster-Trennlinien für Walk-Forward
    for w in wstats:
        ax1.axvline(x=w['train_end'], color='#1e40af', linewidth=0.6, alpha=0.35, linestyle=':')

    ax1.set_ylabel('Equity (USDT)', color=TEXT_COL, fontsize=10)
    ax1.legend(facecolor='#1e293b', edgecolor=GRID_COL, labelcolor=WHITE,
               fontsize=8.5, loc='upper left')
    ax1.yaxis.label.set_color(TEXT_COL)

    winner = 'WF Re-Opt' if pnl_wf > stats_all['pnl_pct'] else 'Alle Configs'
    winner_col = COLOR_WF if pnl_wf > stats_all['pnl_pct'] else COLOR_ALL
    title = (f'dnabot — Strategie-Vergleich  |  Kapital: {capital:.0f} USDT  |  '
             f'Risiko: {risk_pct}%/Trade  |  Besser: ')
    ax1.set_title(title, color=TEXT_COL, fontsize=11, pad=8)
    ax1.text(0.5, 1.01, title + winner, transform=ax1.transAxes,
             ha='center', va='bottom', fontsize=11, color=TEXT_COL)
    ax1.texts[-1].set_color(WHITE)
    ax1.title.set_visible(False)
    fig.suptitle(
        f'dnabot Strategie-Vergleich  —  WF Re-Opt ({lookback_weeks}w) vs. Alle Configs  '
        f'—  Gewinner: ',
        fontsize=12, color=TEXT_COL, y=0.98
    )
    # Gewinner farbig hervorheben — als zweites Text-Element
    fig.text(0.72, 0.978, winner, fontsize=12, color=winner_col, fontweight='bold')

    # ── Panel 2: Drawdown ────────────────────────────────────────────────────
    ax2.fill_between(times_dd_all, dd_all_vals, alpha=0.35, color=COLOR_ALL)
    ax2.fill_between(times_dd_wf,  dd_wf_vals,  alpha=0.45, color=COLOR_WF)
    ax2.plot(times_dd_all, dd_all_vals, color=COLOR_ALL, linewidth=1.2, alpha=0.7)
    ax2.plot(times_dd_wf,  dd_wf_vals,  color=COLOR_WF,  linewidth=1.5)
    ax2.invert_yaxis()
    ax2.set_ylabel('Drawdown %', color=TEXT_COL, fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right', fontsize=8)

    legend_patches = [
        Patch(color=COLOR_WF,  label=f'WF Re-Opt ({lookback_weeks}w)'),
        Patch(color=COLOR_ALL, label='Alle Configs'),
    ]
    ax2.legend(handles=legend_patches, facecolor='#1e293b', edgecolor=GRID_COL,
               labelcolor=WHITE, fontsize=8, loc='lower left')

    output_path = '/tmp/dnabot_strategy_comparison.png'
    docs_path   = os.path.join(DOCS_DIR, 'strategy_comparison_latest.png')
    os.makedirs(DOCS_DIR, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=DARK_BG)
    fig.savefig(docs_path,   dpi=150, bbox_inches='tight', facecolor=DARK_BG)
    plt.close(fig)
    print(f"  {G}✓ PNG-Chart: {output_path}{NC}")

    if not no_telegram:
        token, chat_id = _get_telegram()
        if token:
            sign_wf  = '+' if pnl_wf >= 0 else ''
            sign_all = '+' if stats_all['pnl_pct'] >= 0 else ''
            caption = (
                f"dnabot Strategie-Vergleich\n"
                f"WF Re-Opt ({lookback_weeks}w):  {sign_wf}{pnl_wf:.1f}% | "
                f"MaxDD: {max_dd_wf:.1f}% | WR: {wr_wf:.1%} | {n_wf} Trades\n"
                f"Alle Configs: {sign_all}{stats_all['pnl_pct']:.1f}% | "
                f"MaxDD: {stats_all['max_dd']:.1f}% | WR: {stats_all['wr']:.1%} | {stats_all['n']} Trades\n"
                f"Gewinner: {winner}"
            )
            _send_photo(token, chat_id, output_path, caption)
            print(f"  {G}✓ PNG via Telegram gesendet.{NC}")
        else:
            print(f"  {Y}Telegram nicht konfiguriert.{NC}")

    return output_path


# ─── Plotly HTML Chart ────────────────────────────────────────────────────────

def generate_html_chart(curve_wf, curve_all, wstats, stats_all,
                         capital, risk_pct, lookback_weeks, no_telegram):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print(f"{R}  plotly nicht installiert — HTML übersprungen.{NC}")
        return None

    dd_wf  = _drawdown_series(curve_wf)
    dd_all = _drawdown_series(curve_all)

    times_wf  = [p[0] for p in curve_wf]
    eq_wf     = [p[1] for p in curve_wf]
    times_all = [p[0] for p in curve_all]
    eq_all    = [p[1] for p in curve_all]

    pnl_wf    = (eq_wf[-1] - capital) / capital * 100.0 if capital > 0 else 0.0
    max_dd_wf = max(p[1] for p in dd_wf) if dd_wf else 0.0
    n_wf      = sum(w['n_trades'] for w in wstats)
    wins_wf   = sum(w['wins'] for w in wstats)
    wr_wf     = wins_wf / n_wf if n_wf > 0 else 0.0

    winner = 'WF Re-Opt' if pnl_wf > stats_all['pnl_pct'] else 'Alle Configs'
    sign_wf  = '+' if pnl_wf >= 0 else ''
    sign_all = '+' if stats_all['pnl_pct'] >= 0 else ''

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.7, 0.3], vertical_spacing=0.04,
        subplot_titles=['Equity-Verlauf', 'Drawdown (%)'],
    )

    # Alle Configs (orange)
    fig.add_trace(go.Scatter(
        x=times_all, y=eq_all,
        name=f'Alle Configs  {sign_all}{stats_all["pnl_pct"]:.1f}% | MaxDD {stats_all["max_dd"]:.1f}% | WR {stats_all["wr"]:.1%} | n={stats_all["n"]}',
        line=dict(color='#f97316', width=1.8), opacity=0.85,
    ), row=1, col=1)

    # WF Re-Opt (blau)
    fig.add_trace(go.Scatter(
        x=times_wf, y=eq_wf,
        name=f'WF Re-Opt ({lookback_weeks}w)  {sign_wf}{pnl_wf:.1f}% | MaxDD {max_dd_wf:.1f}% | WR {wr_wf:.1%} | n={n_wf}',
        line=dict(color='#2563eb', width=2.5),
    ), row=1, col=1)

    # Startkapital-Linie
    fig.add_hline(y=capital, line=dict(color='rgba(148,163,184,0.35)', width=1, dash='dash'),
                  row=1, col=1)

    # Fenster-Trennlinien (ohne annotation — crasht bei datetime-Achsen in älteren plotly-Versionen)
    for w in wstats:
        fig.add_vline(
            x=w['train_end'].isoformat(),
            line=dict(color='rgba(37,99,235,0.25)', width=1, dash='dot'),
            row=1, col=1,
        )

    # Drawdown
    fig.add_trace(go.Scatter(
        x=[p[0] for p in dd_all], y=[-p[1] for p in dd_all],
        name='DD Alle Configs',
        line=dict(color='#f97316', width=1.5), fill='tozeroy',
        fillcolor='rgba(249,115,22,0.15)', opacity=0.7,
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=[p[0] for p in dd_wf], y=[-p[1] for p in dd_wf],
        name=f'DD WF Re-Opt',
        line=dict(color='#2563eb', width=2), fill='tozeroy',
        fillcolor='rgba(37,99,235,0.2)',
    ), row=2, col=1)

    fig.update_layout(
        title=dict(
            text=(f'dnabot Strategie-Vergleich  —  Gewinner: <b>{winner}</b>  |  '
                  f'Kapital: {capital:.0f} USDT  |  Risiko: {risk_pct}%/Trade'),
            font=dict(size=14), x=0.5, xanchor='center',
        ),
        height=800, hovermode='x unified', template='plotly_dark',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='center', x=0.5, font=dict(size=9)),
        xaxis2=dict(rangeslider=dict(visible=True)),
    )
    fig.update_yaxes(title_text='Equity (USDT)', row=1, col=1)
    fig.update_yaxes(title_text='Drawdown %', row=2, col=1)

    output_path = '/tmp/dnabot_strategy_comparison.html'
    fig.write_html(output_path)
    print(f"  {G}✓ HTML-Chart: {output_path}{NC}")

    if not no_telegram:
        token, chat_id = _get_telegram()
        if token:
            caption = (
                f"dnabot Strategie-Vergleich (interaktiv)\n"
                f"Gewinner: {winner}\n"
                f"WF Re-Opt ({lookback_weeks}w): {sign_wf}{pnl_wf:.1f}% | MaxDD {max_dd_wf:.1f}%\n"
                f"Alle Configs: {sign_all}{stats_all['pnl_pct']:.1f}% | MaxDD {stats_all['max_dd']:.1f}%"
            )
            _send_document(token, chat_id, output_path, caption)
            print(f"  {G}✓ HTML via Telegram gesendet.{NC}")

    return output_path


# ─── Zusammenfassung ──────────────────────────────────────────────────────────

def print_summary(curve_wf, curve_all, wstats, stats_all,
                   capital, risk_pct, lookback_weeks, no_telegram):
    pnl_wf    = (curve_wf[-1][1] - capital) / capital * 100.0 if capital > 0 else 0.0
    max_dd_wf = max(p[1] for p in _drawdown_series(curve_wf)) if curve_wf else 0.0
    n_wf      = sum(w['n_trades'] for w in wstats)
    wins_wf   = sum(w['wins'] for w in wstats)
    wr_wf     = wins_wf / n_wf if n_wf > 0 else 0.0
    calmar_wf = _calmar(pnl_wf, max_dd_wf)

    calmar_all = _calmar(stats_all['pnl_pct'], stats_all['max_dd'])

    sign_wf  = '+' if pnl_wf >= 0 else ''
    sign_all = '+' if stats_all['pnl_pct'] >= 0 else ''
    col_wf   = G if pnl_wf >= 0 else R
    col_all  = G if stats_all['pnl_pct'] >= 0 else R

    w = 72
    print(f"\n{'=' * w}")
    print(f"{B}  dnabot Strategie-Vergleich{NC}")
    print(f"  WF Re-Opt ({lookback_weeks}w Fenster)  vs.  Alle Configs dauerhaft")
    print(f"{'=' * w}")
    print(f"\n  {'Kennzahl':<22} {'WF Re-Opt':>18} {'Alle Configs':>18}")
    print(f"  {'-' * (w - 2)}")

    rows = [
        ('PnL %',         f"{col_wf}{sign_wf}{pnl_wf:.1f}%{NC}",            f"{col_all}{sign_all}{stats_all['pnl_pct']:.1f}%{NC}"),
        ('Final Equity',  f"{curve_wf[-1][1]:.2f} USDT",                    f"{stats_all['equity']:.2f} USDT"),
        ('Max Drawdown',  f"{R if max_dd_wf > 25 else Y}{max_dd_wf:.1f}%{NC}", f"{R if stats_all['max_dd'] > 25 else Y}{stats_all['max_dd']:.1f}%{NC}"),
        ('Calmar Ratio',  f"{calmar_wf:.2f}",                                f"{calmar_all:.2f}"),
        ('Win-Rate',      f"{wr_wf:.1%}",                                    f"{stats_all['wr']:.1%}"),
        ('Trades',        str(n_wf),                                         str(stats_all['n'])),
        ('WF Fenster',    str(len(wstats)),                                  '—'),
    ]
    for label, vwf, vall in rows:
        print(f"  {label:<22} {vwf:>28} {vall:>28}")

    winner     = 'WF Re-Opt' if calmar_wf > calmar_all else 'Alle Configs'
    winner_col = G if winner == 'WF Re-Opt' else Y
    print(f"\n  {'─' * (w - 2)}")
    print(f"  {B}Gewinner (nach Calmar-Ratio):{NC} {winner_col}{winner}{NC}")

    # Fenster-Details
    if wstats:
        print(f"\n  {Y}Fenster-Übersicht (Walk-Forward){NC}")
        print(f"  {'Zeitraum':<26} {'Pairs':>5} {'n':>5} {'PnL%':>8} {'MaxDD':>8}")
        print(f"  {'-' * (w - 2)}")
        for ws in wstats:
            date_str = f"{ws['train_end'].strftime('%Y-%m-%d')} → {ws['test_end'].strftime('%Y-%m-%d')}"
            col      = G if ws['pnl_pct'] >= 0 else R
            sign     = '+' if ws['pnl_pct'] >= 0 else ''
            print(
                f"  {date_str:<26} {len(ws['selected']):>5} {ws['n_trades']:>5} "
                f"{col}{sign}{ws['pnl_pct']:>6.1f}%{NC} {ws['max_dd']:>7.1f}%"
            )
    print(f"\n{'=' * w}\n")

    if not no_telegram:
        token, chat_id = _get_telegram()
        if token:
            msg = (
                f"dnabot Strategie-Vergleich\n\n"
                f"WF Re-Opt ({lookback_weeks}w Fenster):\n"
                f"  PnL: {sign_wf}{pnl_wf:.1f}% | MaxDD: {max_dd_wf:.1f}%\n"
                f"  WR: {wr_wf:.1%} | Calmar: {calmar_wf:.2f} | {n_wf} Trades\n\n"
                f"Alle Configs:\n"
                f"  PnL: {sign_all}{stats_all['pnl_pct']:.1f}% | MaxDD: {stats_all['max_dd']:.1f}%\n"
                f"  WR: {stats_all['wr']:.1%} | Calmar: {calmar_all:.2f} | {stats_all['n']} Trades\n\n"
                f"Gewinner (Calmar): {winner}"
            )
            _send_message(token, chat_id, msg)
            print(f"  {G}✓ Zusammenfassung via Telegram gesendet.{NC}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    # Defaults aus settings.json lesen
    default_capital   = 100.0
    default_risk      = 1.0
    default_lookback  = 2
    default_start     = None
    try:
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
        default_capital  = float(s.get('optimization_settings', {}).get('start_capital', 100.0))
        default_risk     = float(s.get('risk_settings', {}).get('risk_per_entry_pct', 1.0))
        default_lookback = int(s.get('optimization_settings', {}).get('backtest_lookback_weeks', 2))
        default_start    = s.get('optimization_settings', {}).get('backtest_start_date')
    except Exception:
        pass

    parser = argparse.ArgumentParser(description='dnabot Strategie-Vergleich')
    parser.add_argument('--capital',      type=float, default=default_capital)
    parser.add_argument('--risk',         type=float, default=default_risk)
    parser.add_argument('--max-dd',       type=float, default=30.0,
                        help='Max. Drawdown-Limit für WF Portfolio-Auswahl (%)')
    parser.add_argument('--lookback',     type=int,   default=default_lookback,
                        help='Fenster-Größe in Wochen (Training + Test je lookback Wochen)')
    parser.add_argument('--min-trades',   type=int,   default=2,
                        help='Mindest-Trades pro Pair im Trainingsfenster')
    parser.add_argument('--start-date',   type=str,   default=default_start,
                        help='Startdatum (YYYY-MM-DD)')
    parser.add_argument('--end-date',     type=str,   default=None)
    parser.add_argument('--no-telegram',  action='store_true')
    args = parser.parse_args()

    print(f"\n{'─' * 72}")
    print(f"{B}  dnabot Strategie-Vergleich{NC}")
    print(f"  WF Re-Opt ({args.lookback}w) vs. Alle Configs  |  "
          f"Kapital: {args.capital:.0f} USDT  |  Risiko: {args.risk}%  |  MaxDD-Limit: {args.max_dd:.0f}%")
    print(f"{'─' * 72}\n")

    print("  Lade Backtest-Ergebnisse ...", end='', flush=True)
    all_results = load_all_results()
    if not all_results:
        print(f"\n{R}  Keine Backtest-Ergebnisse gefunden. Zuerst Mode 1 ausführen!{NC}\n")
        sys.exit(1)

    all_trades_flat = [t for r in all_results for t in r['trades']]
    if not all_trades_flat:
        print(f"\n{R}  Keine Trades in den Ergebnissen.{NC}\n")
        sys.exit(1)

    all_times = sorted(t['entry_dt'] for t in all_trades_flat)
    data_start = all_times[0].replace(hour=0, minute=0, second=0, microsecond=0)
    data_end   = all_times[-1].replace(hour=23, minute=59, second=59, microsecond=0)

    start_dt = (datetime.fromisoformat(args.start_date).replace(tzinfo=timezone.utc)
                if args.start_date else data_start)
    end_dt   = (datetime.fromisoformat(args.end_date).replace(tzinfo=timezone.utc)
                if args.end_date else data_end)

    n_configs = len(all_results)
    n_trades  = len(all_trades_flat)
    date_range = f"{start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}"
    print(f" {n_configs} Configs, {n_trades} Trades. Zeitraum: {date_range}")

    min_period = timedelta(weeks=args.lookback * 2)
    if (end_dt - start_dt) < min_period:
        print(f"\n{R}  Zeitraum zu kurz für Walk-Forward "
              f"(mindestens {args.lookback * 2} Wochen benötigt).{NC}\n")
        sys.exit(1)

    print(f"\n  {C}Schritt 1/2: Walk-Forward Re-Opt ({args.lookback}w Fenster) ...{NC}")
    curve_wf, wstats = simulate_walkforward(
        all_results, args.capital, args.risk, args.max_dd,
        args.lookback, args.min_trades, start_dt, end_dt
    )
    n_wf = sum(w['n_trades'] for w in wstats)
    print(f"  {G}✓{NC} {len(wstats)} Fenster, {n_wf} Trades, "
          f"Final Equity: {curve_wf[-1][1]:.2f} USDT")

    print(f"\n  {C}Schritt 2/2: Alle Configs (keine Filterung) ...{NC}")
    curve_all, stats_all = simulate_all_configs(
        all_results, args.capital, args.risk, start_dt, end_dt
    )
    print(f"  {G}✓{NC} {stats_all['n']} Trades, "
          f"Final Equity: {stats_all['equity']:.2f} USDT")

    print()
    print_summary(curve_wf, curve_all, wstats, stats_all,
                  args.capital, args.risk, args.lookback, args.no_telegram)

    print(f"  {C}Erstelle Charts ...{NC}")
    generate_png_chart(curve_wf, curve_all, wstats, stats_all,
                        args.capital, args.risk, args.lookback, args.no_telegram)
    generate_html_chart(curve_wf, curve_all, wstats, stats_all,
                         args.capital, args.risk, args.lookback, args.no_telegram)


if __name__ == '__main__':
    main()
