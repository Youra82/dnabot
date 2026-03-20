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

from dnabot.utils.telegram import send_message
from dnabot.utils.exchange import Exchange
from dnabot.genome.database import GenomeDB
from dnabot.strategy.genome_logic import get_genome_signal, update_genome_with_trade_result

MIN_NOTIONAL_USDT = 5.0
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
    if tracker_path:
        try:
            t = read_tracker(tracker_path)
            protected_ids.update(t.get('take_profit_ids', []))
            protected_ids.update(t.get('stop_loss_ids', []))
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
                  params: dict, tracker_path: str, logger: logging.Logger):
    """Setzt Trailing Stop / SL nach wenn sie fehlen (Sicherheitsnetz)."""
    symbol = params['market']['symbol']
    pos_side = position['side']
    entry_price = float(position.get('entryPrice', 0))

    triggers = exchange.fetch_open_trigger_orders(symbol)
    trigger_ids = {o['id'] for o in triggers}

    tracker = read_tracker(tracker_path)
    tp_ids = set(tracker.get('take_profit_ids', []))
    sl_ids = set(tracker.get('stop_loss_ids', []))

    # TP = Trailing Stop → Tracker-IDs als Wahrheit (Bitget gibt Trailing Orders
    # nicht über fetchOpenOrders zurück, daher kein API-Check möglich)
    tp_exists = bool(tp_ids)

    # SL = fester Trigger → Tracker-IDs, Fallback: Preis-Richtung
    if sl_ids:
        sl_exists = bool(sl_ids & trigger_ids)
    else:
        sl_exists = any(
            o.get('reduceOnly') and (
                (pos_side == 'long' and o.get('side') == 'sell' and float(o.get('triggerPrice', 0)) < entry_price) or
                (pos_side == 'short' and o.get('side') == 'buy' and float(o.get('triggerPrice', 0)) > entry_price)
            )
            for o in triggers
        )

    if tp_exists and sl_exists:
        return

    logger.warning(f"Trailing Stop={tp_exists}, SL={sl_exists} fehlen — nachtragen...")

    contracts = float(position.get('contracts', 0))
    if contracts == 0:
        return

    # Preise aus Signal — Fallback auf gespeichertes active_genome im Tracker
    active_genome = tracker.get('active_genome') or {}
    tp_price = (genome_signal.get('tp_price') if genome_signal else None) or active_genome.get('tp_price')
    sl_price = (genome_signal.get('sl_price') if genome_signal else None) or active_genome.get('sl_price')
    if not tp_price or not sl_price:
        logger.warning("Kein tp_price/sl_price verfügbar (weder Signal noch Tracker) — Nachtragen nicht möglich.")
        return

    trailing_callback = params['risk'].get('trailing_callback_rate_pct', 1.0) / 100.0
    new_tp_ids = list(tp_ids)
    new_sl_ids = list(sl_ids)

    try:
        if not tp_exists and tp_price:
            trail_side = 'sell' if pos_side == 'long' else 'buy'
            o = exchange.place_trailing_stop_order(symbol, trail_side, contracts, tp_price, trailing_callback)
            if o and 'id' in o:
                new_tp_ids = [o['id']]
            logger.info(f"Trailing Stop nachgetragen (Aktivierung @ {tp_price:.4f}, Callback {trailing_callback*100:.1f}%)")
            time.sleep(0.2)

        if not sl_exists and sl_price:
            sl_side = 'sell' if pos_side == 'long' else 'buy'
            o = exchange.place_trigger_market_order(symbol, sl_side, contracts, sl_price, reduce=True)
            if o and 'id' in o:
                new_sl_ids = [o['id']]
            logger.info(f"SL nachgetragen @ {sl_price:.4f}")
    except Exception as e:
        logger.error(f"Fehler beim Nachtragen von Trailing Stop/SL: {e}", exc_info=True)

    tracker['take_profit_ids'] = new_tp_ids
    tracker['stop_loss_ids'] = new_sl_ids
    _write_tracker(tracker_path, tracker)


# ─── Entry Orders ─────────────────────────────────────────────────────────────

def place_entry_orders(
    exchange: Exchange,
    genome_signal: dict,
    params: dict,
    balance: float,
    tracker_path: str,
    telegram_config: dict,
    logger: logging.Logger,
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

    try:
        # 1. Trailing Stop (aktiviert @ TP-Preis = 2:1 R:R, dann trailing)
        tp_order = exchange.place_trailing_stop_order(symbol, tp_side, amount_coins, tp_price, trailing_callback)
        if tp_order and 'id' in tp_order:
            new_tp_ids.append(tp_order['id'])
        logger.info(f"Trailing Stop gesetzt (Aktivierung @ {tp_price:.4f}, Callback {trailing_callback*100:.1f}%)")
        time.sleep(0.2)

        # 2. SL (reduceOnly)
        sl_order = exchange.place_trigger_market_order(symbol, sl_side, amount_coins, sl_price, reduce=True)
        if sl_order and 'id' in sl_order:
            new_sl_ids.append(sl_order['id'])
        logger.info(f"SL gesetzt @ {sl_price:.4f}")
        time.sleep(0.2)

        # 3. Entry Market-Order (Sequenz ist abgeschlossen → sofort einsteigen)
        exchange.place_market_order(symbol, order_side, amount_coins, reduce=False,
                                    margin_mode=risk.get('margin_mode', 'isolated'))
        logger.info(f"Entry Market-Order platziert: {order_side.upper()} @ ~{entry_price:.4f}")

    except ccxt.InsufficientFunds as e:
        logger.error(f"Nicht genug Guthaben: {e}")
        cancel_entry_orders(exchange, symbol, logger)
        return
    except Exception as e:
        logger.error(f"Fehler beim Platzieren: {e}", exc_info=True)
        cancel_entry_orders(exchange, symbol, logger)
        return

    # Tracker aktualisieren (Genome-Info für Self-Learning)
    tracker = read_tracker(tracker_path)
    tracker['stop_loss_ids'] = new_sl_ids
    tracker['take_profit_ids'] = new_tp_ids
    tracker['last_side'] = side
    tracker['status'] = 'ok_to_trade'
    tracker['last_notified_entry_price'] = entry_price
    tracker['last_notified_side'] = side
    tracker['active_genome'] = {
        "genome_id": genome_signal['genome_id'],
        "sequence": genome_signal['sequence'],
        "direction": side.upper(),
        "seq_length": genome_signal['seq_length'],
        "score": genome_signal['score'],
        "winrate": genome_signal['winrate'],
        "total_occurrences": genome_signal['total_occurrences'],
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
    }
    _write_tracker(tracker_path, tracker)

    logger.info(f"Entry-Orders erfolgreich platziert für {symbol} ({side.upper()}).")

    # --- Telegram-Benachrichtigung ---
    try:
        timeframe   = params['market']['timeframe']
        direction_emoji = "🟢" if side == 'long' else "🔴"
        sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
        tp_dist_pct = abs(tp_price - entry_price) / entry_price * 100
        rr_ratio    = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
        risk_usdt   = balance * risk_pct / 100.0
        msg = (
            f"🚀 dnabot SIGNAL: {symbol} ({timeframe})\n"
            f"{'─' * 32}\n"
            f"{direction_emoji} Richtung: {side.upper()}\n"
            f"💰 Entry:        ${entry_price:.6f}\n"
            f"🛑 SL:           ${sl_price:.6f} (-{sl_dist_pct:.2f}%)\n"
            f"🎯 Trailing (ab): ${tp_price:.6f} (+{tp_dist_pct:.2f}%)\n"
            f"🔁 Callback:     {trailing_callback*100:.1f}%\n"
            f"📊 Min R:R:      1:{rr_ratio:.1f}\n"
            f"⚙️ Hebel:        {leverage}x\n"
            f"🛡️ Risiko:       {risk_pct:.1f}% ({risk_usdt:.2f} USDT)\n"
            f"📦 Kontr.:       {amount_coins:.4f}\n"
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
        ensure_tp_sl(exchange, position, genome_signal, params, tracker_path, logger)

    else:
        # Position weg — prüfen ob ein aktiver Trade im Tracker war
        tracker = read_tracker(tracker_path)
        had_tp_ids = bool(tracker.get('take_profit_ids'))
        had_sl_ids = bool(tracker.get('stop_loss_ids'))

        if had_tp_ids or had_sl_ids:
            # Trade wurde geschlossen — Preis-Heuristik: Long↑=WIN, Short↓=WIN
            active_genome = tracker.get('active_genome') or {}
            entry_price = active_genome.get('entry_price', 0)
            last_side = tracker.get('last_side', 'long')

            outcome = None
            if entry_price > 0:
                if last_side == 'long':
                    outcome = 'win' if current_price >= entry_price else 'loss'
                else:
                    outcome = 'win' if current_price <= entry_price else 'loss'
                outcome_label = 'WIN' if outcome == 'win' else 'LOSS'
                reason = "Trailing Stop" if outcome == 'win' else "Stop Loss"
                logger.info(
                    f"Trade geschlossen → {reason} → {outcome_label} "
                    f"(Entry: {entry_price:.4f}, aktuell: {current_price:.4f})"
                )
                record_trade_result(tracker_path, outcome, logger)
                try:
                    self_learn_from_closed_trade(tracker_path, db, outcome_label, current_price, logger)
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
            else:
                logger.warning("Trade geschlossen, kein Entry-Preis im Tracker — kein Self-Learning.")

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
        place_entry_orders(exchange, genome_signal, params, balance, tracker_path, telegram_config, logger)

    db.close()
    logger.info(f"Trade-Zyklus abgeschlossen für {symbol} ({timeframe}).")
