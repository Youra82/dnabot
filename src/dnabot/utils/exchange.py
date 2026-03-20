# src/dnabot/utils/exchange.py
# Adaptiert aus dbot — nur Namespace geändert
import ccxt
import pandas as pd
from datetime import datetime, timezone
import logging
import time
from typing import Optional
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

logger = logging.getLogger(__name__)


class Exchange:
    def __init__(self, account_config):
        self.account = account_config
        self.exchange = getattr(ccxt, 'bitget')({
            'apiKey': self.account.get('apiKey'),
            'secret': self.account.get('secret'),
            'password': self.account.get('password'),
            'options': {'defaultType': 'swap'},
            'enableRateLimit': True,
        })
        try:
            self.markets = self.exchange.load_markets()
            logger.info("Märkte geladen.")
        except Exception as e:
            logger.critical(f"Märkte konnten nicht geladen werden: {e}")
            self.markets = {}

    def fetch_recent_ohlcv(self, symbol, timeframe, limit=1000):
        if not self.markets:
            return pd.DataFrame()
        BATCH = 1000
        timeframe_ms = self.exchange.parse_timeframe(timeframe) * 1000
        all_ohlcv = []
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=min(limit, BATCH))
            if not ohlcv:
                return pd.DataFrame()
            all_ohlcv = ohlcv
        except Exception as e:
            logger.error(f"Fehler beim Laden von OHLCV für {symbol}: {e}")
            return pd.DataFrame()

        while len(all_ohlcv) < limit:
            oldest_ts = all_ohlcv[0][0]
            fetch_since = oldest_ts - timeframe_ms * BATCH
            remaining = limit - len(all_ohlcv)
            try:
                time.sleep(self.exchange.rateLimit / 1000)
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, fetch_since, min(remaining + 10, BATCH))
                if not ohlcv:
                    break
                ohlcv = [c for c in ohlcv if c[0] < oldest_ts]
                if not ohlcv:
                    break
                all_ohlcv = ohlcv + all_ohlcv
            except ccxt.RateLimitExceeded:
                time.sleep(5)
            except Exception as e:
                logger.error(f"Fehler beim Laden älterer OHLCV für {symbol}: {e}")
                break

        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]
        if len(df) > limit:
            df = df.iloc[-limit:]
        return df

    def fetch_historical_ohlcv(self, symbol, timeframe, start_date_str, end_date_str):
        if not self.markets:
            return pd.DataFrame()
        start_ts = int(self.exchange.parse8601(start_date_str + 'T00:00:00Z'))
        end_ts = int(self.exchange.parse8601(end_date_str + 'T23:59:59Z'))
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        all_ohlcv = []
        current_ts = start_ts
        logger.info(f"Historischer Download: {symbol} ({timeframe}) | {start_date_str} → {end_date_str}")

        while current_ts < end_ts:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, current_ts, 200)
                if not ohlcv:
                    break
                ohlcv = [c for c in ohlcv if c[0] <= end_ts]
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                current_ts = ohlcv[-1][0] + tf_ms
                time.sleep(self.exchange.rateLimit / 1000)
            except ccxt.RateLimitExceeded:
                time.sleep(10)
            except Exception as e:
                logger.error(f"Fehler beim historischen Download: {e}")
                time.sleep(5)

        if not all_ohlcv:
            return pd.DataFrame()
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]
        logger.info(f"Geladen: {len(df)} Kerzen für {symbol}")
        return df

    def fetch_ticker(self, symbol):
        if not self.markets:
            return None
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Ticker-Fehler {symbol}: {e}")
            raise

    def fetch_min_amount_tradable(self, symbol: str) -> float:
        if not self.markets:
            return 0.0
        try:
            if symbol not in self.markets:
                self.markets = self.exchange.load_markets()
            min_amount = self.markets[symbol].get('limits', {}).get('amount', {}).get('min')
            return float(min_amount) if min_amount is not None else 0.0
        except Exception as e:
            logger.error(f"Fehler min_amount {symbol}: {e}")
            return 0.0

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception:
            return str(amount)

    def price_to_precision(self, symbol: str, price: float) -> str:
        try:
            return self.exchange.price_to_precision(symbol, price)
        except Exception:
            return str(price)

    def fetch_balance_usdt(self) -> float:
        if not self.markets:
            return 0.0
        try:
            params = {'marginCoin': 'USDT', 'productType': 'USDT-FUTURES'}
            balance = self.exchange.fetch_balance(params=params)
            usdt = 0.0
            if 'USDT' in balance and balance['USDT'].get('free') is not None:
                usdt = float(balance['USDT']['free'])
            elif 'info' in balance and isinstance(balance['info'], list):
                for a in balance['info']:
                    if a.get('marginCoin') == 'USDT':
                        usdt = float(a.get('available', 0.0))
                        break
            logger.info(f"USDT-Guthaben: {usdt:.2f}")
            return usdt
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Guthabens: {e}", exc_info=True)
            return 0.0

    def fetch_open_positions(self, symbol):
        if not self.markets:
            return []
        try:
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            positions = self.exchange.fetch_positions([symbol], params=params)
            open_pos = []
            for p in positions:
                try:
                    size_key = 'contracts' if 'contracts' in p else 'contractSize'
                    contracts = p.get(size_key)
                    if contracts is not None and abs(float(contracts)) > 1e-9:
                        open_pos.append(p)
                    elif p.get('initialMargin', 0) > 0:
                        open_pos.append(p)
                except Exception:
                    continue
            return open_pos
        except Exception as e:
            logger.error(f"Fehler bei offenen Positionen {symbol}: {e}", exc_info=True)
            return []

    def fetch_open_orders(self, symbol: str):
        if not self.markets:
            return []
        try:
            return self.exchange.fetch_open_orders(symbol, params={'stop': False, 'productType': 'USDT-FUTURES'})
        except Exception as e:
            logger.error(f"Fehler open orders {symbol}: {e}")
            return []

    def fetch_open_trigger_orders(self, symbol: str):
        if not self.markets:
            return []
        try:
            return self.exchange.fetch_open_orders(symbol, params={'stop': True, 'productType': 'USDT-FUTURES'})
        except Exception as e:
            logger.error(f"Fehler trigger orders {symbol}: {e}")
            return []

    def cancel_order(self, id: str, symbol: str):
        if not self.markets:
            return None
        try:
            return self.exchange.cancel_order(id, symbol, params={'stop': False, 'productType': 'USDT-FUTURES'})
        except ccxt.OrderNotFound:
            return None
        except Exception as e:
            logger.error(f"Fehler cancel order {id}: {e}")
            raise

    def cancel_trigger_order(self, id: str, symbol: str):
        if not self.markets:
            return None
        try:
            return self.exchange.cancel_order(id, symbol, params={'stop': True, 'productType': 'USDT-FUTURES'})
        except ccxt.OrderNotFound:
            return None
        except Exception as e:
            logger.error(f"Fehler cancel trigger {id}: {e}")
            raise

    def cancel_all_orders_for_symbol(self, symbol):
        if not self.markets:
            return 0
        count = 0
        for stop_flag in [False, True]:
            try:
                self.exchange.cancel_all_orders(symbol, params={'productType': 'USDT-FUTURES', 'stop': stop_flag})
                count += 1
                time.sleep(0.5)
            except ccxt.ExchangeError as e:
                if any(x in str(e) for x in ['Order not found', 'no order to cancel', '22001']):
                    pass
                else:
                    logger.error(f"Fehler cancel_all (stop={stop_flag}): {e}")
            except Exception as e:
                logger.error(f"Unerwarteter Fehler cancel_all: {e}")
        return count

    def set_margin_mode(self, symbol, margin_mode='isolated'):
        if not self.markets: return
        margin_mode_lower = margin_mode.lower()
        if margin_mode_lower not in ['isolated', 'cross']:
            logger.error(f"Ungültiger Margin-Modus: {margin_mode}.")
            return
        try:
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            self.exchange.set_margin_mode(margin_mode_lower, symbol, params=params)
            logger.info(f"Margin-Modus für {symbol} auf '{margin_mode_lower}' gesetzt.")
        except ccxt.ExchangeError as e:
            if 'Margin mode is the same' in str(e) or 'margin mode is not changed' in str(e).lower() or '40051' in str(e):
                logger.debug(f"Margin-Modus für {symbol} ist bereits '{margin_mode_lower}'.")
            else:
                logger.error(f"Fehler beim Setzen des Margin-Modus für {symbol}: {e}")
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Setzen des Margin-Modus für {symbol}: {e}")

    def set_leverage(self, symbol, leverage, margin_mode='isolated'):
        if not self.markets: return
        try:
            leverage = int(leverage)
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT', 'marginMode': margin_mode.lower()}
            self.exchange.set_leverage(leverage, symbol, params=params)
            logger.info(f"Hebel für {symbol} ({margin_mode}) auf {leverage}x gesetzt.")
        except ccxt.ExchangeError as e:
            if 'Leverage not changed' in str(e) or 'leverage is not modified' in str(e).lower() or '40052' in str(e):
                logger.debug(f"Hebel für {symbol} ist bereits {leverage}x.")
            else:
                logger.error(f"Fehler beim Setzen des Hebels für {symbol}: {e}")
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Setzen des Hebels für {symbol}: {e}")

    def place_market_order(self, symbol: str, side: str, amount: float, reduce: bool = False, margin_mode: str = 'isolated', params={}):
        if not self.markets:
            return None
        try:
            p = {'reduceOnly': reduce, 'productType': 'USDT-FUTURES', 'marginCoin': 'USDT', 'marginMode': margin_mode, **params}
            amount_str = self.amount_to_precision(symbol, amount)
            logger.info(f"Market Order: {side.upper()} {amount_str} {symbol}")
            return self.exchange.create_order(symbol, 'market', side, float(amount_str), params=p)
        except Exception as e:
            logger.error(f"Fehler market order {symbol}: {e}", exc_info=True)
            raise

    def place_trigger_market_order(self, symbol: str, side: str, amount: float,
                                    trigger_price: float, reduce: bool = False, params={}):
        if not self.markets:
            return None
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_str = self.price_to_precision(symbol, trigger_price)
            p = {'triggerPrice': trigger_str, 'reduceOnly': reduce, 'productType': 'USDT-FUTURES', **params}
            logger.info(f"Trigger Market: {side.upper()} {amount_str} {symbol} @ trigger {trigger_str}")
            return self.exchange.create_order(symbol, 'market', side, float(amount_str), params=p)
        except Exception as e:
            logger.error(f"Fehler trigger market {symbol}: {e}", exc_info=True)
            raise

    def place_trailing_stop_order(self, symbol: str, side: str, amount: float,
                                   activation_price: float, callback_rate_decimal: float, params={}):
        """Bitget nativer Trailing Stop. callback_rate_decimal: z.B. 0.01 = 1%."""
        if not self.markets:
            return None
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            activation_str = self.price_to_precision(symbol, activation_price)
            callback_pct = round(callback_rate_decimal * 100, 4)
            p = {
                'trailingTriggerPrice': activation_str,
                'trailingPercent': callback_pct,
                'reduceOnly': True,
                'productType': 'USDT-FUTURES',
                **params
            }
            logger.info(f"Trailing Stop: {side.upper()} {amount_str} {symbol} | Aktivierung @ {activation_str} | Callback {callback_pct:.2f}%")
            return self.exchange.create_order(symbol, 'market', side, float(amount_str), params=p)
        except Exception as e:
            logger.error(f"Fehler trailing stop {symbol}: {e}", exc_info=True)
            raise

    def place_trigger_limit_order(self, symbol: str, side: str, amount: float,
                                   trigger_price: float, price: float, reduce: bool = False, params={}):
        if not self.markets:
            return None
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_str = self.price_to_precision(symbol, trigger_price)
            price_str = self.price_to_precision(symbol, price)
            p = {'triggerPrice': trigger_str, 'reduceOnly': reduce, 'productType': 'USDT-FUTURES', **params}
            logger.info(f"Trigger Limit: {side.upper()} {amount_str} {symbol} @ trigger {trigger_str} limit {price_str}")
            return self.exchange.create_order(symbol, 'limit', side, float(amount_str), float(price_str), params=p)
        except Exception as e:
            logger.error(f"Fehler trigger limit {symbol}: {e}", exc_info=True)
            raise

    def close_position(self, symbol: str):
        if not self.markets:
            return None
        try:
            positions = self.fetch_open_positions(symbol)
            if not positions:
                return None
            pos = positions[0]
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            size_key = 'contracts' if 'contracts' in pos else 'contractSize'
            amount = float(pos.get(size_key, 0))
            return self.place_market_order(symbol, close_side, amount, reduce=True)
        except Exception as e:
            logger.error(f"Fehler close_position {symbol}: {e}")
            raise
