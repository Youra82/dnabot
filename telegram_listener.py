#!/usr/bin/env python3
# telegram_listener.py
# Lauscht auf Telegram-Befehle und antwortet.
#
# Befehl "Gen":
#   → Aktuelle GenCode-Sequenz (letzte 6 Gene) pro aktiver Strategie
#   → Wahrscheinlichster nächster GenCode (aus DB-Statistik)
#
# Start: python3 telegram_listener.py
# Empfehlung: in screen/tmux oder als systemd-Service laufen lassen

import json
import os
import sys
import time
import logging
import requests

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from dnabot.genome.encoder import encode_dataframe
from dnabot.genome.database import GenomeDB
from dnabot.genome.regime import detect_regime
from dnabot.utils.exchange import Exchange
from dnabot.utils.telegram import send_message

DB_PATH      = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'genome.db')
OFFSET_FILE  = os.path.join(PROJECT_ROOT, 'artifacts', 'telegram_offset.json')
SETTINGS_FILE = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE   = os.path.join(PROJECT_ROOT, 'secret.json')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_ROOT, 'logs', 'telegram_listener.log')),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)


# ─── Telegram Polling ─────────────────────────────────────────────────────────

def _get_updates(bot_token: str, offset: int | None, timeout: int = 30) -> list:
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {'timeout': timeout, 'allowed_updates': ['message']}
    if offset is not None:
        params['offset'] = offset
    try:
        r = requests.get(url, params=params, timeout=timeout + 5)
        r.raise_for_status()
        return r.json().get('result', [])
    except Exception as e:
        logger.warning(f"getUpdates Fehler: {e}")
        return []


def _load_offset() -> int | None:
    if os.path.exists(OFFSET_FILE):
        try:
            with open(OFFSET_FILE) as f:
                return json.load(f).get('offset')
        except Exception:
            pass
    return None


def _save_offset(offset: int):
    os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
    with open(OFFSET_FILE, 'w') as f:
        json.dump({'offset': offset}, f)


# ─── GenCode-Logik ────────────────────────────────────────────────────────────

def _predict_next_gene(db: GenomeDB, current_genes: list[str], market: str, timeframe: str) -> dict | None:
    """
    Sucht in der DB nach dem wahrscheinlichsten nächsten Gen.

    Methode:
      - Nimmt die letzten 3 aktuellen Gene als Prefix (gA|gB|gC|...)
      - Findet alle DB-Sequenzen die damit beginnen
      - Extrahiert das 4. Gen (= das Gen das historisch am häufigsten folgte)
      - Gibt das häufigste / am besten bewertete zurück

    Rückgabe: { 'gene': str, 'occ': int, 'score': float, 'direction': str }
    """
    if len(current_genes) < 3:
        return None

    prefix_str = "|".join(current_genes[-3:]) + "|"

    try:
        rows = db._conn.execute("""
            SELECT sequence, direction, score, wins, total_occurrences,
                   CAST(wins AS REAL) / total_occurrences AS winrate
            FROM genomes
            WHERE market = ? AND timeframe = ? AND sequence LIKE ?
              AND total_occurrences >= 3
            ORDER BY score DESC
        """, (market, timeframe, prefix_str + '%')).fetchall()
    except Exception as e:
        logger.warning(f"DB-Abfrage Prediction fehlgeschlagen: {e}")
        return None

    if not rows:
        return None

    # Kandidaten: alle 4. Gene aus passenden Sequenzen aggregieren
    candidates: dict[str, dict] = {}
    prefix_parts = current_genes[-3:]

    for row in rows:
        parts = row['sequence'].split('|')
        # Nur Sequenzen wo die ersten 3 Gene exakt unseren letzten 3 entsprechen
        if parts[:3] != list(prefix_parts) or len(parts) < 4:
            continue
        next_gene = parts[3]
        if next_gene not in candidates:
            candidates[next_gene] = {'occ': 0, 'score': 0.0, 'direction': row['direction']}
        candidates[next_gene]['occ']   += row['total_occurrences']
        candidates[next_gene]['score'] += row['score']

    if not candidates:
        return None

    # Bestes = höchste Gesamtoccurrence (mit Score als Tiebreaker)
    best_gene, best_stats = max(
        candidates.items(),
        key=lambda x: (x[1]['occ'], x[1]['score'])
    )
    return {'gene': best_gene, **best_stats}


_BODY    = {'1': 'klein',  '2': 'mittel', '3': 'groß'}
_WICK    = {'U': '↑Wick', 'D': '↓Wick',  'B': '↕Wick', 'N': 'kein Wick'}
_VOL     = {'H': 'vol↑',  'L': 'vol↓'}
_DIR     = {'B': ('🟢', 'Bullish'), 'S': ('🔴', 'Bearish')}


def _decode_gene(gene: str) -> str:
    """
    Dekodiert einen Gen-String zu einer kurzen lesbaren Beschreibung.
    Beispiel: "B2H-NH" → "🟢 Bullish · mittel · kein Wick · vol↑"
    """
    try:
        main, ext = gene.split('-')
        emoji, direction = _DIR.get(main[0], ('?', '?'))
        body = _BODY.get(main[1], main[1])
        wick = _WICK.get(ext[0], ext[0])
        vol  = _VOL.get(ext[1], ext[1])
        return f"{emoji} {direction} · {body} · {wick} · {vol}"
    except Exception:
        return gene


def _confidence(n: int) -> str:
    if n >= 50:
        return "starke Basis"
    if n >= 15:
        return "moderate Basis"
    return "wenig Daten"


def _handle_gen(bot_token: str, chat_id: str):
    """
    Verarbeitet den 'Gen'-Befehl:
    Lädt Kerzen für jede aktive Strategie, kodiert Gene, sucht Prediction.
    """
    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
        with open(SECRET_FILE) as f:
            secrets = json.load(f)
    except Exception as e:
        send_message(bot_token, chat_id, f"Fehler beim Laden der Config: {e}")
        return

    strategies = [
        s for s in settings.get('live_trading_settings', {}).get('active_strategies', [])
        if s.get('active', False)
    ]
    if not strategies:
        send_message(bot_token, chat_id, "Keine aktiven Strategien in settings.json.")
        return

    accounts = secrets.get('dnabot', [])
    if not accounts:
        send_message(bot_token, chat_id, "Kein dnabot-Account in secret.json.")
        return

    exchange = Exchange(accounts[0])
    db = GenomeDB(DB_PATH)

    from datetime import datetime
    header = f"🧬 dnabot GenCode-Report\n{datetime.now().strftime('%d.%m.%Y %H:%M')}"

    blocks = [header]

    for strat in strategies:
        symbol    = strat['symbol']
        timeframe = strat['timeframe']
        coin      = symbol.split('/')[0]

        try:
            df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=200)
            if df is None or len(df) < 20:
                blocks.append(f"\n{symbol} ({timeframe}): Keine Daten")
                continue

            genes  = encode_dataframe(df)
            last4  = genes[-4:]
            regime = detect_regime(df)

            # Kerzen-Sequenz (letzte 4, älteste zuerst)
            seq_lines = []
            labels = ['  -3', '  -2', '  -1', '  »']
            for i, (label, g) in enumerate(zip(labels, last4)):
                marker = ' ← jetzt' if i == 3 else ''
                seq_lines.append(f"{label}  {g:10s}  {_decode_gene(g)}{marker}")

            # Prediction
            prediction = _predict_next_gene(db, last4, symbol, timeframe)
            if prediction:
                conf  = _confidence(prediction['occ'])
                pred_decoded = _decode_gene(prediction['gene'])
                pred_block = (
                    f"🔮 Nächste Kerze:\n"
                    f"     {prediction['gene']:10s}  {pred_decoded}\n"
                    f"     {prediction['occ']} Fälle in DB · {conf}"
                )
            else:
                pred_block = "🔮 Nächste Kerze: keine Prediction (zu wenig Daten)"

            block = (
                f"\n{'─' * 32}\n"
                f"📊 {coin} ({timeframe}) · Regime: {regime}\n"
                + "\n".join(seq_lines)
                + f"\n{pred_block}"
            )
            blocks.append(block)

        except Exception as e:
            logger.error(f"Fehler bei {symbol}: {e}", exc_info=True)
            blocks.append(f"\n{symbol} ({timeframe}): Fehler — {e}")

        time.sleep(0.3)

    db.close()
    send_message(bot_token, chat_id, "\n".join(blocks))
    logger.info("Gen-Report gesendet.")


# ─── Main Loop ────────────────────────────────────────────────────────────────

def main():
    os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)

    try:
        with open(SECRET_FILE) as f:
            secrets = json.load(f)
    except Exception as e:
        logger.critical(f"secret.json nicht ladbar: {e}")
        sys.exit(1)

    telegram = secrets.get('telegram', {})
    bot_token = telegram.get('bot_token')
    chat_id   = str(telegram.get('chat_id', ''))

    if not bot_token or not chat_id:
        logger.critical("Kein bot_token / chat_id in secret.json unter 'telegram'.")
        sys.exit(1)

    offset = _load_offset()
    logger.info(f"Telegram-Listener gestartet. Warte auf 'Gen' von Chat {chat_id} ...")

    while True:
        updates = _get_updates(bot_token, offset, timeout=30)

        for update in updates:
            offset = update['update_id'] + 1
            _save_offset(offset)

            msg       = update.get('message', {})
            text      = msg.get('text', '').strip()
            from_chat = str(msg.get('chat', {}).get('id', ''))

            if not text or from_chat != chat_id:
                continue

            logger.info(f"Nachricht empfangen: '{text}'")

            if text.lower() == 'gen':
                logger.info("Gen-Befehl erkannt — starte GenCode-Report...")
                _handle_gen(bot_token, chat_id)

        if not updates:
            time.sleep(1)


if __name__ == '__main__':
    main()
