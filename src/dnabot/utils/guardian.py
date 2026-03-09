# src/dnabot/utils/guardian.py
import logging
from functools import wraps
from dnabot.utils.telegram import send_message


def guardian_decorator(func):
    """
    Decorator: Fängt alle unerwarteten Ausnahmen ab, loggt sie
    und sendet eine Telegram-Warnung.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger = None
        telegram_config = {}
        params = {}

        for arg in args:
            if isinstance(arg, logging.Logger):
                logger = arg
            if isinstance(arg, dict) and 'bot_token' in arg:
                telegram_config = arg
            if isinstance(arg, dict) and 'market' in arg:
                params = arg

        if not logger:
            logger = logging.getLogger("guardian_fallback")
            if not logger.handlers:
                logger.addHandler(logging.StreamHandler())

        try:
            return func(*args, **kwargs)

        except Exception as e:
            symbol = params.get('market', {}).get('symbol', 'Unbekannt')
            timeframe = params.get('market', {}).get('timeframe', 'N/A')

            logger.critical(f"!!! KRITISCHER FEHLER: {symbol} ({timeframe}) — {e}", exc_info=True)

            try:
                send_message(
                    telegram_config.get('bot_token'),
                    telegram_config.get('chat_id'),
                    f"KRITISCHER FEHLER dnabot für {symbol} ({timeframe}):\n{e.__class__.__name__}: {e}"
                )
            except Exception:
                pass

            raise e

    return wrapper
