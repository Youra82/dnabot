# src/dnabot/utils/trade_manager.py
# Trade-Management für dnabot (momentum_exit-Strategie, siehe
# strategy/momentum_exit_logic.py)
#
# Unterschiede zu dbot/ltbbot:
#   - Signal kommt von momentum_exit_logic (nicht-praediktiver Einstieg,
#     Richtung = eigene Kerzenrichtung; Edge steckt im Risiko-/Exit-Gen)
#   - SL = Low/High der letzten seq_len Kerzen (nicht % vom Entry)
#   - Self-Learning: Nach Trade-Abschluss wird das aktive Risiko-Gen in der
#     RiskGenomeDB aktualisiert (Calmar-Tracking, siehe genome/risk_genome_db.py)
#   - 1 Entry (kein 3-Layer-System)

import logging
import time
import json
import os
import sys
import ccxt
import pandas as pd
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker')

sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.telegram import send_message, send_photo
from dnabot.utils.exchange import Exchange
from dnabot.genome.risk_genome_db import RiskGenomeDB
from dnabot.strategy.momentum_exit_logic import get_momentum_exit_signal

MIN_NOTIONAL_USDT = 5.0
MAX_NOTIONAL_USDT = 200_000.0   # Obergrenze Positionsgröße pro Trade
RISK_DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_genome.db')


FETCH_LIMIT = 200   # Kerzen für Signal-Berechnung (ATR + Sequenz)


# ─── Tracker File Handling ────────────────────────────────────────────────────

def get_tracker_file_path(symbol: str, timeframe: str) -> str:
    os.makedirs(TRACKER_DIR, exist_ok=True)
    safe = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}.json"
    return os.path.join(TRACKER_DIR, safe)


def read_tracker(path: str) -> dict:
    default = {
        "status": "ok_to_trade",
        "last_side": None,
        "stop_loss_ids": [],
        "take_profit_ids": [],
        "active_genome": None,
        "performance": {
            "total_trades": 0, "wins": 0, "losses": 0,
            "consecutive_losses": 0, "consecutive_wins": 0,
        }
    }
    if not os.path.exists(path):
        _write_tracker(path, default)
        return default
    try:
        with open(path, 'r') as f:
            content = f.read()
        return json.loads(content) if content else default
    except (json.JSONDecodeError, FileNotFoundError):
        _write_tracker(path, default)
        return default


def _write_tracker(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Fehler beim Schreiben des Trackers {path}: {e}")


# ─── Performance Tracking ─────────────────────────────────────────────────────

def record_trade_result(path: str, outcome: str, logger: logging.Logger):
    tracker = read_tracker(path)
    perf = tracker.setdefault('performance', {
        "total_trades": 0, "wins": 0, "losses": 0,
        "consecutive_losses": 0, "consecutive_wins": 0,
    })
    perf['total_trades'] = perf.get('total_trades', 0) + 1
    if outcome == 'win':
        perf['wins'] = perf.get('wins', 0) + 1
        perf['consecutive_wins'] = perf.get('consecutive_wins', 0) + 1
        perf['consecutive_losses'] = 0
    else:
        perf['losses'] = perf.get('losses', 0) + 1
        perf['consecutive_losses'] = perf.get('consecutive_losses', 0) + 1
        perf['consecutive_wins'] = 0

    total = perf['total_trades']
    if total > 0:
        perf['win_rate'] = perf['wins'] / total
    _write_tracker(path, tracker)


def should_skip_trading(path: str) -> tuple[bool, str]:
    tracker = read_tracker(path)
    perf = tracker.get('performance', {})
    if perf.get('consecutive_losses', 0) >= 5:
        return True, f"{perf['consecutive_losses']} aufeinanderfolgende Verluste"
    total = perf.get('total_trades', 0)
    if total >= 30 and perf.get('win_rate', 1.0) < 0.25:
        return True, f"Win-Rate {perf.get('win_rate', 0):.1%} nach {total} Trades"
    return False, "OK"


# ─── Order Management ────────────────────────────────────────────────────────

def cancel_entry_orders(exchange: Exchange, symbol: str, logger: logging.Logger,
                         tracker_path: str = None):
    """Storniert alle offenen Limit- und nicht-reduceOnly Trigger-Orders."""
    # TP/SL-Order-IDs aus Tracker schützen (Bitget gibt reduceOnly oft nicht zurück)
    protected_ids: set = set()
    has_active_trade = False
    if tracker_path:
        try:
            t = read_tracker(tracker_path)
            protected_ids.update(t.get('take_profit_ids', []))
            protected_ids.update(t.get('stop_loss_ids', []))
            has_active_trade = bool(
                t.get('active_genome') or
                t.get('take_profit_ids') or
                t.get('stop_loss_ids')
            )
        except Exception:
            pass

    count = 0
    for order in exchange.fetch_open_orders(symbol):
        if order['id'] in protected_ids:
            continue
        try:
            exchange.cancel_order(order['id'], symbol)
            count += 1
            time.sleep(0.1)
        except ccxt.OrderNotFound:
            pass
        except Exception as e:
            logger.warning(f"Konnte Order {order['id']} nicht stornieren: {e}")

    # Trigger-Orders (SL/TP) nur canceln wenn dieser TF-Bot-Instanz einen aktiven Trade hat.
    # Ohne aktiven Trade könnten die Orders von einem anderen TF-Bot für dasselbe Symbol stammen
    # (Bitget hat nur 1 Position pro Symbol — mehrere TF-Instanzen sehen dieselbe Position).
    if not has_active_trade:
        return count

    for order in exchange.fetch_open_trigger_orders(symbol):
        if order.get('reduceOnly') or order['id'] in protected_ids:
            continue
        try:
            exchange.cancel_trigger_order(order['id'], symbol)
            count += 1
            time.sleep(0.1)
        except ccxt.OrderNotFound:
            pass
        except Exception as e:
            logger.warning(f"Konnte Trigger {order['id']} nicht stornieren: {e}")

    return count


def check_sl_triggered(exchange: Exchange, symbol: str, tracker_path: str,
                        logger: logging.Logger, current_price: float = 0.0) -> bool:
    """
    SL ausgelöst wenn SL-ID nicht mehr unter offenen Trigger-Orders
    UND aktueller Preis unter dem SL-Preis liegt (unterscheidet SL von TP-Storno).
    """
    tracker = read_tracker(tracker_path)
    sl_ids = tracker.get('stop_loss_ids', [])
    if not sl_ids:
        return False
    try:
        open_trigger_ids = {o['id'] for o in exchange.fetch_open_trigger_orders(symbol)}
        gone = [oid for oid in sl_ids if oid not in open_trigger_ids]
        if not gone:
            return False

        # Preis-Check: SL-Order weg wegen Auslösung oder wegen TP-Storno?
        active_genome = tracker.get('active_genome') or {}
        sl_price = active_genome.get('sl_price', 0)
        last_side = tracker.get('last_side', 'long')

        sl_hit = False
        if sl_price > 0 and current_price > 0:
            if last_side == 'long' and current_price <= sl_price:
                sl_hit = True
            elif last_side == 'short' and current_price >= sl_price:
                sl_hit = True
        else:
            # Kein Preisvergleich möglich → annehmen dass SL ausgelöst wurde
            sl_hit = True

        if sl_hit:
            logger.warning(f"STOP LOSS ausgelöst für {symbol}! (Preis {current_price:.4f} ≤ SL {sl_price:.4f})")
            tracker.update({
                "status": "ok_to_trade",
                "last_side": last_side,
                "stop_loss_ids": [],
                "take_profit_ids": [],
            })
            tracker.pop('last_notified_entry_price', None)
            tracker.pop('last_notified_side', None)
            _write_tracker(tracker_path, tracker)
            return True
        else:
            logger.info(f"SL-Order verschwunden, aber Preis ({current_price:.4f}) > SL ({sl_price:.4f}) → TP hat ausgelöst, SL wurde storniert.")
    except Exception as e:
        logger.error(f"Fehler beim Prüfen des SL: {e}", exc_info=True)
    return False


def check_tp_triggered(exchange: Exchange, symbol: str, tracker_path: str,
                        logger: logging.Logger, current_price: float = 0.0) -> bool:
    """TP/Trailing Stop ausgelöst wenn TP-ID nicht mehr unter offenen Trigger-Orders."""
    tracker = read_tracker(tracker_path)
    tp_ids = tracker.get('take_profit_ids', [])
    if not tp_ids:
        return False
    try:
        open_trigger_ids = {o['id'] for o in exchange.fetch_open_trigger_orders(symbol)}
        gone = [oid for oid in tp_ids if oid not in open_trigger_ids]
        if gone:
            logger.info(f"TAKE PROFIT / Trailing Stop ausgelöst für {symbol}!")
            tracker.update({"status": "ok_to_trade", "take_profit_ids": [], "stop_loss_ids": []})
            tracker.pop('last_notified_entry_price', None)
            tracker.pop('last_notified_side', None)
            _write_tracker(tracker_path, tracker)
            return True
    except Exception as e:
        logger.error(f"Fehler beim Prüfen des TP: {e}", exc_info=True)
    return False


def notify_new_position(exchange: Exchange, position: dict, params: dict,
                         tracker_path: str, telegram_config: dict, logger: logging.Logger):
    """Tracker aktualisieren wenn Position erkannt wird (Telegram kommt bereits von place_entry_orders)."""
    tracker = read_tracker(tracker_path)
    entry_price = float(position.get('entryPrice', 0))
    side = position.get('side', '')

    last_entry = tracker.get('last_notified_entry_price')
    last_side = tracker.get('last_notified_side')

    is_new = (
        last_entry is None or last_side is None or
        abs(entry_price - last_entry) > entry_price * 0.001 or
        side != last_side
    )

    if is_new:
        tracker['last_notified_entry_price'] = entry_price
        tracker['last_notified_side'] = side
        _write_tracker(tracker_path, tracker)


def ensure_tp_sl(exchange: Exchange, position: dict, genome_signal: dict,
                  params: dict, tracker_path: str, telegram_config: dict,
                  logger: logging.Logger):
    """Erkennt fehlende SL/TP-Orders und stellt sie neu aus.
    Strategie: ID-Check zuerst, Preis-Richtungs-Fallback wenn keine IDs.
    Sendet Telegram-Alert wenn Reparatur nötig oder Preis unbekannt."""
    symbol = params['market']['symbol']
    pos_side = position['side']
    entry_price = float(position.get('entryPrice', 0))
    contracts = float(position.get('contracts', 0))
    if contracts == 0:
        return

    triggers = exchange.fetch_open_trigger_orders(symbol)
    trigger_ids = {o['id'] for o in triggers}

    tracker = read_tracker(tracker_path)
    tp_ids = set(tracker.get('take_profit_ids', []))
    sl_ids = set(tracker.get('stop_loss_ids', []))

    # Wenn active_genome, IDs und last_side alle leer: jungfräulicher Tracker —
    # diese TF-Instanz hat die Position nicht eröffnet (anderer TF-Bot für dasselbe Symbol).
    if not tracker.get('active_genome') and not tp_ids and not sl_ids and not tracker.get('last_side'):
        logger.debug(f"Self-Repair übersprungen ({symbol}): kein aktiver Trade in diesem TF-Bot-Tracker.")
        return

    def _trig_price(o: dict) -> float:
        info = o.get('info', {})
        raw = (o.get('stopPrice') or o.get('triggerPrice')
               or info.get('triggerPrice')
               or info.get('planPrice')
               or info.get('trailingTriggerPrice')
               or info.get('movingPrice') or 0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    # --- SL: ID-Check, dann Preis-Richtungs-Fallback ---
    if sl_ids:
        sl_exists = bool(sl_ids & trigger_ids)
        logger.info(f"SL ID-Check: {sl_exists} ({len(trigger_ids)} offene Trigger-Orders)")
    else:
        sl_exists = any(
            (pos_side == 'long' and _trig_price(o) < entry_price and _trig_price(o) > 0) or
            (pos_side == 'short' and _trig_price(o) > entry_price and entry_price > 0)
            for o in triggers
        )
        logger.info(f"SL Preis-Fallback: {sl_exists} ({len(triggers)} Trigger-Orders)")

    # --- TP (Trailing Stop): Sichtbarkeits-bewusstes ID-Tracking ---
    # tsl_api_visible=False → Bitget gibt track_plan nie zurück → IDs vertrauen.
    # tsl_api_visible=True oder None → normaler ID-Check; nicht gefunden → Repair.
    close_side_tp = 'sell' if pos_side == 'long' else 'buy'
    if tp_ids:
        if tp_ids & trigger_ids:
            # TSL in API sichtbar und gefunden
            tp_exists = True
            logger.info(f"TP ID-Check: True (ID in Trigger-Orders sichtbar)")
        elif tracker.get('tsl_api_visible') is False:
            # Bekannt API-unsichtbar seit Platzierung → IDs vertrauen
            tp_exists = True
            logger.info(f"TP ID gespeichert, bekannt API-unsichtbar — als aktiv angenommen (Exchange verwaltet)")
        else:
            # tsl_api_visible=True oder None → ID nicht gefunden → Repair nötig
            tp_exists = False
            logger.info(f"TP ID gespeichert aber nicht in API gefunden (tsl_api_visible={tracker.get('tsl_api_visible')!r}) → Repair")
    else:
        # Keine gespeicherten IDs (Entry-Fehler oder Tracker-Reset) → Preis-Fallback
        tp_exists = any(
            (o.get('side', '').lower() == close_side_tp) and (
                (pos_side == 'long' and _trig_price(o) > entry_price and entry_price > 0) or
                (pos_side == 'short' and _trig_price(o) < entry_price and _trig_price(o) > 0)
            )
            for o in triggers
        )
        logger.info(f"TP Preis-Fallback (keine IDs): {tp_exists} ({len(triggers)} Trigger-Orders)")

    if sl_exists and tp_exists:
        return

    logger.warning(f"Self-Repair: SL={sl_exists} TP={tp_exists} für {symbol}")

    # --- Preise für Repair: Original-Trade-Parameter haben Vorrang ---
    # active_genome enthält die Preise vom ursprünglichen Entry → diese verwenden.
    # Fallback auf aktuelles Signal nur wenn active_genome leer (z.B. nach Tracker-Reset).
    active_genome = tracker.get('active_genome') or {}
    tp_price = active_genome.get('tp_price') or (genome_signal.get('tp_price') if genome_signal else None)
    sl_price = active_genome.get('sl_price') or (genome_signal.get('sl_price') if genome_signal else None)

    # TP-Preis aus Entry + SL rekonstruieren wenn unbekannt
    if not tp_price and sl_price and entry_price > 0:
        rr = float(params.get('risk', {}).get('rr_ratio', 2.0))
        sl_dist = abs(entry_price - float(sl_price))
        tp_price = (entry_price + rr * sl_dist) if pos_side == 'long' else (entry_price - rr * sl_dist)
        logger.warning(f"TP-Preis rekonstruiert aus Entry/SL (R:R {rr}:1): {tp_price:.6f}")

    trailing_callback = params['risk'].get('trailing_callback_rate_pct', 1.0) / 100.0
    new_tp_ids = list(tp_ids)
    new_sl_ids = list(sl_ids)
    repaired = []

    if not tp_exists:
        if tp_price and contracts > 0:
            trail_side = 'sell' if pos_side == 'long' else 'buy'
            try:
                o = exchange.place_trailing_stop_order(symbol, trail_side, contracts, float(tp_price), trailing_callback)
                if o and 'id' in o:
                    new_tp_ids = [o['id']]
                    # Sichtbarkeit direkt nach Platzierung prüfen
                    time.sleep(0.3)
                    verify_triggers = exchange.fetch_open_trigger_orders(symbol)
                    tsl_visible = o['id'] in {v['id'] for v in verify_triggers}
                    tracker['tsl_api_visible'] = tsl_visible
                    logger.info(f"Trailing Stop nachgetragen (Aktivierung @ {tp_price:.4f}, Callback {trailing_callback*100:.1f}%) — "
                                f"API-sichtbar: {tsl_visible}")
                repaired.append(f"TP@{float(tp_price):.4f}")
            except Exception as e:
                logger.error(f"TP-Reparatur fehlgeschlagen: {e}", exc_info=True)
        else:
            logger.error("TP fehlt aber Preis unbekannt — manuelle Intervention nötig!")
            send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                         f"🚨 dnabot ALARM ({symbol}): TP fehlt, Preis unbekannt — manuelle Intervention!")

    if not sl_exists:
        if sl_price and contracts > 0:
            sl_side = 'sell' if pos_side == 'long' else 'buy'
            try:
                o = exchange.place_trigger_market_order(symbol, sl_side, contracts, float(sl_price), reduce=True)
                if o and 'id' in o:
                    new_sl_ids = [o['id']]
                repaired.append(f"SL@{float(sl_price):.4f}")
                logger.info(f"SL nachgetragen @ {sl_price:.4f}")
            except Exception as e:
                logger.error(f"SL-Reparatur fehlgeschlagen: {e}", exc_info=True)
        else:
            logger.error("SL fehlt aber Preis unbekannt — manuelle Intervention nötig!")
            send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                         f"🚨 dnabot ALARM ({symbol}): SL fehlt, Preis unbekannt — manuelle Intervention!")

    tracker['take_profit_ids'] = new_tp_ids
    tracker['stop_loss_ids'] = new_sl_ids
    _write_tracker(tracker_path, tracker)

    if repaired:
        send_message(
            telegram_config.get('bot_token'), telegram_config.get('chat_id'),
            f"🔧 dnabot Self-Repair ({symbol}): {', '.join(repaired)} automatisch nachgetragen."
        )


# ─── Housekeeper ─────────────────────────────────────────────────────────────

def housekeeper_routine(exchange: Exchange, symbol: str, logger: logging.Logger) -> bool:
    """Räumt verbleibende TP/SL-Orders auf der Exchange auf (analog stbot).
    Wird aufgerufen wenn keine offene Position mehr existiert."""
    try:
        logger.info(f"Housekeeper: Starte Aufräumroutine für {symbol}...")
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(1)

        # Sicherheitsnetz: verwaiste Position schließen falls doch noch eine offen ist
        position = exchange.fetch_open_positions(symbol)
        if position:
            pos_info = position[0]
            close_side = 'sell' if pos_info['side'] == 'long' else 'buy'
            logger.warning(f"Housekeeper: Verwaiste Position ({pos_info['side']}) — schließe...")
            exchange.place_market_order(symbol, close_side, float(pos_info['contracts']), reduce=True)
            time.sleep(3)

        if exchange.fetch_open_positions(symbol):
            logger.error("Housekeeper: Position konnte nicht geschlossen werden!")
        else:
            logger.info(f"Housekeeper: {symbol} ist sauber.")
        return True
    except Exception as e:
        logger.error(f"Housekeeper-Fehler: {e}", exc_info=True)
        return False


# ─── Chart-Generierung: Entry-Kerzendiagramm ─────────────────────────────────

def _generate_entry_chart_png(df: pd.DataFrame, signal: dict,
                               symbol: str, timeframe: str,
                               n_candles: int = 40) -> str:
    """
    Zeichnet Kerzendiagramm mit hervorgehobenem SL-Sequenzfenster und
    Entry/SL/TP(Trailing-Aktivierung)-Tags. Gibt Pfad zur temporaeren
    PNG-Datei zurueck (oder None bei Fehler).
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return None

    if df is None or df.empty:
        return None

    display_df = df[['open', 'high', 'low', 'close']].iloc[-n_candles:].reset_index(drop=True)
    n = len(display_df)
    if n == 0:
        return None

    opens  = display_df['open'].values
    highs  = display_df['high'].values
    lows   = display_df['low'].values
    closes = display_df['close'].values

    side         = signal.get('side', 'long')
    entry_price  = signal.get('entry_price', closes[-1])
    sl_price     = signal.get('sl_price', 0.0)
    tp_price     = signal.get('tp_price', 0.0)
    seq_length   = int(signal.get('seq_length', 5))
    risk_gene_id = (signal.get('risk_gene_id') or '')[:8]
    rr_ratio     = signal.get('gene_rr_ratio', 0.0)
    trailing_pct = signal.get('gene_trailing_pct', 0.0)

    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor('#0d1117')
    ax.set_facecolor('#0d1117')

    bar_w = 0.6

    # 1. Y-Limits (vor dem Zeichnen berechnen)
    y_min = float(lows.min())
    y_max = float(highs.max())
    for p in filter(None, [entry_price, sl_price, tp_price]):
        y_min = min(y_min, float(p) * 0.999)
        y_max = max(y_max, float(p) * 1.001)
    margin = (y_max - y_min) * 0.14
    y_lo, y_hi = y_min - margin, y_max + margin
    ax.set_xlim(-1, n + 1)
    ax.set_ylim(y_lo, y_hi)

    def _in_range(price):
        return y_lo < float(price) < y_hi

    # 2. SL-Fenster-Hintergrund (letzte seq_length Kerzen bestimmen den SL)
    pat_start = max(0, n - seq_length - 1)
    pat_end   = n - 1
    pat_color = '#00e676' if side == 'long' else '#ff1744'
    ax.axvspan(pat_start - 0.5, pat_end + 0.5,
               color=pat_color, alpha=0.08, zorder=1)
    ax.annotate('', xy=(pat_end + 0.5, y_lo), xytext=(pat_start - 0.5, y_lo),
                arrowprops=dict(arrowstyle='-', color=pat_color, lw=0.8, alpha=0.5))
    mid_x = (pat_start + pat_end) / 2
    ax.text(mid_x, y_lo + (y_hi - y_lo) * 0.01,
            f'SL-Fenster (seq_len={seq_length})', color=pat_color, fontsize=7,
            ha='center', va='bottom', fontfamily='monospace', alpha=0.85, zorder=7)

    # 3. Kerzen
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        in_pat = pat_start <= i <= pat_end
        color = ('#26a69a' if c >= o else '#ef5350')
        if in_pat:
            color = ('#4cceac' if c >= o else '#ff6b6b')
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        body_h = max(abs(c - o), (h - l) * 0.005)
        ax.add_patch(mpatches.FancyBboxPatch(
            (i - bar_w / 2, min(o, c)), bar_w, body_h,
            boxstyle="square,pad=0", linewidth=0, facecolor=color, zorder=3,
        ))

    # 4. Risiko/Reward-Zonen
    if sl_price and _in_range(sl_price):
        ax.axhspan(min(sl_price, entry_price), max(sl_price, entry_price),
                   color='#ff1744', alpha=0.07, zorder=1)
    if tp_price and _in_range(tp_price):
        ax.axhspan(min(tp_price, entry_price), max(tp_price, entry_price),
                   color='#00c853', alpha=0.07, zorder=1)

    # 5. Entry/SL/TP Preis-Tags
    def _price_tag(price, label, color, lw=1.5, ls='--'):
        if not price or not _in_range(price):
            return
        ax.axhline(price, color=color, linewidth=lw, linestyle=ls, zorder=6)
        ax.text(n - 0.3, price, f'  {label}: {price:.6g}  ',
                color='#0d1117', fontsize=8.5, va='center', ha='right',
                fontweight='bold', zorder=8,
                bbox=dict(facecolor=color, edgecolor='none', alpha=0.92,
                          boxstyle='square,pad=0.25'))

    _price_tag(tp_price,    'Trail-Akt.', '#00c853')
    _price_tag(entry_price, 'Entry',      '#ffd700')
    _price_tag(sl_price,    'SL',         '#ff1744')

    # 6. Infobox oben links
    side_label = 'LONG ▲' if side == 'long' else 'SHORT ▼'
    info_lines = [
        f"{side_label}   RR 1:{rr_ratio:.1f}   Trail: {trailing_pct:.1f}%",
        f"Risiko-Gen: {risk_gene_id}...",
    ]
    ax.text(0.01, 0.98, '\n'.join(info_lines),
            transform=ax.transAxes, fontsize=8, va='top', ha='left',
            color='#cccccc', fontfamily='monospace',
            bbox=dict(facecolor='#1a2332', edgecolor='#2a3a4a',
                      alpha=0.88, boxstyle='round,pad=0.5'),
            zorder=9)

    # 7. Styling
    ax.set_title(
        f"DNABOT  |  {symbol}  {timeframe}  |  {side_label}  |  letzte {n} Kerzen",
        color='#e0e0e0', fontsize=11, pad=10,
    )
    ax.tick_params(colors='#888888', labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor('#2a3a4a')
    ax.set_xticks([])
    ax.yaxis.tick_right()
    ax.grid(axis='y', color='#1e2a3a', linewidth=0.4, zorder=0)
    plt.tight_layout()

    tmp_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    from datetime import timezone
    ts       = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    sym_safe = symbol.replace('/', '-').replace(':', '-')
    path     = os.path.join(tmp_dir, f'entry_{sym_safe}_{timeframe}_{ts}.png')
    fig.savefig(path, dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def _send_entry_chart(df: pd.DataFrame, signal: dict, symbol: str,
                       timeframe: str, telegram_config: dict, logger: logging.Logger):
    """Generiert Entry-Chart-PNG und sendet es via Telegram."""
    if not telegram_config or not telegram_config.get('bot_token') or not telegram_config.get('chat_id'):
        return
    try:
        path = _generate_entry_chart_png(df, signal, symbol, timeframe)
        if path and os.path.exists(path):
            side_label = 'LONG' if signal.get('side') == 'long' else 'SHORT'
            ep = signal.get('entry_price', 0)
            sl = signal.get('sl_price', 0)
            tp = signal.get('tp_price', 0)
            caption = (
                f"DNABOT | {symbol} ({timeframe})\n"
                f"{side_label} @ {ep:.6g}  |  SL: {sl:.6g}  |  Trail-Akt.: {tp:.6g}\n"
                f"Risiko-Gen: {(signal.get('risk_gene_id') or '')[:8]}..."
            )
            send_photo(telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                       path, caption)
            os.remove(path)
    except Exception as e:
        logger.warning(f"Entry-Chart senden fehlgeschlagen: {e}")


# ─── Entry Orders ─────────────────────────────────────────────────────────────

def place_entry_orders(
    exchange: Exchange,
    genome_signal: dict,
    params: dict,
    balance: float,
    tracker_path: str,
    telegram_config: dict,
    logger: logging.Logger,
    df: pd.DataFrame = None,
):
    """
    Platziert einen Entry-Trade basierend auf dem momentum_exit-Signal.

    Entry: Market-Order (sofort, da Signalkerze bereits abgeschlossen ist)
    SL: Aus dem seq_len-Fenster (Low/High der letzten Kerzen)
    Trailing-Aktivierung: RR * SL-Distanz vom Entry (siehe momentum_exit_logic.py)
    """
    symbol = params['market']['symbol']
    side = genome_signal.get('side')

    if side is None:
        logger.info("Kein Signal → kein Trade.")
        return

    # Nur EIN Entry pro Timeframe-Kerze: das Signal bleibt fuer die gesamte
    # Kerzendauer identisch (Richtung = Kerzen-eigene Richtung), ohne dieses
    # Gate wuerde jeder Cronjob-Durchlauf innerhalb derselben Kerze erneut
    # einsteigen -- fruehers durch den inzwischen entfernten SL-Cooldown
    # (Commit c2bfd2f) implizit verhindert.
    candle_time = genome_signal.get('candle_time')
    if candle_time:
        tracker_pre = read_tracker(tracker_path)
        if tracker_pre.get('last_entry_candle_time') == candle_time:
            logger.info(f"Bereits in dieser Kerze gehandelt ({candle_time}) — kein zweiter Entry.")
            return

    if side == 'long' and not params.get('behavior', {}).get('use_longs', True):
        logger.info("Longs deaktiviert.")
        return
    if side == 'short' and not params.get('behavior', {}).get('use_shorts', True):
        logger.info("Shorts deaktiviert.")
        return

    risk = params['risk']
    leverage = risk['leverage']
    risk_pct = risk.get('risk_per_entry_pct', 1.0)
    trailing_callback = risk.get('trailing_callback_rate_pct', 1.0) / 100.0

    # Risiko-Reduktion bei schlechter Performance
    skip, reason = should_skip_trading(tracker_path)
    if skip:
        logger.warning(f"Trading pausiert: {reason}")
        return

    entry_price = genome_signal['entry_price']
    sl_price = genome_signal['sl_price']
    tp_price = genome_signal['tp_price']
    sl_pct = genome_signal['sl_pct']

    if sl_pct <= 0:
        logger.warning("SL-Distanz = 0. Überspringe.")
        return

    # Positionsgröße: risikiertes Kapital / SL-Distanz
    sl_distance_price = abs(entry_price - sl_price)
    risk_amount_usd = balance * (risk_pct / 100.0)
    amount_coins = risk_amount_usd / sl_distance_price

    # Notional-Cap: max. 200.000 USDT pro Trade
    notional_uncapped = amount_coins * entry_price
    if notional_uncapped > MAX_NOTIONAL_USDT:
        amount_coins = MAX_NOTIONAL_USDT / entry_price
        logger.info(f"Notional-Cap: {notional_uncapped:.0f} → {MAX_NOTIONAL_USDT:.0f} USDT ({amount_coins:.6f} Kontrakte)")

    # Mindest-Checks
    min_amount = exchange.fetch_min_amount_tradable(symbol)
    if amount_coins < min_amount:
        logger.warning(f"Menge {amount_coins:.6f} unter Minimum {min_amount:.6f}. Überspringe.")
        return

    notional = amount_coins * entry_price
    if notional < MIN_NOTIONAL_USDT:
        logger.warning(f"Notional {notional:.2f} USDT unter Minimum {MIN_NOTIONAL_USDT} USDT. Überspringe.")
        return

    # Margin und Leverage setzen
    try:
        exchange.set_margin_mode(symbol, risk.get('margin_mode', 'isolated'))
        time.sleep(0.3)
        exchange.set_leverage(symbol, leverage, risk.get('margin_mode', 'isolated'))
        time.sleep(0.3)
    except Exception as e:
        logger.warning(f"Konnte Margin/Leverage nicht setzen: {e}")

    if side == 'long':
        order_side = 'buy'
        tp_side = sl_side = 'sell'
    else:
        order_side = 'sell'
        tp_side = sl_side = 'buy'

    logger.info(
        f"[Entry] {side.upper()} {amount_coins:.6f} {symbol} | "
        f"Market @ ~{entry_price:.4f} | SL={sl_price:.4f} ({sl_pct:.2f}%) | "
        f"Trail-Akt.={tp_price:.4f} | Gen={(genome_signal.get('risk_gene_id') or '')[:8]}"
    )

    new_tp_ids = []
    new_sl_ids = []

    # 1. Entry Market-Order zuerst (wie stbot) — keine Zombie-Trigger-Orders bei Fehler
    try:
        exchange.place_market_order(symbol, order_side, amount_coins, reduce=False,
                                    margin_mode=risk.get('margin_mode', 'isolated'))
        logger.info(f"Entry Market-Order platziert: {order_side.upper()} @ ~{entry_price:.4f}")
    except ccxt.InsufficientFunds as e:
        logger.error(f"Nicht genug Guthaben: {e}")
        return
    except Exception as e:
        logger.error(f"Fehler beim Entry: {e}", exc_info=True)
        return

    # 2. Position bestätigen und echte Kontrakte/Fill-Preis holen
    time.sleep(2)
    open_positions = exchange.fetch_open_positions(symbol)
    if not open_positions:
        logger.error(f"Entry gesendet aber keine offene Position gefunden — abgebrochen.")
        return

    pos_info = open_positions[0]
    actual_contracts = float(pos_info['contracts'])
    actual_entry = float(pos_info.get('entryPrice') or entry_price)
    logger.info(f"Position bestätigt: {side.upper()} {actual_contracts:.6f} Kontr. @ {actual_entry:.4f}")

    # Trailing-Aktivierungspreis an den ECHTEN Fill-Preis anpassen: sl_price ist
    # strukturell (Low/High des seq_len-Fensters, unabhaengig vom Entry) und bleibt
    # unveraendert, aber tp_price wurde relativ zum THEORETISCHEN Signal-Entry
    # (Kerzenschluss zum Signalzeitpunkt) berechnet. Weicht der echte Market-Order-
    # Fill davon ab (Slippage/Zeitverzug bis zur Ausfuehrung), verschiebt sich sonst
    # das tatsaechlich realisierte R:R relativ zum Gen-Wert -- Neuberechnung mit dem
    # echten Entry haelt das Ziel-R:R korrekt (siehe Live-Vorfall XRP/USDT 2026-08-26:
    # Entry driftete von 1.4114 auf 1.3716, reales R:R fiel von 1:1.5 auf 1:0.83).
    rr_ratio = risk.get('rr_ratio', 2.0)
    sl_distance_actual = abs(actual_entry - sl_price)
    if side == 'long':
        tp_price = actual_entry + rr_ratio * sl_distance_actual
    else:
        tp_price = actual_entry - rr_ratio * sl_distance_actual

    # genome_signal synchron halten -- Entry-Chart und Tracker lesen direkt aus
    # dem Dict, nicht aus den lokalen Variablen. Ohne das wuerde der Chart
    # weiterhin den theoretischen (nicht den echten) Entry- und Trail-Preis
    # zeigen (siehe Live-Chart XRP/USDT 2026-08-26, wo genau das auffiel).
    genome_signal['entry_price'] = actual_entry
    genome_signal['tp_price'] = tp_price

    # 3. SL und Trailing Stop mit echten Kontrakten platzieren
    try:
        sl_order = exchange.place_trigger_market_order(symbol, sl_side, actual_contracts, sl_price, reduce=True)
        if sl_order and 'id' in sl_order:
            new_sl_ids.append(sl_order['id'])
        logger.info(f"SL gesetzt @ {sl_price:.4f}")
        time.sleep(0.2)

        tp_order = exchange.place_trailing_stop_order(symbol, tp_side, actual_contracts, tp_price, trailing_callback)
        tsl_api_visible = False
        if tp_order and 'id' in tp_order:
            new_tp_ids.append(tp_order['id'])
            # Sichtbarkeit direkt nach Platzierung prüfen (bestimmt künftiges Verhalten von ensure_tp_sl)
            time.sleep(0.3)
            verify_trig = exchange.fetch_open_trigger_orders(symbol)
            tsl_api_visible = tp_order['id'] in {v['id'] for v in verify_trig}
        logger.info(f"Trailing Stop gesetzt (Aktivierung @ {tp_price:.4f}, Callback {trailing_callback*100:.1f}%) — "
                    f"API-sichtbar: {tsl_api_visible}")

    except Exception as e:
        logger.error(f"SL/TP-Placement fehlgeschlagen: {e}", exc_info=True)
        for oid in new_tp_ids + new_sl_ids:
            try:
                exchange.cancel_trigger_order(oid, symbol)
            except Exception:
                pass
        logger.warning("Schließe Position via Housekeeper da SL/TP nicht gesetzt werden konnten.")
        housekeeper_routine(exchange, symbol, logger)
        return

    # Tracker aktualisieren (Signal-Info für Self-Learning)
    tracker = read_tracker(tracker_path)
    tracker['stop_loss_ids'] = new_sl_ids
    tracker['take_profit_ids'] = new_tp_ids
    tracker['tsl_api_visible'] = tsl_api_visible
    tracker['last_side'] = side
    tracker['status'] = 'ok_to_trade'
    tracker['last_notified_entry_price'] = actual_entry
    tracker['last_notified_side'] = side
    tracker['last_entry_candle_time'] = genome_signal.get('candle_time')
    tracker['active_genome'] = {
        "direction": side.upper(),
        "seq_length": genome_signal['seq_length'],
        "entry_price": actual_entry,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "is_momentum_exit": genome_signal.get('is_momentum_exit', False),
        "risk_gene_id": genome_signal.get('risk_gene_id'),
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }
    _write_tracker(tracker_path, tracker)

    logger.info(f"Entry-Orders erfolgreich platziert für {symbol} ({side.upper()}).")

    # --- Telegram-Benachrichtigung ---
    try:
        timeframe   = params['market']['timeframe']
        direction_emoji = "🟢" if side == 'long' else "🔴"
        sl_dist_pct = abs(actual_entry - sl_price) / actual_entry * 100
        tp_dist_pct = abs(tp_price - actual_entry) / actual_entry * 100
        rr_ratio    = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
        risk_usdt   = balance * risk_pct / 100.0
        msg = (
            f"🚀 dnabot SIGNAL: {symbol} ({timeframe})\n"
            f"{'─' * 32}\n"
            f"{direction_emoji} Richtung: {side.upper()}\n"
            f"💰 Entry:        ${actual_entry:.6f}\n"
            f"🛑 SL:           ${sl_price:.6f} (-{sl_dist_pct:.2f}%)\n"
            f"🎯 Trailing (ab): ${tp_price:.6f} (+{tp_dist_pct:.2f}%)\n"
            f"🔁 Callback:     {trailing_callback*100:.1f}%\n"
            f"📊 Min R:R:      1:{rr_ratio:.1f}\n"
            f"⚙️ Hebel:        {leverage}x\n"
            f"🛡️ Risiko:       {risk_pct:.1f}% ({risk_usdt:.2f} USDT)\n"
            f"📦 Kontr.:       {actual_contracts:.4f}\n"
            f"{'─' * 32}\n"
            f"🧬 Risiko-Gen:  {(genome_signal.get('risk_gene_id') or '')[:8]}..."
        )
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
    except Exception as e:
        logger.warning(f"Telegram-Benachrichtigung fehlgeschlagen: {e}")

    # Chart per Telegram senden
    _send_entry_chart(df, genome_signal, symbol, timeframe, telegram_config, logger)


# ─── Self-Learning Update ─────────────────────────────────────────────────────

def self_learn_from_closed_trade(
    tracker_path: str, outcome: str,
    exit_price: float, logger: logging.Logger, risk_db: RiskGenomeDB = None,
):
    """
    Aktualisiert das aktive Risiko-Gen in der RiskGenomeDB (Calmar-Tracking,
    siehe genome/risk_genome_db.py) nach einem abgeschlossenen Trade.
    Wird aufgerufen wenn SL oder TP/Trailing-Stop ausgeloest wurde.
    """
    tracker = read_tracker(tracker_path)
    active_genome = tracker.get('active_genome')

    if not active_genome:
        return

    entry_price = active_genome.get('entry_price', 0)
    direction = active_genome.get('direction', 'LONG')

    if entry_price > 0 and exit_price > 0:
        if direction == 'LONG':
            actual_move_pct = (exit_price - entry_price) / entry_price * 100
        else:
            actual_move_pct = (entry_price - exit_price) / entry_price * 100
    else:
        actual_move_pct = 0.0

    risk_gene_id = active_genome.get('risk_gene_id')
    sl_price = active_genome.get('sl_price', 0)
    if risk_db is not None and risk_gene_id and entry_price > 0 and sl_price:
        sl_pct = abs(entry_price - sl_price) / entry_price * 100.0
        risk_db.record_trade(
            risk_gene_id=risk_gene_id,
            entry_time=active_genome.get('entry_time', datetime.now(timezone.utc).isoformat()),
            exit_time=datetime.now(timezone.utc).isoformat(),
            outcome=outcome,
            pnl_pct=actual_move_pct,
            sl_pct=sl_pct,
            source='live',
        )
        logger.info(f"[RiskGenomeDB] Gen {risk_gene_id} aktualisiert: {outcome}, "
                    f"pnl={actual_move_pct:+.2f}%")
    else:
        logger.warning("Trade abgeschlossen, aber kein risk_gene_id/sl_price "
                       "im Tracker -- kein Self-Learning fuer dieses Risiko-Gen.")

    tracker['active_genome'] = None
    _write_tracker(tracker_path, tracker)


def _close_dbs(risk_db: RiskGenomeDB):
    if risk_db is not None:
        risk_db.close()


# ─── Haupt-Trading-Zyklus ─────────────────────────────────────────────────────

def full_trade_cycle(
    exchange: Exchange,
    params: dict,
    telegram_config: dict,
    logger: logging.Logger,
):
    """
    Vollständiger Handelszyklus für dnabot:

    1. OHLCV-Daten laden
    2. momentum_exit-Signal berechnen (aktives Risiko-Gen aus RiskGenomeDB)
    3. SL/TP-Trigger prüfen + Self-Learning
    4. Alte Entry-Orders stornieren
    5. Offene Position verwalten ODER neue Entry platzieren
    """
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    tracker_path = get_tracker_file_path(symbol, timeframe)

    # Markt in Tracker schreiben für Self-Learning
    tracker = read_tracker(tracker_path)
    tracker['market'] = symbol
    tracker['timeframe'] = timeframe
    _write_tracker(tracker_path, tracker)

    # 1. OHLCV laden
    logger.info(f"Lade {FETCH_LIMIT} Kerzen für {symbol} ({timeframe})...")
    df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=FETCH_LIMIT)
    if df is None or len(df) < 50:
        logger.error(f"Zu wenig Daten ({len(df) if df is not None else 0}). Abbruch.")
        return

    # 2. Signal -- nicht-praediktiver Momentum-Einstieg (Richtung = eigene
    # Kerzenrichtung), Risiko/Exit-Parameter kommen aus dem aktiven Risiko-Gen
    # der RiskGenomeDB (siehe genome/risk_genome_db.py, momentum_exit_logic.py).
    risk_db = RiskGenomeDB(RISK_DB_PATH)
    genome_signal = get_momentum_exit_signal(df, params, db=risk_db)
    if genome_signal:
        # Aktives Risiko-Gen bestimmt live rr_ratio/trailing/risk -- ueberschreibt
        # die statische settings.json-Konfiguration fuer DIESEN Zyklus (params
        # wird pro Cronjob-Lauf frisch aus settings.json gebaut, kein Bleed-Over).
        params = dict(params)
        params['risk'] = {**params['risk'],
                           'rr_ratio': genome_signal['gene_rr_ratio'],
                           'trailing_callback_rate_pct': genome_signal['gene_trailing_pct'],
                           'risk_per_entry_pct': genome_signal['gene_risk_pct']}
        logger.info(f"Momentum-Exit Signal: {genome_signal['side'].upper()} "
                    f"(Gen {genome_signal['risk_gene_id']})")
    else:
        logger.info("Kein Momentum-Exit-Signal (deaktiviert, zu wenig Kerzen, "
                    "oder kein aktives Risiko-Gen fuer dieses Pair/Timeframe).")

    current_price = float(df['close'].iloc[-1])

    # 3. Entry-Orders stornieren (SL/TP bleiben durch protected_ids + reduceOnly geschützt)
    cancel_entry_orders(exchange, symbol, logger, tracker_path)

    # 4. Position prüfen
    open_positions = exchange.fetch_open_positions(symbol)

    if open_positions:
        position = open_positions[0]
        logger.info(f"Offene Position: {position.get('side')} @ {position.get('entryPrice')}")

        try:
            exchange.set_margin_mode(symbol, params['risk'].get('margin_mode', 'isolated'))
            exchange.set_leverage(symbol, params['risk']['leverage'], params['risk'].get('margin_mode', 'isolated'))
        except Exception:
            pass

        notify_new_position(exchange, position, params, tracker_path, telegram_config, logger)
        ensure_tp_sl(exchange, position, genome_signal, params, tracker_path, telegram_config, logger)

        # --- Preis-Overshoot-Check: Position schließen falls Preis SL bereits überschritten ---
        # Nur fuer SL sinnvoll: das ist ein harter Stop, ein Ueberschreiten ohne Exit ist ein
        # echter Notfall. TP ist dagegen nur der Aktivierungstrigger fuer den nativen Bitget
        # Trailing Stop (siehe place_trailing_stop_order) - der Preis soll TP absichtlich
        # ueberschreiten, damit der Trailing Stop den Trend weiter mitnehmen kann. Ein
        # Force-Close dort wuerde profitable Trades genau beim Aktivieren abwuergen statt sie
        # laufen zu lassen (siehe BCH-Vorfall 2026-07-09).
        try:
            ov_tracker = read_tracker(tracker_path)
            ov_genome = ov_tracker.get('active_genome') or {}
            sl_price_ov = ov_genome.get('sl_price')
            pos_side_ov = position.get('side', 'long')
            contracts_ov = float(position.get('contracts', 0))
            close_side_ov = 'sell' if pos_side_ov == 'long' else 'buy'

            if sl_price_ov and contracts_ov > 0 and current_price > 0:
                sl_val = float(sl_price_ov)
                breached = (current_price <= sl_val) if pos_side_ov == 'long' else (current_price >= sl_val)
                if breached:
                    logger.warning(
                        f"Preis-Overshoot: {current_price:.6f} hat SL "
                        f"({sl_val:.6f}) überschritten — schließe {symbol} per Market."
                    )
                    try:
                        exchange.cancel_all_orders_for_symbol(symbol)
                    except Exception:
                        pass
                    exchange.place_market_order(symbol, close_side_ov, contracts_ov, reduce=True)
                    time.sleep(2)
                    remaining = exchange.fetch_open_positions(symbol)
                    if not remaining:
                        _write_tracker(tracker_path, {})
                        logger.info(f"Overshoot-Schließung {symbol} erfolgreich — Tracker geleert.")
                    else:
                        logger.error(f"Overshoot-Schließung {symbol}: Position noch offen!")
                    send_message(
                        telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                        f"⚡ dnabot NOTSCHLIESSUNG ({symbol}): Preis {current_price:.6f} hat "
                        f"SL ({sl_val:.6f}) überschritten. "
                        f"Position per Market geschlossen."
                    )
        except Exception as e:
            logger.error(f"Fehler beim Preis-Overshoot-Check: {e}")

    else:
        # Position weg — Exchange aufräumen (verbleibende TP/SL-Orders stornieren)
        housekeeper_routine(exchange, symbol, logger)

        # Position weg — prüfen ob ein aktiver Trade im Tracker war
        tracker = read_tracker(tracker_path)
        had_tp_ids = bool(tracker.get('take_profit_ids'))
        had_sl_ids = bool(tracker.get('stop_loss_ids'))

        if had_tp_ids or had_sl_ids:
            # Trade wurde geschlossen — echte Ausführung via API ermitteln
            active_genome = tracker.get('active_genome') or {}
            entry_price = active_genome.get('entry_price', 0)
            last_side = tracker.get('last_side', 'long')
            sl_price = active_genome.get('sl_price', 0)

            # Bitget Trailing Stop führt eine Market-Order aus → in fetchClosedOrders(stop=False)
            fill_price = None
            outcome = None
            try:
                closed_orders = exchange.fetch_recent_closed_market_orders(symbol, limit=10)
                reduce_fills = [
                    o for o in closed_orders
                    if o.get('reduceOnly') and o.get('status') in ('closed', 'filled')
                    and float(o.get('filled', 0) or 0) > 0
                ]
                if reduce_fills:
                    # Neueste Ausführung nehmen
                    latest = max(reduce_fills, key=lambda o: o.get('timestamp') or 0)
                    fill_price = float(latest.get('average') or latest.get('price') or 0)
                    logger.info(f"Trailing Stop-Ausführung gefunden: fill @ {fill_price:.6f}")
            except Exception as e:
                logger.error(f"Fehler beim Abrufen der Trailing-Stop-Ausführung: {e}")

            if fill_price and fill_price > 0 and entry_price > 0:
                if last_side == 'long':
                    outcome = 'win' if fill_price > entry_price else 'loss'
                else:
                    outcome = 'win' if fill_price < entry_price else 'loss'
                reason = "Trailing Stop"
                logger.info(
                    f"Trade geschlossen via {reason} → {'WIN' if outcome == 'win' else 'LOSS'} "
                    f"(Entry: {entry_price:.6f}, Fill: {fill_price:.6f})"
                )
            elif entry_price > 0 and sl_price > 0:
                # Kein Fill gefunden → SL-Preis aus Tracker als Fallback
                if last_side == 'long':
                    outcome = 'loss' if current_price <= sl_price * 1.005 else 'win'
                else:
                    outcome = 'loss' if current_price >= sl_price * 0.995 else 'win'
                reason = "Stop Loss" if outcome == 'loss' else "Trailing Stop"
                logger.warning(
                    f"Kein Fill gefunden → Fallback SL-Preisvergleich → "
                    f"{'WIN' if outcome == 'win' else 'LOSS'}"
                )
            else:
                logger.warning("Trade geschlossen, aber weder Fill noch Entry/SL-Preis bekannt — kein Self-Learning.")

            if outcome:
                outcome_label = 'WIN' if outcome == 'win' else 'LOSS'
                record_trade_result(tracker_path, outcome, logger)
                try:
                    price_for_learning = fill_price if fill_price else current_price
                    self_learn_from_closed_trade(tracker_path, outcome_label, price_for_learning, logger, risk_db=risk_db)
                except Exception as e:
                    logger.error(f"Self-Learning Fehler: {e}")
                emoji = "✅" if outcome == 'win' else "🛑"
                try:
                    send_message(
                        telegram_config.get('bot_token'),
                        telegram_config.get('chat_id'),
                        f"{emoji} dnabot {reason}: {symbol} ({timeframe})\n"
                        f"Risiko-Gen aktualisiert → {outcome_label}"
                    )
                except Exception:
                    pass

            tracker = read_tracker(tracker_path)
            tracker.update({"stop_loss_ids": [], "take_profit_ids": [], "status": "ok_to_trade"})
            tracker.pop('last_notified_entry_price', None)
            tracker.pop('last_notified_side', None)
            _write_tracker(tracker_path, tracker)

            # Kein Sofort-Reentry im selben Zyklus: housekeeper_routine() hat
            # gerade erst Cancel-Requests fuer die alten SL/Trailing-Orders
            # abgeschickt (kein garantiertes sofortiges Settlement auf Bitget-
            # Seite). Ein place_entry_orders()-Aufruf in DERSELBEN Sekunde kann
            # dadurch auf einen Exchange-Zustand treffen, in dem die alte
            # Position/Trigger-Orders noch nicht vollständig abgewickelt sind --
            # neue Position landet dann faktisch ungeschuetzt bzw. mit stale
            # Trigger-Preisen (siehe Live-Vorfall AAVE/USDT 2026-08-25). Der
            # naechste Cronjob-Lauf (wenige Minuten spaeter) prueft ganz normal
            # erneut auf ein Signal, dann mit garantiert sauberem, abgeschlossenem
            # Exchange-Zustand.
            logger.info(f"Trade-Abschluss verarbeitet ({symbol}) — Reentry erst im naechsten Zyklus.")
            _close_dbs(risk_db)
            return

        balance = exchange.fetch_balance_usdt()
        logger.info(f"Guthaben: {balance:.2f} USDT")

        if balance < MIN_NOTIONAL_USDT:
            logger.warning(f"Guthaben zu niedrig ({balance:.2f} USDT).")
            _close_dbs(risk_db)
            return

        if genome_signal is None:
            logger.info("Kein Signal → kein Entry.")
            _close_dbs(risk_db)
            return
        place_entry_orders(exchange, genome_signal, params, balance, tracker_path, telegram_config, logger, df=df)

    _close_dbs(risk_db)
    logger.info(f"Trade-Zyklus abgeschlossen für {symbol} ({timeframe}).")
