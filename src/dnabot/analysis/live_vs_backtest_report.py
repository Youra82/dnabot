"""Illustrierte Live-vs-Backtest-Uebersicht fuer den Tagescheck (daily_live_vs_backtest_check.py).
Uebernommen von stbot.

Erzeugt aus den abgeglichenen Trades
  - eine eigenstaendige HTML-Seite (handytauglich, hell/dunkel, ohne externe Dateien)
  - ein PNG mit denselben zwei Diagrammen als Vorschau im Telegram-Chat
Diagramme: (1) Ergebnis seit Fensterbeginn Live vs. Backtest, (2) Zeitachse je Strategie
mit Team-Zeitraum, Live- und Backtest-Trades und markierten Abweichungen. Dazu Kennzahlen,
Portfolio-Verlauf und eine Tabelle aller Trades mit Ursache.

items: Liste von dicts, eine Zeile je Fall
  status  'gleich' | 'fehlt' | 'nur_live'
  key     Strategie (z. B. ENAUSDTUSDT_15m) oder None
  name    Anzeigename (ENA/15m)
  side    long/short
  live    dict(entry_time, exit_time, entry, exit, pnl) oder None
  sim     dict(signal_time, exit_time, entry, exit, pnl) oder None
  exit_dev_pct  Exit-Abweichung in % (nur 'gleich' mit beiden Exits) oder None
  reason  Ursache/Hinweis (Text) oder ''
"""
import html
import math
import os

import pandas as pd

LIVE_COLOR, SIM_COLOR = '#2a78d6', '#eb6834'          # Referenzpalette Slot 1/2 (hell)
LIVE_DARK, SIM_DARK = '#3987e5', '#d95926'            # dieselben Plaetze, dunkel
GOOD, SERIOUS, CRITICAL = '#0ca30c', '#ec835a', '#d03b3b'
STATUS = {'gleich': ('✓', 'gleich', GOOD), 'fehlt': ('✕', 'fehlt live', CRITICAL),
          'nur_live': ('!', 'nur live', SERIOUS)}


def _t(ts):
    return '' if ts is None else pd.Timestamp(ts).strftime('%d.%m. %H:%M')


def _num(x, fmt='{:+.2f}'):
    return '–' if x is None else fmt.format(x)


def _price(x):
    return '–' if x is None else f'{x:.6g}'


def _cell(tr, tkey):
    """Tabellenzelle: Zeit, Einstieg, Ausstieg, Ergebnis -- untereinander, damit es aufs Handy passt."""
    if not tr:
        return '–'
    return (f"{_t(tr.get(tkey))}<br>{_price(tr.get('entry'))} →<br>{_price(tr.get('exit')) if tr.get('exit') else 'offen'}"
            f"<br><b>{_num(tr.get('pnl'))}</b>")


def _nice_step(span):
    """Runde Achsenschritte (1, 2, 5 x 10^k), etwa 3-5 Linien."""
    span = max(span, 0.5)
    base = 10 ** math.floor(math.log10(span / 4))
    return next(m * base for m in (1, 2, 5, 10) if span / (m * base) <= 5)


def equity_steps(items, start, end):
    """Kumuliertes Ergebnis (USDT) je Seite als Treppen-Stuetzpunkte [(t, wert), ...]."""
    out = {}
    for side in ('live', 'sim'):
        pts = sorted((d[side]['exit_time'], d[side]['pnl']) for d in items
                     if d[side] and d[side]['exit_time'] is not None and d[side]['pnl'] is not None
                     and d[side]['exit_time'] <= end)
        cum, steps = 0.0, [(start, 0.0)]
        for t, pnl in pts:
            cum += pnl
            steps.append((t, cum))
        steps.append((end, cum))
        out[side] = steps
    return out


def lanes(items, team_periods):
    """Zeilen der Zeitachse: alle Strategien mit Team-Zeitraum oder Trade, in Teamreihenfolge."""
    names = list(team_periods)
    for d in items:
        if d['name'] not in names:
            names.append(d['name'])
    return names


# --- SVG (HTML-Seite) ---------------------------------------------------------

def _svg_equity(steps, start, end):
    W, H, L, R, T, B = 340, 200, 38, 8, 16, 28
    vals = [v for s in steps.values() for _, v in s] + [0.0]
    step = _nice_step(max(vals) - min(vals))
    lo, hi = math.floor(min(vals) / step) * step, math.ceil(max(vals) / step) * step
    if hi - lo < step:
        hi = lo + step
    span = (end - start).total_seconds()
    x = lambda t: L + (pd.Timestamp(t) - start).total_seconds() / span * (W - L - R)
    y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Ergebnis seit Fensterbeginn">']
    # Gitter + Achse (ruhig)
    for k in range(int(round((hi - lo) / step)) + 1):
        v = lo + k * step
        parts.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text class="ax" x="{L - 5}" y="{y(v) + 4:.1f}" text-anchor="end">{v:+g}</text>')
    parts.append(f'<line class="base" x1="{L}" x2="{W - R}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    days = pd.date_range(start.normalize() + pd.Timedelta(days=1), end, freq='D')
    for dday in days[::max(1, -(-len(days) // 4))]:
        parts.append(f'<text class="ax" x="{x(dday):.1f}" y="{H - 10}" text-anchor="middle">'
                     f'{dday.strftime("%d.%m.")}</text>')
    for side, cls in (('sim', 'sim'), ('live', 'live')):
        pts, path = steps[side], []
        for i, (t, v) in enumerate(pts):
            if i:
                path.append(f'L{x(t):.1f},{y(pts[i - 1][1]):.1f}')
            path.append(f'{"M" if not i else "L"}{x(t):.1f},{y(v):.1f}')
        parts.append(f'<path class="line {cls}" d="{" ".join(path)}"/>')
        for t, v in pts[1:-1]:
            tip = f'{"Live" if side == "live" else "Backtest"} {_t(t)}: {v:+.2f} USDT'
            parts.append(f'<circle class="dot {cls}" cx="{x(t):.1f}" cy="{y(v):.1f}" r="4" data-tip="{tip}">'
                         f'<title>{tip}</title></circle>')
        last = pts[-1]
        parts.append(f'<text class="lbl" x="{W - R}" y="{y(last[1]) - 6:.1f}" text-anchor="end">'
                     f'{"Live" if side == "live" else "Backtest"} {last[1]:+.2f}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def _svg_timeline(items, team_periods, start, end, carry=()):
    names = lanes(items, team_periods)
    W, L, R, T, LANE = 340, 70, 8, 8, 30
    H = T + LANE * len(names) + 26
    span = (end - start).total_seconds()
    x = lambda t: L + (min(max(pd.Timestamp(t), start), end) - start).total_seconds() / span * (W - L - R)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Trades je Strategie">']
    for dday in pd.date_range(start.normalize() + pd.Timedelta(days=1), end, freq='D'):
        parts.append(f'<line class="grid" x1="{x(dday):.1f}" x2="{x(dday):.1f}" y1="{T}" y2="{H - 22}"/>'
                     f'<text class="ax" x="{x(dday):.1f}" y="{H - 8}" text-anchor="middle">{dday.strftime("%d.")}</text>')
    for i, name in enumerate(names):
        yc = T + LANE * i + LANE / 2
        parts.append(f'<text class="lane" x="{L - 6}" y="{yc + 4:.1f}" text-anchor="end">{html.escape(name)}</text>')
        for a, b in team_periods.get(name, []):
            x0, x1 = x(a), x(b if b is not None else end)
            if x1 > x0:
                parts.append(f'<rect class="team" x="{x0:.1f}" y="{yc - 11:.1f}" width="{x1 - x0:.1f}" height="22" rx="4">'
                             f'<title>{html.escape(name)} im Team {_t(a)} – {_t(b) or "jetzt"}</title></rect>')
    for c in carry:
        if c['name'] not in names:
            continue
        yc = T + LANE * names.index(c['name']) + LANE / 2 - 4
        tip = f"{c['name']} Live-Position seit {_t(c['entry_time'])}, {'offen' if c['exit_time'] is None else 'zu ' + _t(c['exit_time'])}"
        parts.append(f'<g data-tip="{html.escape(tip)}"><title>{html.escape(tip)}</title>'
                     f'<line class="bar live carry" x1="{x(start):.1f}" x2="{x(c["exit_time"] or end):.1f}" '
                     f'y1="{yc:.1f}" y2="{yc:.1f}"/></g>')
    for d in items:
        i = names.index(d['name'])
        yc = T + LANE * i + LANE / 2
        sym, word, _ = STATUS[d['status']]
        for side, dy in (('live', -4), ('sim', 4)):
            tr = d[side]
            if not tr:
                continue
            t0 = tr['entry_time'] if side == 'live' else tr['signal_time']
            t1 = tr['exit_time'] or end
            tip = (f'{d["name"]} {d["side"]} {"Live" if side == "live" else "Backtest"} {_t(t0)} → '
                   f'{_t(tr["exit_time"]) or "offen"} | {_num(tr["pnl"])} USDT | {word}')
            parts.append(f'<g data-tip="{html.escape(tip)}"><title>{html.escape(tip)}</title>'
                         f'<line class="bar {side}" x1="{x(t0):.1f}" x2="{max(x(t1), x(t0) + 3):.1f}" '
                         f'y1="{yc + dy:.1f}" y2="{yc + dy:.1f}"/>'
                         f'<circle class="dot {side}" cx="{x(t0):.1f}" cy="{yc + dy:.1f}" r="4"/></g>')
        if d['status'] != 'gleich':
            t0 = (d['sim'] or {}).get('signal_time') or d['live']['entry_time']
            parts.append(f'<text class="flag {d["status"]}" x="{x(t0):.1f}" y="{yc - 9:.1f}" text-anchor="middle">{sym}</text>')
    parts.append('</svg>')
    return ''.join(parts)


CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--base:#c3c2b7;--ring:rgba(11,11,11,.10);--live:%(lv)s;--sim:%(sm)s;--team:rgba(137,135,129,.14);
--good:%(gd)s;--serious:%(se)s;--critical:%(cr)s}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--base:#383835;--ring:rgba(255,255,255,.10);--live:%(lvd)s;--sim:%(smd)s;
--team:rgba(195,194,183,.10)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;
--base:#383835;--ring:rgba(255,255,255,.10);--live:%(lvd)s;--sim:%(smd)s;--team:rgba(195,194,183,.10)}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:880px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px}h2{font-size:16px;margin:0 0 8px}.sub{color:var(--ink2);margin:0 0 14px;font-size:13px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px;margin:0 0 14px}
.tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:0 0 14px}
@media(min-width:640px){.tiles{grid-template-columns:repeat(4,1fr)}}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:12px}
.tile .k{color:var(--ink2);font-size:12px}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .s{color:var(--muted);font-size:12px}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--ink2);margin:0 0 6px}
.sw{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
svg{width:100%%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.base{stroke:var(--base);stroke-width:1}
.ax{fill:var(--muted);font-size:11px}.lane{fill:var(--ink2);font-size:12px}.lbl{fill:var(--ink);font-size:12px;font-weight:600}
.line{fill:none;stroke-width:2;stroke-linejoin:round}.line.live{stroke:var(--live)}.line.sim{stroke:var(--sim)}
.dot{stroke:var(--surface);stroke-width:2}.dot.live{fill:var(--live)}.dot.sim{fill:var(--sim)}
.bar{stroke-width:3;stroke-linecap:round}.bar.live{stroke:var(--live)}.bar.sim{stroke:var(--sim)}
.carry{stroke-dasharray:4 4}.team{fill:var(--team)}.flag{font-size:12px;font-weight:700}.flag.fehlt{fill:var(--critical)}.flag.nur_live{fill:var(--serious)}
ul.pf{margin:0;padding-left:18px}ul.pf li{margin:2px 0}
.tbl{width:100%%;border-collapse:collapse;font-size:13px}.tbl th{text-align:left;color:var(--ink2);font-weight:500;
border-bottom:1px solid var(--base);padding:6px 4px}.tbl td{border-bottom:1px solid var(--grid);padding:6px 4px;vertical-align:top}
.tbl .n{font-variant-numeric:tabular-nums}.st{white-space:nowrap;font-weight:600}
.st.gleich{color:var(--good)}.st.fehlt{color:var(--critical)}.st.nur_live{color:var(--serious)}
.why{color:var(--ink2);font-size:12px}.scroll{overflow-x:auto}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
border-radius:8px;padding:6px 8px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.18);display:none;max-width:260px}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
 const show=e=>{tip.textContent=el.dataset.tip;tip.style.display='block';
  const p=e.touches?e.touches[0]:e;tip.style.left=Math.min(p.clientX+12,innerWidth-270)+'px';tip.style.top=(p.clientY+12)+'px'};
 el.addEventListener('mousemove',show);el.addEventListener('touchstart',show,{passive:true});
 el.addEventListener('mouseleave',()=>tip.style.display='none')});
document.addEventListener('touchstart',e=>{if(!e.target.closest('[data-tip]'))tip.style.display='none'},{passive:true});
"""


def build_html(items, team_periods, portfolio_lines, order_flags, summary, start, end, carry=()):
    steps = equity_steps(items, start, end)
    n_sim = sum(1 for d in items if d['sim'])
    n_same = sum(1 for d in items if d['status'] == 'gleich')
    tiles = [
        ('Live', f"{summary['live_pnl']:+.2f} USDT", f"{summary['live_n']} Trades"),
        ('Backtest', f"{summary['sim_pnl']:+.2f} USDT", f"{n_sim} Trades"),
        ('Gleiche Einstiege', f"{n_same}/{n_sim}", f"{sum(d['status'] == 'fehlt' for d in items)} fehlen, "
                                                    f"{sum(d['status'] == 'nur_live' for d in items)} nur live"),
        ('Konto', f"{summary['equity']:.2f} USDT" if summary['equity'] is not None else '–', 'Bitget, jetzt'),
    ]
    rows = []
    for d in sorted(items, key=lambda d: (d['live'] or {}).get('entry_time') or d['sim']['signal_time']):
        sym, word, _ = STATUS[d['status']]
        lv, sm = d['live'] or {}, d['sim'] or {}
        dev = f"<br><span class='why'>Exit {d['exit_dev_pct']:.1f} % daneben</span>" if d.get('exit_dev_pct') else ''
        rows.append(
            f"<tr><td class='st {d['status']}'>{sym} {word}</td><td>{html.escape(d['name'])}<br>"
            f"<span class='why'>{d['side']}</span></td>"
            f"<td class='n'>{_cell(lv, 'entry_time')}</td><td class='n'>{_cell(sm, 'signal_time')}</td></tr>"
            + (f"<tr><td></td><td colspan='3' class='why'>{html.escape(d['reason'])}{dev}</td></tr>"
               if d['reason'] or dev else ''))
    flags = ''.join(f'<li>{html.escape(f)}</li>' for f in order_flags) or '<li>keine</li>'
    pf = ''.join(f'<li>{html.escape(l.lstrip("• "))}</li>' for l in portfolio_lines[1:])
    css = CSS % dict(lv=LIVE_COLOR, sm=SIM_COLOR, lvd=LIVE_DARK, smd=SIM_DARK, gd=GOOD, se=SERIOUS, cr=CRITICAL)
    legend_eq = (f"<div class='legend'><span><i class='sw' style='background:var(--live)'></i>Live</span>"
                 f"<span><i class='sw' style='background:var(--sim)'></i>Backtest</span></div>")
    legend_tl = (legend_eq[:-6] + "<span><i class='sw' style='background:var(--team);height:10px'></i>im Team</span>"
                 "<span style='color:var(--critical)'>✕ fehlt live</span>"
                 "<span style='color:var(--serious)'>! nur live</span>"
                 "<span><i class='sw' style='background:repeating-linear-gradient(90deg,var(--live) 0 4px,transparent 4px 8px)'></i>"
                 "Live-Position von vorher</span></div>")
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>dnabot Live vs Backtest</title>
<style>{css}</style></head><body><main>
<h1>dnabot Live vs. Backtest</h1>
<p class="sub">{_t(start)} – {_t(end)} UTC · Backtest = Portfolio-Simulator mit den Live-Regeln und dem jeweils gültigen Team</p>
<div class="tiles">{''.join(f"<div class='tile'><div class='k'>{k}</div><div class='v'>{v}</div><div class='s'>{s}</div></div>" for k, v, s in tiles)}</div>
<div class="card"><h2>Ergebnis seit Fensterbeginn (USDT)</h2>{legend_eq}{_svg_equity(steps, start, end)}</div>
<div class="card"><h2>Trades je Strategie</h2>{legend_tl}{_svg_timeline(items, team_periods, start, end, carry)}</div>
<div class="card"><h2>Alle Trades</h2><div class="scroll"><table class="tbl">
<tr><th>Status</th><th>Strategie</th><th>Live</th><th>Backtest</th></tr>{''.join(rows)}</table></div></div>
<div class="card"><h2>Portfolio im Zeitraum</h2><ul class="pf">{pf}</ul></div>
<div class="card"><h2>Hinweise (Daten &amp; Orders)</h2><ul class="pf">{flags}</ul></div>
</main><div id="tip"></div><script>{JS}</script></body></html>"""


# --- PNG (Telegram-Vorschau) --------------------------------------------------

def build_png(items, team_periods, start, end, path, carry=()):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    steps = equity_steps(items, start, end)
    names = lanes(items, team_periods)
    ink, ink2, muted, grid = '#0b0b0b', '#52514e', '#898781', '#e1e0d9'
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.2, 3.2 + 0.42 * len(names)), dpi=150,
                                 gridspec_kw={'height_ratios': [2.2, 0.45 * len(names) + 0.6]})
    fig.patch.set_facecolor('#fcfcfb')
    for ax in (a1, a2):
        ax.set_facecolor('#fcfcfb')
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(colors=muted, labelsize=8, length=0)
    a1.set_title('Ergebnis seit Fensterbeginn (USDT)', loc='left', fontsize=10, color=ink)
    a1.axhline(0, color='#c3c2b7', lw=1)
    a1.grid(axis='y', color=grid, lw=0.8)
    for side, color, label in (('live', LIVE_COLOR, 'Live'), ('sim', SIM_COLOR, 'Backtest')):
        ts = [pd.Timestamp(t).tz_convert(None) for t, _ in steps[side]]
        vs = [v for _, v in steps[side]]
        a1.step(ts, vs, where='post', color=color, lw=2, label=f'{label} {vs[-1]:+.2f}')
    a1.legend(loc='upper left', frameon=False, fontsize=8, labelcolor=ink2)
    a1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m.'))

    a2.set_title('Trades je Strategie  (blau Live, orange Backtest, ✕ fehlt live, ! nur live, - - Position von vorher)',
                 loc='left', fontsize=9, color=ink)
    for i, name in enumerate(names):
        y = len(names) - 1 - i
        for a, b in team_periods.get(name, []):
            a0 = max(pd.Timestamp(a), start).tz_convert(None)
            b0 = (pd.Timestamp(b) if b is not None else end).tz_convert(None)
            if b0 > a0:
                a2.barh(y, b0 - a0, left=a0, height=0.7, color='#898781', alpha=0.14, lw=0)
    for c in carry:
        if c['name'] in names:
            y = len(names) - 1 - names.index(c['name'])
            a2.plot([start.tz_convert(None), pd.Timestamp(c['exit_time'] or end).tz_convert(None)], [y + 0.13] * 2,
                    color=LIVE_COLOR, lw=2, ls=(0, (3, 3)))
    for d in items:
        y = len(names) - 1 - names.index(d['name'])
        for side, dy, color in (('live', 0.13, LIVE_COLOR), ('sim', -0.13, SIM_COLOR)):
            tr = d[side]
            if not tr:
                continue
            t0 = pd.Timestamp(tr['entry_time'] if side == 'live' else tr['signal_time']).tz_convert(None)
            t1 = pd.Timestamp(tr['exit_time'] or end).tz_convert(None)
            a2.plot([t0, t1], [y + dy, y + dy], color=color, lw=2.5, solid_capstyle='round')
            a2.plot([t0], [y + dy], 'o', color=color, ms=5, mec='#fcfcfb', mew=1.2)
        if d['status'] != 'gleich':
            sym, _, col = STATUS[d['status']]
            t0 = pd.Timestamp((d['sim'] or {}).get('signal_time') or d['live']['entry_time']).tz_convert(None)
            a2.text(t0, y + 0.36, sym, color=col, fontsize=10, fontweight='bold', ha='center', va='bottom')
    a2.set_yticks(range(len(names)), list(reversed(names)), color=ink2, fontsize=8)
    a2.set_ylim(-0.6, len(names) - 0.1 + 0.5)
    a2.set_xlim(start.tz_convert(None), end.tz_convert(None))
    a2.xaxis.set_major_formatter(mdates.DateFormatter('%d.'))
    a2.grid(axis='x', color=grid, lw=0.8)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def write_report(items, team_periods, portfolio_lines, order_flags, summary, start, end, out_dir, carry=()):
    os.makedirs(out_dir, exist_ok=True)
    stamp = end.strftime('%Y-%m-%d')
    html_path = os.path.join(out_dir, f'dnabot_live_vs_backtest_{stamp}.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(build_html(items, team_periods, portfolio_lines, order_flags, summary, start, end, carry))
    png_path = build_png(items, team_periods, start, end,
                         os.path.join(out_dir, f'dnabot_live_vs_backtest_{stamp}.png'), carry)
    return png_path, html_path
