# src/dnabot/utils/trade_manager.py
# Trade-Management für dnabot (Genome-basierte Signale)
#
# Unterschiede zu dbot/ltbbot:
#   - Signal kommt von genome_logic (nicht LSTM)
#   - SL = Low/High der Sequenz-Kerzen (nicht % vom Entry)
#   - Self-Learning: Nach Trade-Abschluss wird Genome in DB aktualisiert
#   - 1 Entry (kein 3-Layer-System)

import logging
import time
import json
import os
import sys
import ccxt
import pandas as pd
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker')

sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.utils.telegram import send_message, send_photo
from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.genome.scoring import kelly_risk_pct as _kelly_risk_pct
from dnabot.strategy.genome_logic import get_genome_signal, update_genome_with_trade_result
from dnabot.strategy.order_block_logic import get_order_block_signal

MIN_NOTIONAL_USDT = 5.0
MAX_NOTIONAL_USDT = 200_000.0   # Obergrenze Positionsgröße pro Trade


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


# ─── Chart-Generierung: Genome-Kerzendiagramm ────────────────────────────────

def _generate_genome_chart_png(df: pd.DataFrame, genome_signal: dict,
                                symbol: str, timeframe: str,
                                n_candles: int = 40) -> str:
    """
    Zeichnet Kerzendiagramm mit hervorgehobenem Genome-Muster und Entry/SL/TP-Tags.
    Gibt Pfad zur temporaeren PNG-Datei zurueck (oder None bei Fehler).
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

    side        = genome_signal.get('side', 'long')
    entry_price = genome_signal.get('entry_price', closes[-1])
    sl_price    = genome_signal.get('sl_price', 0.0)
    tp_price    = genome_signal.get('tp_price', 0.0)
    seq_length  = int(genome_signal.get('seq_length', 4))
    sequence    = genome_signal.get('sequence', '')
    score       = genome_signal.get('score', 0.0)
    winrate     = genome_signal.get('winrate', 0.0)
    occurrences = genome_signal.get('total_occurrences', 0)
    genome_id   = genome_signal.get('genome_id', '')[:8]
    regime      = genome_signal.get('regime', '')
    avg_move    = genome_signal.get('avg_move_pct', 0.0)
    gene_codes  = [g for g in sequence.split('|') if g]  # z.B. ['B3H-UH', 'S2H-DH', ...]

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

    # 2. Genome-Pattern-Hintergrund (letzte seq_length Kerzen)
    pat_start = max(0, n - seq_length - 1)
    pat_end   = n - 1
    pat_color = '#00e676' if side == 'long' else '#ff1744'
    ax.axvspan(pat_start - 0.5, pat_end + 0.5,
               color=pat_color, alpha=0.08, zorder=1)
    # Vertikale Klammer-Linie unten
    ax.annotate('', xy=(pat_end + 0.5, y_lo), xytext=(pat_start - 0.5, y_lo),
                arrowprops=dict(arrowstyle='-', color=pat_color, lw=0.8, alpha=0.5))
    # Sequenz-Label unterhalb
    mid_x = (pat_start + pat_end) / 2
    ax.text(mid_x, y_lo + (y_hi - y_lo) * 0.01,
            f'DNA: {sequence}', color=pat_color, fontsize=7,
            ha='center', va='bottom', fontfamily='monospace', alpha=0.85, zorder=7)

    # 3. Kerzen + Gene-Code-Labels über Pattern-Kerzen
    gene_start = n - seq_length   # erster Index der eigentlichen Pattern-Kerzen
    label_h    = (y_hi - y_lo) * 0.02
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
        # Gene-Code-Label über jeder Pattern-Kerze
        if gene_start <= i < gene_start + len(gene_codes):
            gene_idx = i - gene_start
            code = gene_codes[gene_idx]
            ax.text(i, h + label_h, code,
                    color=pat_color, fontsize=6.5, ha='center', va='bottom',
                    fontfamily='monospace', fontweight='bold',
                    rotation=0, zorder=8,
                    bbox=dict(facecolor='#0d1117', edgecolor=pat_color,
                              alpha=0.75, boxstyle='round,pad=0.15', linewidth=0.5))

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

    _price_tag(tp_price,    'TP',    '#00c853')
    _price_tag(entry_price, 'Entry', '#ffd700')
    _price_tag(sl_price,    'SL',    '#ff1744')

    # 6. Genome-Infobox oben links
    side_label = 'LONG ▲' if side == 'long' else 'SHORT ▼'
    sl_pct  = abs(entry_price - sl_price) / entry_price * 100 if sl_price else 0
    tp_pct  = abs(tp_price - entry_price) / entry_price * 100 if tp_price else 0
    rr      = tp_pct / sl_pct if sl_pct > 0 else 0
    regime_str = f"   [{regime}]" if regime else ""
    avg_str    = f"   AvgMove: {avg_move:.2f}%" if avg_move else ""
    info_lines = [
        f"{side_label}   R:R 1:{rr:.1f}{regime_str}",
        f"Score:   {score:.3f}   WR: {winrate:.1%}   n={occurrences}{avg_str}",
        f"Genome:  {genome_id}...",
    ]
    # Gene-Codes als Bedingungen anzeigen (eine pro Zeile)
    if gene_codes and gene_codes[0] != '[SIMULATION]':
        info_lines.append("─" * 28)
        for j, code in enumerate(gene_codes):
            info_lines.append(f"  K{j+1}: {code}")
    else:
        info_lines.append(f"Seq:     {sequence[:35]}")
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
    path     = os.path.join(tmp_dir, f'genome_entry_{sym_safe}_{timeframe}_{ts}.png')
    fig.savefig(path, dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def _send_genome_chart(df: pd.DataFrame, genome_signal: dict, symbol: str,
                        timeframe: str, telegram_config: dict, logger: logging.Logger):
    """Generiert Genome-Chart-PNG und sendet es via Telegram."""
    if not telegram_config or not telegram_config.get('bot_token') or not telegram_config.get('chat_id'):
        return
    try:
        path = _generate_genome_chart_png(df, genome_signal, symbol, timeframe)
        if path and os.path.exists(path):
            side_label = 'LONG' if genome_signal.get('side') == 'long' else 'SHORT'
            ep = genome_signal.get('entry_price', 0)
            sl = genome_signal.get('sl_price', 0)
            tp = genome_signal.get('tp_price', 0)
            caption = (
                f"DNABOT | {symbol} ({timeframe})\n"
                f"{side_label} @ {ep:.6g}  |  SL: {sl:.6g}  |  TP: {tp:.6g}\n"
                f"Score: {genome_signal.get('score', 0):.3f}  |  "
                f"WR: {genome_signal.get('winrate', 0):.1%}  |  "
                f"n={genome_signal.get('total_occurrences', 0)}"
            )
            send_photo(telegram_config.get('bot_token'), telegram_config.get('chat_id'),
                       path, caption)
            os.remove(path)
    except Exception as e:
        logger.warning(f"Genome-Chart senden fehlgeschlagen: {e}")


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
    Platziert einen Entry-Trade basierend auf dem Genome-Signal.

    Entry: Market-Order (sofort, da Sequenz bereits abgeschlossen ist)
    SL: Aus der Sequenz-Struktur (Low/High der Genome-Kerzen)
    TP: 2:1 R:R vom Entry
    """
    symbol = params['market']['symbol']
    side = genome_signal.get('side')

    if side is None:
        logger.info("Kein Genome-Signal → kein Trade.")
        return

    if side == 'long' and not params.get('behavior', {}).get('use_longs', True):
        logger.info("Longs deaktiviert.")
        return
    if side == 'short' and not params.get('behavior', {}).get('use_shorts', True):
        logger.info("Shorts deaktiviert.")
        return

    risk = params['risk']
    leverage = risk['leverage']
    fallback_risk_pct = risk.get('risk_per_entry_pct', 1.0)
    if risk.get('use_kelly_sizing', False):
        threshold_winrate = params.get('genome', {}).get('min_winrate', 0.45)
        risk_pct = _kelly_risk_pct(
            winrate=genome_signal.get('winrate', 0.0),
            rr_ratio=risk.get('rr_ratio', 2.0),
            threshold_winrate=threshold_winrate,
            min_mult=risk.get('kelly_min_mult', 0.5),
            max_mult=risk.get('kelly_max_mult', 3.0),
            fallback_risk_pct=fallback_risk_pct,
            dampening=risk.get('kelly_dampening', 0.3),
        )
        logger.info(
            f"[Kelly Sizing] WR={genome_signal.get('winrate', 0.0):.1%} "
            f"(Schwelle {threshold_winrate:.1%}) RR={risk.get('rr_ratio', 2.0)} "
            f"-> Risk={risk_pct:.2f}% (statt fix {fallback_risk_pct:.2f}%)"
        )
    else:
        risk_pct = fallback_risk_pct
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
        f"TP={tp_price:.4f} | Score={genome_signal['score']:.3f}"
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

    # Tracker aktualisieren (Genome-Info für Self-Learning)
    tracker = read_tracker(tracker_path)
    tracker['stop_loss_ids'] = new_sl_ids
    tracker['take_profit_ids'] = new_tp_ids
    tracker['tsl_api_visible'] = tsl_api_visible
    tracker['last_side'] = side
    tracker['status'] = 'ok_to_trade'
    tracker['last_notified_entry_price'] = actual_entry
    tracker['last_notified_side'] = side
    tracker['active_genome'] = {
        "genome_id": genome_signal['genome_id'],
        "sequence": genome_signal['sequence'],
        "direction": side.upper(),
        "seq_length": genome_signal['seq_length'],
        "score": genome_signal['score'],
        "winrate": genome_signal['winrate'],
        "total_occurrences": genome_signal['total_occurrences'],
        "entry_price": actual_entry,
        "sl_price": sl_price,
        "tp_price": tp_price,
        # Regime zum Signal-Zeitpunkt (von genome_logic.py gesetzt) -- wird beim
        # Trade-Abschluss ans Self-Learning zurueckgegeben, siehe
        # self_learn_from_closed_trade(). Ohne das landet JEDER Live-Trade in der
        # NEUTRAL-Statistikspalte, egal in welchem Regime er tatsaechlich lief,
        # waehrend discovery.py offline korrekt das tatsaechliche Regime trackt.
        "regime": genome_signal.get('regime', 'NEUTRAL'),
        # Order-Block-Signale haben keine echte Genome-DB-Zeile (siehe
        # genome/order_blocks.py-Docstring) -- self_learn_from_closed_trade()
        # muss das erkennen und den DB-Schreibversuch ueberspringen.
        "is_order_block": genome_signal.get('is_order_block', False),
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
            f"🧬 Genome:  {genome_signal['genome_id'][:8]}... | "
            f"Score: {genome_signal['score']:.3f} | "
            f"WR: {genome_signal['winrate']:.1%} | "
            f"n={genome_signal['total_occurrences']}\n"
            f"🔢 Sequenz: {genome_signal['sequence']}"
        )
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
    except Exception as e:
        logger.warning(f"Telegram-Benachrichtigung fehlgeschlagen: {e}")

    # Chart mit Genome-Pattern per Telegram senden
    _send_genome_chart(df, genome_signal, symbol, timeframe, telegram_config, logger)


# ─── Self-Learning Update ─────────────────────────────────────────────────────

def self_learn_from_closed_trade(
    tracker_path: str, db: GenomeDB, outcome: str,
    exit_price: float, logger: logging.Logger
):
    """
    Aktualisiert die Genome-DB nach einem abgeschlossenen Trade.
    Wird aufgerufen wenn SL oder TP ausgelöst wurde.
    """
    tracker = read_tracker(tracker_path)
    active_genome = tracker.get('active_genome')

    if not active_genome:
        return

    if active_genome.get('is_order_block'):
        # Order Blocks haben keine Genome-DB-Zeile und kein Signifikanz-
        # Tracking (siehe genome/order_blocks.py) -- ein Upsert mit dem
        # Platzhalter-"sequence"-String wuerde nur bedeutungslose Einmal-
        # Zeilen in der Genome-DB erzeugen. Tracker trotzdem aufraeumen.
        logger.info("OB-Trade abgeschlossen (kein DB-Update, kein Signifikanz-Tracking für Order Blocks).")
        tracker['active_genome'] = None
        _write_tracker(tracker_path, tracker)
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

    update_genome_with_trade_result(
        db=db,
        genome_id=active_genome['genome_id'],
        sequence=active_genome['sequence'],
        market=tracker.get('market', ''),
        timeframe=tracker.get('timeframe', ''),
        direction=direction,
        seq_length=active_genome['seq_length'],
        outcome=outcome,
        actual_move_pct=actual_move_pct,
        # Regime vom Signal-Zeitpunkt (in place_entry_orders() gespeichert) --
        # ohne das faellt update_genome_with_trade_result() auf 'NEUTRAL'
        # zurueck, unabhaengig vom tatsaechlichen Regime des Trades.
        regime=active_genome.get('regime', 'NEUTRAL'),
    )

    # Genome aus Tracker löschen (Trade abgeschlossen)
    tracker['active_genome'] = None
    _write_tracker(tracker_path, tracker)


# ─── Haupt-Trading-Zyklus ─────────────────────────────────────────────────────

def full_trade_cycle(
    exchange: Exchange,
    params: dict,
    telegram_config: dict,
    db_path: str,
    logger: logging.Logger,
):
    """
    Vollständiger Handelszyklus für dnabot:

    1. OHLCV-Daten laden
    2. Genome-Signal berechnen
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

    # 2. Genome-Signal
    db = GenomeDB(db_path)
    genome_signal = get_genome_signal(df, params, db)

    if genome_signal:
        logger.info(
            f"Genome Signal: {genome_signal['side'].upper()} | "
            f"Score: {genome_signal['score']:.3f} | WR: {genome_signal['winrate']:.1%}"
        )
    else:
        logger.info("Kein aktives Genome-Signal für aktuellen Markt.")

    # 2b. Order-Block-Signal -- nur als Fallback, wenn kein Genome-Signal
    # vorliegt (etabliertes System hat Vorrang, siehe order_block_logic.py-
    # Docstring). get_order_block_signal() liefert selbst None, solange
    # order_block_settings.enabled=false ist (Standard).
    ob_signal = None
    if not genome_signal:
        ob_signal = get_order_block_signal(df, params)
    genome_signal = genome_signal or ob_signal

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
                    self_learn_from_closed_trade(tracker_path, db, outcome_label, price_for_learning, logger)
                except Exception as e:
                    logger.error(f"Self-Learning Fehler: {e}")
                emoji = "✅" if outcome == 'win' else "🛑"
                try:
                    send_message(
                        telegram_config.get('bot_token'),
                        telegram_config.get('chat_id'),
                        f"{emoji} dnabot {reason}: {symbol} ({timeframe})\n"
                        f"Genome aktualisiert → {outcome_label}"
                    )
                except Exception:
                    pass

            tracker = read_tracker(tracker_path)
            tracker.update({"stop_loss_ids": [], "take_profit_ids": [], "status": "ok_to_trade"})
            tracker.pop('last_notified_entry_price', None)
            tracker.pop('last_notified_side', None)
            _write_tracker(tracker_path, tracker)

        balance = exchange.fetch_balance_usdt()
        logger.info(f"Guthaben: {balance:.2f} USDT")

        if balance < MIN_NOTIONAL_USDT:
            logger.warning(f"Guthaben zu niedrig ({balance:.2f} USDT).")
            db.close()
            return

        if genome_signal is None:
            logger.info("Kein Genome-Signal → kein Entry.")
            db.close()
            return
        place_entry_orders(exchange, genome_signal, params, balance, tracker_path, telegram_config, logger, df=df)

    db.close()
    logger.info(f"Trade-Zyklus abgeschlossen für {symbol} ({timeframe}).")
