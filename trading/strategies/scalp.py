import logging
import asyncio
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional

from trading.strategies.base import BaseStrategy
from database.db import get_db, add_log
from config_manager import get_strategy_settings, save_strategy_settings

logger = logging.getLogger(__name__)


class ScalpStrategy(BaseStrategy):
    """
    Скальпінг стратегія для короткострокової торгівлі
    Таймфрейм: 1 хвилина
    Індикатори: EMA9, EMA21, RSI14, MACD, StochRSI, Volume
    """

    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        super().__init__(strategy_id, name, mode, exchange)

        # Завантажуємо налаштування з файлу
        saved_settings = get_strategy_settings('scalp')

        self.symbols = saved_settings.get('symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
        self.enabled = saved_settings.get('enabled', False)

        # Параметри торгівлі
        self.trade_size_usdt = saved_settings.get('trade_size_usdt', 20)
        self.take_profit_percent = saved_settings.get('take_profit_percent', 1.2)  # Збільшили з 0.5 до 1.2
        self.stop_loss_percent = saved_settings.get('stop_loss_percent', 0.6)  # Збільшили з 0.25 до 0.6
        self.trailing_stop_percent = saved_settings.get('trailing_stop_percent', 0.5)  # Збільшили з 0.3 до 0.5

        # Параметри індикаторів
        self.rsi_period = 14
        self.rsi_overbought = 70
        self.rsi_oversold = 30

        # MACD параметри
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9

        # StochRSI параметри
        self.stoch_rsi_period = 14
        self.stoch_rsi_k_period = 3
        self.stoch_rsi_d_period = 3
        self.stoch_rsi_overbought = 80
        self.stoch_rsi_oversold = 20

        # Стан
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        self.max_price_position = 80  # Не купуємо якщо ціна вище 80% діапазону
        self.max_5min_gain = 3.0  # Не купуємо якщо зросло більше ніж на 3% за 5 хвилин
        self.max_atr_percent = 2.0  # Не купуємо якщо волатильність >2%

        # Timeframe
        self.timeframe = saved_settings.get('timeframe', '1')

        # Поточні позиції
        self.open_positions: Dict[str, dict] = {}
        self.current_prices: Dict[str, float] = {}

        # Кеш для індикаторів
        self.ema9_cache: Dict[str, List[float]] = {}
        self.ema21_cache: Dict[str, List[float]] = {}
        self.rsi_cache: Dict[str, List[float]] = {}
        self.macd_cache: Dict[str, Dict] = {}
        self.stoch_rsi_cache: Dict[str, Dict] = {}
        self.last_update: Dict[str, datetime] = {}
        self._analysis_task = None

        self.telegram_bot = None

        self._load_history()

        logger.info(f"ScalpStrategy ініціалізовано для пар: {self.symbols}")

    async def start(self):
        await super().start()
        save_strategy_settings('scalp', enabled=True)
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        self.exchange.add_price_callback(self.on_price_update)
        logger.info("ScalpStrategy: цикл аналізу запущено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, True)

    async def stop(self):
        """Зупинка стратегії"""
        if self._analysis_task:
            self._analysis_task.cancel()
            self._analysis_task = None
        await super().stop()
        save_strategy_settings('scalp', enabled=False)
        logger.info("ScalpStrategy: зупинено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, False)

    async def _analysis_loop(self):
        while self.enabled:
            try:
                await self.analyze()
            except asyncio.CancelledError:
                logger.info("ScalpStrategy: цикл аналізу скасовано")
                break
            except Exception as e:
                logger.error(f"Помилка аналізу скальпінгу: {e}")
            await asyncio.sleep(60)

    def _load_history(self):
        with get_db() as conn:
            bal = conn.execute(
                "SELECT amount FROM balances WHERE strategy_id = ? AND asset = 'USDT' AND symbol IS NULL",
                (self.strategy_id,)
            ).fetchone()
            if bal:
                self.balance = bal['amount']
            else:
                self.balance = 100.0
                self._save_balance()

            # Завантажуємо відкриті позиції
            orders = conn.execute(
                "SELECT * FROM orders WHERE strategy_id = ? AND status = 'open'",
                (self.strategy_id,)
            ).fetchall()

            for order in orders:
                o = dict(order)
                if o['side'] == 'buy':
                    # Розраховуємо Stop Loss та Take Profit якщо вони не збережені в БД
                    stop_loss = o.get('stop_loss')
                    take_profit = o.get('take_profit')

                    if not stop_loss or stop_loss == 0:
                        stop_loss = o['price'] * (1 - self.stop_loss_percent / 100)
                    if not take_profit or take_profit == 0:
                        take_profit = o['price'] * (1 + self.take_profit_percent / 100)

                    self.open_positions[o['symbol']] = {
                        'order_id': o['order_id'],
                        'entry_price': o['price'],
                        'quantity': o['quantity'],
                        'highest_price': o['price'],
                        'lowest_price': o['price'],
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'opened_at': o.get('opened_at', datetime.now().isoformat())
                    }
                    self.locked_balance += o['quantity'] * o['price']

                    logger.info(
                        f"Відновлено позицію {o['symbol']}: entry={o['price']}, SL={stop_loss}, TP={take_profit}")

            stats = conn.execute(
                "SELECT SUM(pnl) as pnl, COUNT(*) as cnt FROM orders WHERE strategy_id = ? AND status = 'closed'",
                (self.strategy_id,)
            ).fetchone()
            if stats and stats['pnl']:
                self.total_pnl = stats['pnl']
                self.total_trades = stats['cnt']
                self.winning_trades = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE strategy_id = ? AND status = 'closed' AND pnl > 0",
                    (self.strategy_id,)
                ).fetchone()[0]
                self.losing_trades = self.total_trades - self.winning_trades

    def _save_balance(self):
        with get_db() as conn:
            conn.execute("DELETE FROM balances WHERE strategy_id = ? AND asset = 'USDT' AND symbol IS NULL",
                         (self.strategy_id,))
            conn.execute(
                "INSERT INTO balances (strategy_id, asset, amount, mode, updated_at) VALUES (?, ?, ?, ?, ?)",
                (self.strategy_id, 'USDT', self.balance, self.mode, datetime.now().isoformat())
            )

    async def force_close_position(self, symbol: str) -> dict:
        """Примусове закриття позиції по символу за поточною ціною"""
        if symbol not in self.open_positions:
            return {'success': False, 'error': f'No open position for {symbol}'}

        position = self.open_positions[symbol]
        current_price = self.current_prices.get(symbol, 0)

        if current_price <= 0:
            current_price = await self.exchange.get_current_price(symbol)

        if current_price <= 0:
            return {'success': False, 'error': f'Cannot get current price for {symbol}'}

        # Розраховуємо PnL
        revenue = position['quantity'] * current_price
        cost = position['quantity'] * position['entry_price']
        commission = revenue * 0.0018 + cost * 0.0018
        pnl = revenue - cost - commission
        pnl_percent = (current_price - position['entry_price']) / position['entry_price'] * 100

        # Закриваємо позицію
        await self._close_position(symbol, 'force_close', current_price)

        return {
            'success': True,
            'symbol': symbol,
            'entry_price': position['entry_price'],
            'close_price': current_price,
            'pnl': round(pnl, 2),
            'pnl_percent': round(pnl_percent, 2),
            'quantity': position['quantity']
        }

    def _save_order(self, order_id: str, symbol: str, side: str, price: float, quantity: float, status: str,
                    stop_loss: float = None, take_profit: float = None):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO orders 
                (order_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at, stop_loss, take_profit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, self.strategy_id, symbol, side, price, quantity, status, 'Market',
                  datetime.now().isoformat(), stop_loss, take_profit))
            logger.info(f"✅ Ордер збережено: {order_id} {side} {quantity} {symbol} @ {price}")

    def _update_order(self, order_id: str, pnl: float = None, commission: float = None, status: str = None):
        with get_db() as conn:
            updates = []
            params = []
            if pnl is not None:
                updates.append("pnl = ?")
                params.append(pnl)
            if commission is not None:
                updates.append("commission = ?")
                params.append(commission)
            if status is not None:
                updates.append("status = ?")
                params.append(status)
                updates.append("closed_at = ?")
                params.append(datetime.now().isoformat())
            if updates:
                query = f"UPDATE orders SET {', '.join(updates)} WHERE order_id = ?"
                params.append(order_id)
                conn.execute(query, params)

    @property
    def available_balance(self):
        return self.balance - self.locked_balance

    def get_current_balance(self) -> float:
        return self.balance

    async def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []
        ema = []
        multiplier = 2 / (period + 1)
        ema.append(prices[0])
        for price in prices[1:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])
        return ema

    async def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains = []
        losses = []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        if len(gains) < period or len(losses) < period:
            return 50.0
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    async def calculate_macd(self, prices: List[float]) -> Dict:
        """
        Розрахунок MACD (Moving Average Convergence Divergence)
        Повертає: macd, signal, histogram
        """
        if len(prices) < self.macd_slow:
            return {'macd': 0, 'signal': 0, 'histogram': 0, 'bullish': False, 'bearish': False}

        # Розрахунок EMA
        ema_fast = await self.calculate_ema(prices, self.macd_fast)
        ema_slow = await self.calculate_ema(prices, self.macd_slow)

        if len(ema_fast) < len(prices) or len(ema_slow) < len(prices):
            return {'macd': 0, 'signal': 0, 'histogram': 0, 'bullish': False, 'bearish': False}

        # MACD лінія
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(prices))]

        # Сигнальна лінія (EMA від MACD)
        signal_line = await self.calculate_ema(macd_line, self.macd_signal)

        if not signal_line:
            return {'macd': 0, 'signal': 0, 'histogram': 0, 'bullish': False, 'bearish': False}

        current_macd = macd_line[-1]
        current_signal = signal_line[-1]
        histogram = current_macd - current_signal

        # Сигнали
        prev_macd = macd_line[-2] if len(macd_line) > 1 else 0
        prev_signal = signal_line[-2] if len(signal_line) > 1 else 0

        bullish_cross = prev_macd <= prev_signal and current_macd > current_signal
        bearish_cross = prev_macd >= prev_signal and current_macd < current_signal

        return {
            'macd': current_macd,
            'signal': current_signal,
            'histogram': histogram,
            'bullish': bullish_cross,
            'bearish': bearish_cross
        }

    async def calculate_stoch_rsi(self, prices: List[float]) -> Dict:
        """
        Розрахунок Stochastic RSI
        Повертає: k, d, overbought, oversold
        """
        if len(prices) < self.stoch_rsi_period + self.stoch_rsi_k_period:
            return {'k': 50, 'd': 50, 'overbought': False, 'oversold': False}

        # Розрахунок RSI
        rsi_values = []
        for i in range(len(prices) - self.stoch_rsi_period + 1):
            segment = prices[i:i + self.stoch_rsi_period]
            rsi = await self.calculate_rsi(segment, self.stoch_rsi_period)
            rsi_values.append(rsi)

        if len(rsi_values) < self.stoch_rsi_k_period:
            return {'k': 50, 'd': 50, 'overbought': False, 'oversold': False}

        # Розрахунок StochRSI
        stoch_rsi_values = []
        for i in range(len(rsi_values) - self.stoch_rsi_k_period + 1):
            segment = rsi_values[i:i + self.stoch_rsi_k_period]
            min_rsi = min(segment)
            max_rsi = max(segment)
            if max_rsi == min_rsi:
                stoch = 50
            else:
                stoch = (segment[-1] - min_rsi) / (max_rsi - min_rsi) * 100
            stoch_rsi_values.append(stoch)

        if len(stoch_rsi_values) < self.stoch_rsi_d_period:
            return {'k': stoch_rsi_values[-1] if stoch_rsi_values else 50, 'd': 50,
                    'overbought': False, 'oversold': False}

        # %K та %D
        k = stoch_rsi_values[-1]
        d = sum(stoch_rsi_values[-self.stoch_rsi_d_period:]) / self.stoch_rsi_d_period

        return {
            'k': k,
            'd': d,
            'overbought': k > self.stoch_rsi_overbought and d > self.stoch_rsi_overbought,
            'oversold': k < self.stoch_rsi_oversold and d < self.stoch_rsi_oversold
        }

    async def get_indicators(self, symbol: str) -> dict:
        try:
            klines = await self.exchange.get_klines(symbol, interval=self.timeframe, limit=100)
            if not klines or len(klines) < 50:
                return None

            closes = [k['close'] for k in klines]
            highs = [k['high'] for k in klines]
            lows = [k['low'] for k in klines]
            volumes = [k['volume'] for k in klines]

            # Існуючі індикатори
            ema9 = await self.calculate_ema(closes, 9)
            ema21 = await self.calculate_ema(closes, 21)
            rsi = await self.calculate_rsi(closes, self.rsi_period)
            macd = await self.calculate_macd(closes)
            stoch_rsi = await self.calculate_stoch_rsi(closes)

            # НОВІ індикатори
            bb = await self.calculate_bollinger_bands(closes, period=20, std_dev=2)
            vp = await self.calculate_volume_profile(klines, num_levels=20)
            vwap = await self.calculate_vwap(klines)

            current_price = closes[-1]
            self.current_prices[symbol] = current_price

            current_ema9 = ema9[-1] if ema9 else current_price
            current_ema21 = ema21[-1] if ema21 else current_price

            # Розрахунок фільтрів
            highest_20 = max(highs[-20:]) if len(highs) >= 20 else current_price
            lowest_20 = min(lows[-20:]) if len(lows) >= 20 else current_price
            price_position = (current_price - lowest_20) / (
                        highest_20 - lowest_20) * 100 if highest_20 != lowest_20 else 50
            is_at_peak = price_position > 80

            ema_trend_up = current_ema9 > current_ema21 and (ema9[-2] < ema9[-1] if len(ema9) > 1 else True)

            last_5_change = ((closes[-1] - closes[-6]) / closes[-6] * 100) if len(closes) >= 6 else 0
            too_much_gain = last_5_change > 3

            atr = await self._calculate_atr(highs, lows, closes)
            atr_percent = (atr / current_price) * 100 if current_price > 0 else 0
            too_volatile = atr_percent > 2

            avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
            volume_surge = volumes[-1] > avg_volume * 1.5 if len(volumes) >= 20 else False
            volume_too_high = volumes[-1] > avg_volume * 3 if len(volumes) >= 20 else False

            # Збір сигналів
            buy_signals = []
            sell_signals = []

            # EMA сигнали
            if current_ema9 > current_ema21:
                buy_signals.append('ema')
            if current_ema9 < current_ema21:
                sell_signals.append('ema')

            # RSI сигнали
            if rsi < self.rsi_oversold:
                buy_signals.append('rsi')
            if rsi > self.rsi_overbought:
                sell_signals.append('rsi')
            rsi_neutral = self.rsi_oversold <= rsi <= self.rsi_overbought

            # MACD сигнали
            if macd['bullish'] or (macd['histogram'] > 0 and macd['histogram'] > abs(macd['histogram'] * 0.5)):
                buy_signals.append('macd')
            if macd['bearish'] or (macd['histogram'] < 0 and macd['histogram'] < -abs(macd['histogram'] * 0.5)):
                sell_signals.append('macd')

            # StochRSI сигнали
            if stoch_rsi['oversold']:
                buy_signals.append('stoch_rsi')
            if stoch_rsi['overbought']:
                sell_signals.append('stoch_rsi')

            if volume_surge and not volume_too_high:
                buy_signals.append('volume')

            # ============= НОВІ СИГНАЛИ =============

            # Bollinger Bands сигнали
            if bb:
                if bb['oversold'] and bb['bb_percent_b'] < 0.1:
                    buy_signals.append('bb_oversold')
                    logger.debug(f"[{symbol}] BB перепродано: {bb['bb_percent_b']:.2f}")
                    #add_log("DEBUG", self.name, f"[{symbol}] BB перепродано: {bb['bb_percent_b']:.2f}")
                elif bb['overbought'] and bb['bb_percent_b'] > 0.9:
                    sell_signals.append('bb_overbought')
                    logger.debug(f"[{symbol}] BB перекуплено: {bb['bb_percent_b']:.2f}")
                    #add_log("DEBUG", self.name, f"[{symbol}] BB перекуплено: {bb['bb_percent_b']:.2f}")
                if bb['squeeze']:
                    buy_signals.append('bb_squeeze')
                    logger.debug(f"[{symbol}] BB звуження (squeeze) - можливий прорив")
                    add_log("DEBUG", self.name, f"[{symbol}] BB звуження (squeeze) - можливий прорив")

            # Volume Profile сигнали
            if vp:
                if vp['is_below_value_area']:
                    buy_signals.append('vp_support')
                    logger.debug(f"[{symbol}] Ціна нижче Value Area - підтримка")
                elif vp['is_above_value_area']:
                    sell_signals.append('vp_resistance')
                    logger.debug(f"[{symbol}] Ціна вище Value Area - опір")

                if vp['price_at_poc']:
                    buy_signals.append('vp_poc')
                    logger.debug(f"[{symbol}] Ціна на рівні POC ({vp['poc_price']})")

            # VWAP сигнали
            if vwap:
                if vwap['below_vwap'] and vwap['deviation_percent'] < -1:
                    buy_signals.append('vwap_support')
                    logger.debug(f"[{symbol}] Ціна нижче VWAP на {vwap['deviation_percent']:.1f}% - підтримка")
                elif vwap['above_vwap'] and vwap['deviation_percent'] > 1:
                    sell_signals.append('vwap_resistance')
                    logger.debug(f"[{symbol}] Ціна вище VWAP на {vwap['deviation_percent']:.1f}% - опір")

                if vwap['far_below']:
                    buy_signals.append('vwap_oversold')
                    logger.debug(f"[{symbol}] Ціна значно нижче VWAP (2σ) - сильна перепроданість")
                elif vwap['far_above']:
                    sell_signals.append('vwap_overbought')
                    logger.debug(f"[{symbol}] Ціна значно вище VWAP (2σ) - сильна перекупленість")

            # Фінальний сигнал з додатковими фільтрами
            buy_signal = (
                    len(buy_signals) >= 3 and
                    rsi_neutral and
                    not is_at_peak and
                    ema_trend_up and
                    not too_much_gain and
                    not too_volatile
            )

            strong_buy = (
                    len(buy_signals) >= 4 and
                    rsi_neutral and
                    not is_at_peak and
                    ema_trend_up and
                    not too_much_gain and
                    not too_volatile
            )

            sell_signal = len(sell_signals) >= 3 and rsi_neutral
            strong_sell = len(sell_signals) >= 4

            # Логування для діагностики
            add_log("DEBUG", self.name, f"[{symbol}] Сигнали: покупка={len(buy_signals)}/{buy_signals}, "
                         f"продаж={len(sell_signals)}/{sell_signals}, "
                         f"BB={bb['bandwidth']:.1f}% vod if bb else 'N/A', "
                         f"VWAP={vwap['deviation_percent']:.1f}% if vwap else 'N/A', "
                         f"VP POC={vp['poc_price'] if vp else 'N/A'}")

            logger.debug(f"[{symbol}] Сигнали: покупка={len(buy_signals)}/{buy_signals}, "
                         f"продаж={len(sell_signals)}/{sell_signals}, "
                         f"BB={bb['bandwidth']:.1f}% vod if bb else 'N/A', "
                         f"VWAP={vwap['deviation_percent']:.1f}% if vwap else 'N/A', "
                         f"VP POC={vp['poc_price'] if vp else 'N/A'}")

            return {
                'price': current_price,
                'ema9': current_ema9,
                'ema21': current_ema21,
                'rsi': rsi,
                'macd': macd,
                'stoch_rsi': stoch_rsi,
                'bb': bb,
                'vp': vp,
                'vwap': vwap,
                'volume_surge': volume_surge,
                'buy_signals': buy_signals,
                'sell_signals': sell_signals,
                'buy_signal': buy_signal,
                'sell_signal': sell_signal,
                'strong_buy': strong_buy,
                'strong_sell': strong_sell,
                'price_position': price_position,
                'atr_percent': atr_percent
            }

        except Exception as e:
            logger.error(f"Помилка отримання індикаторів {symbol}: {e}")
            add_log("ERROR", self.name, f"Помилка отримання індикаторів {symbol}: {e}")
            return None

    async def on_price_update(self, symbol: str, price: float):
        """Оновлення ціни через WebSocket"""
        self.current_prices[symbol] = price

        # Оновлюємо PnL для відкритих позицій
        if symbol in self.open_positions:
            position = self.open_positions[symbol]
            pnl_percent = (price - position['entry_price']) / position['entry_price'] * 100
            logger.debug(f"[{symbol}] Оновлення ціни: ${price:.2f}, PnL: {pnl_percent:.2f}%")

    async def _calculate_atr(self, highs: List[float], lows: List[float], closes: List[float],
                             period: int = 14) -> float:
        """Розрахунок ATR (Average True Range) для фільтра волатильності"""
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
            return 0

        true_ranges = []
        for i in range(1, len(highs)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i - 1]

            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            true_range = max(tr1, tr2, tr3)
            true_ranges.append(true_range)

        if not true_ranges:
            return 0

        return sum(true_ranges[-period:]) / period

    async def check_exit_signals(self, symbol: str, position: dict, current_price: float) -> str:
        if current_price > position['highest_price']:
            position['highest_price'] = current_price
        if current_price < position['lowest_price']:
            position['lowest_price'] = current_price

        pnl_percent = (current_price - position['entry_price']) / position['entry_price'] * 100

        # Використовуємо збережений Stop Loss
        stop_loss = position.get('stop_loss', position['entry_price'] * (1 - self.stop_loss_percent / 100))

        # Stop Loss перевірка
        if current_price <= stop_loss:
            return 'stop_loss'

        # Трейлінг стоп
        if self.trailing_stop_percent > 0:
            trailing_stop_price = position['highest_price'] * (1 - self.trailing_stop_percent / 100)
            if trailing_stop_price > position['entry_price'] and current_price <= trailing_stop_price:
                return 'trailing_stop'

        # Take profit
        if pnl_percent >= self.take_profit_percent:
            return 'take_profit'

        return 'hold'

    async def analyze(self) -> dict:
        if not self.enabled:
            return {'action': 'hold'}

        # Перевірка лімітів
        if not self.can_trade(self.trade_size_usdt):
            logger.warning(f"[Scalp] Торгівля заблокована: {self._block_reason}")
            add_log("WARNING", self.name, f"Торгівля заблокована: {self._block_reason}")
            return {'action': 'hold', 'blocked': True, 'reason': self._block_reason}

        results = {}
        signals_generated = []

        for symbol in self.symbols:
            try:
                indicators = await self.get_indicators(symbol)
                if not indicators:
                    add_log("DEBUG", self.name, f"Немає індикаторів для {symbol}")
                    continue

                price = indicators['price']

                add_log("DEBUG", self.name,
                        f"[{symbol}] RSI={indicators['rsi']:.1f}, MACD={indicators['macd']['histogram']:.4f}, "
                        f"Buy={indicators['buy_signals']}, Sell={indicators['sell_signals']}, "
                        f"Price=${price:.2f}")

                logger.info(f"[{symbol}] RSI={indicators['rsi']:.1f}, MACD={indicators['macd']['histogram']:.2f}, "
                            f"StochK={indicators['stoch_rsi']['k']:.1f}, Buy={indicators['buy_signals']}, Sell={indicators['sell_signals']}")

                if symbol in self.open_positions:
                    exit_signal = await self.check_exit_signals(symbol, self.open_positions[symbol], price)
                    if exit_signal != 'hold':
                        add_log("INFO", self.name, f"[{symbol}] Вихід за сигналом: {exit_signal}")
                        await self._close_position(symbol, exit_signal, price)
                else:
                    # Перевіряємо чи є вільний баланс
                    if self.available_balance < self.trade_size_usdt:
                        add_log("DEBUG", self.name, f"[{symbol}] Недостатньо балансу для входу")
                        logger.debug(f"[{symbol}] Недостатньо балансу для входу: потрібно ${self.trade_size_usdt:.2f}, "
                                     f"доступно ${self.available_balance:.2f}")
                        continue

                    # Логуємо сигнали
                    if indicators['strong_buy']:
                        logger.info(
                            f"[{symbol}] 🔥 СИЛЬНИЙ СИГНАЛ НА ПОКУПКУ! Підтвердження: {indicators['buy_signals']}")
                        signals_generated.append(f"{symbol}: STRONG_BUY")
                        add_log("INFO", self.name, f"[{symbol}] 🔥 СИЛЬНИЙ СИГНАЛ НА ПОКУПКУ!")
                        await self._open_position(symbol, price, strong=True)
                    elif indicators['buy_signal']:
                        add_log("INFO", self.name, f"[{symbol}] ✅ Сигнал на покупку!")
                        logger.info(f"[{symbol}] ✅ Сигнал на покупку! Підтвердження: {indicators['buy_signals']}")
                        signals_generated.append(f"{symbol}: BUY")
                        await self._open_position(symbol, price, strong=False)

            except Exception as e:
                logger.error(f"Помилка аналізу {symbol}: {e}")
                add_log("ERROR", self.name, f"Помилка аналізу {symbol}: {e}")

        add_log("DEBUG", self.name, f"Аналіз завершено, сигналів: {len(signals_generated)}")
        return {'action': 'hold', 'results': results, 'signals': signals_generated}

    async def execute(self, signal: dict):
        pass

    async def update_settings(self, symbols=None, trade_size_usdt=None,
                              take_profit_percent=None, stop_loss_percent=None,
                              trailing_stop_percent=None, timeframe=None):
        if symbols is not None:
            self.symbols = symbols
        if trade_size_usdt is not None:
            self.trade_size_usdt = trade_size_usdt
        if take_profit_percent is not None:
            self.take_profit_percent = take_profit_percent
        if stop_loss_percent is not None:
            self.stop_loss_percent = stop_loss_percent
        if trailing_stop_percent is not None:
            self.trailing_stop_percent = trailing_stop_percent
        if timeframe is not None:
            self.timeframe = timeframe

        save_strategy_settings('scalp',
                               symbols=self.symbols,
                               trade_size_usdt=self.trade_size_usdt,
                               take_profit_percent=self.take_profit_percent,
                               stop_loss_percent=self.stop_loss_percent,
                               trailing_stop_percent=self.trailing_stop_percent,
                               timeframe=self.timeframe
                               )

        add_log("INFO", self.name, f"Оновлено налаштування: {self.symbols}, розмір=${self.trade_size_usdt}, таймфрейм={self.timeframe}мін")
        return True

    async def _open_position(self, symbol: str, price: float, strong: bool = False):
        # Отримуємо актуальну ціну
        current_price = await self.exchange.get_current_price(symbol)

        # Перевірка зміни ціни
        price_diff_pct = abs(current_price - price) / price * 100 if price > 0 else 0
        if price_diff_pct > 2:
            logger.warning(f"[{symbol}] Ціна змінилась на {price_diff_pct:.2f}%! Позиція НЕ відкрита")
            return

        quantity = self.trade_size_usdt / current_price
        cost = quantity * current_price

        if self.available_balance < cost:
            logger.warning(f"[{symbol}] Недостатньо балансу: потрібно ${cost:.2f}")
            return

        order_id = f"scalp_{symbol}_{int(datetime.now().timestamp())}_{self.strategy_id}"

        result = await self.exchange.create_order(symbol, 'buy', 'Market', quantity, current_price)

        if result.get('error'):
            logger.error(f"Помилка відкриття позиції {symbol}: {result}")
            return

        # Розраховуємо Stop Loss ціну
        stop_loss_price = current_price * (1 - self.stop_loss_percent / 100)
        take_profit_price = current_price * (1 + self.take_profit_percent / 100)

        self.open_positions[symbol] = {
            'order_id': order_id,
            'entry_price': current_price,
            'quantity': quantity,
            'highest_price': current_price,
            'lowest_price': current_price,
            'stop_loss': stop_loss_price,
            'take_profit': take_profit_price,
            'strong_signal': strong,
            'opened_at': datetime.now().isoformat()
        }

        self.locked_balance += cost

        # ВАЖЛИВО: зберігаємо ордер в БД
        self._save_order(order_id, symbol, 'buy', current_price, quantity, 'open', stop_loss_price, take_profit_price)

        await self._save_trade_chart_data(order_id, symbol, datetime.now())

        signal_type = "🔥 СИЛЬНИЙ СИГНАЛ" if strong else "✅ Звичайний сигнал"
        add_log("INFO", self.name,
                f"📈 Відкрито LONG позицію {symbol} @ ${current_price:.2f}, SL=${stop_loss_price:.2f} ({signal_type})")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📈 *ВІДКРИТО ПОЗИЦІЮ* (Scalp)\n"
                f"└ Пара: `{symbol}`\n"
                f"└ Ціна: `${current_price:.2f}`\n"
                f"└ SL: `${stop_loss_price:.2f}`\n"
                f"└ Розмір: `{quantity:.6f}`\n"
                f"└ Сигнал: {signal_type}",
                parse_mode='Markdown'
            )

        self.increment_daily_trades()
        self.update_balance_for_drawdown()

    async def _close_position(self, symbol: str, reason: str, price: float):
        position = self.open_positions.get(symbol)
        # Після закриття позиції, оновлюємо свічки до моменту закриття
        opened_at_str = position.get('opened_at')
        if opened_at_str:
            opened_at = datetime.fromisoformat(opened_at_str)
        else:
            # Якщо немає opened_at, використовуємо поточний час
            opened_at = datetime.now()

        await self._save_trade_chart_data(
            position['order_id'],
            symbol,
            opened_at,
            datetime.now()
        )
        if not position:
            logger.warning(f"[{symbol}] Позиція не знайдена для закриття")
            return

        try:
            revenue = position['quantity'] * price
            cost = position['quantity'] * position['entry_price']
            commission = revenue * 0.0018 + cost * 0.0018
            pnl = revenue - cost - commission

            result = await self.exchange.create_order(symbol, 'sell', 'Market', position['quantity'], price)

            if result.get('error'):
                logger.error(f"Помилка закриття позиції {symbol}: {result}")
                return

            self.balance += revenue - commission
            self.locked_balance -= cost
            self.total_pnl += pnl
            self.total_trades += 1

            if pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1

            # Оновлюємо статус ордера з ціною закриття
            with get_db() as conn:
                conn.execute(
                    "UPDATE orders SET status = 'closed', closed_at = ?, closed_price = ?, pnl = ?, commission = ? WHERE order_id = ?",
                    (datetime.now().isoformat(), price, pnl, commission, position['order_id'])
                )

            reason_text = {
                'take_profit': '🎯 Take Profit',
                'stop_loss': '🛑 Stop Loss',
                'trailing_stop': '📉 Trailing Stop',
                'emergency_stop': '🛑 Екстрена зупинка',
                'reset': '🔄 Скидання'
            }.get(reason, reason)

            order_id_saved = position['order_id']
            entry_price_saved = position['entry_price']
            del self.open_positions[symbol]

            add_log("INFO", self.name,
                    f"📉 Закрито LONG позицію {symbol} @ ${price:.2f} | PnL: ${pnl:.2f} | {reason_text}")

            # Telegram сповіщення
            if self.telegram_bot:
                pnl_icon = "✅" if pnl >= 0 else "❌"
                await self.telegram_bot.send_notification(
                    f"📉 *ЗАКРИТО ПОЗИЦІЮ* (Scalp)\n"
                    f"└ Пара: `{symbol}`\n"
                    f"└ Ціна входу: `${entry_price_saved:.2f}`\n"
                    f"└ Ціна виходу: `${price:.2f}`\n"
                    f"└ PnL: {pnl_icon} `${pnl:.2f}`\n"
                    f"└ Причина: {reason_text}",
                    parse_mode='Markdown'
                )

            self.update_balance_for_drawdown()

        except Exception as e:
            logger.error(f"Критична помилка при закритті позиції {symbol}: {e}")
            # Примусово видаляємо позицію, щоб уникнути блокування
            if symbol in self.open_positions:
                del self.open_positions[symbol]

    async def calculate_bollinger_bands(self, prices: List[float], period: int = 20, std_dev: float = 2) -> Dict:
        """
        Розрахунок смуг Боллінджера

        Повертає:
        - upper_band: верхня смуга
        - middle_band: середня смуга (SMA)
        - lower_band: нижня смуга
        - bandwidth: ширина смуг (волатильність)
        - bb_percent_b: позиція ціни відносно смуг (0-1)
        - squeeze: звуження смуг (перед проривом)
        - overbought/oversold: ціна за межами смуг
        """
        if len(prices) < period:
            return None

        # Розраховуємо SMA (просте ковзне середнє)
        sma = sum(prices[-period:]) / period

        # Розраховуємо стандартне відхилення
        variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
        std = variance ** 0.5

        upper_band = sma + (std_dev * std)
        lower_band = sma - (std_dev * std)
        bandwidth = (upper_band - lower_band) / sma * 100 if sma > 0 else 0

        # Позиція ціни відносно смуг
        current_price = prices[-1]
        if upper_band != lower_band:
            bb_percent_b = (current_price - lower_band) / (upper_band - lower_band)
            bb_percent_b = max(0, min(1, bb_percent_b))  # обмежуємо від 0 до 1
        else:
            bb_percent_b = 0.5

        # Сигнали
        squeeze = bandwidth < 5  # Низька волатильність (перед проривом)
        overbought = current_price > upper_band  # Ціна вище верхньої смуги
        oversold = current_price < lower_band  # Ціна нижче нижньої смуги

        return {
            'upper_band': round(upper_band, 2),
            'middle_band': round(sma, 2),
            'lower_band': round(lower_band, 2),
            'bandwidth': round(bandwidth, 2),
            'bb_percent_b': round(bb_percent_b, 3),
            'squeeze': squeeze,
            'overbought': overbought,
            'oversold': oversold
        }

    async def calculate_volume_profile(self, klines: List[dict], num_levels: int = 20) -> Dict:
        """
        Профіль об'єму - показує розподіл об'ємів за рівнями цін

        Повертає:
        - poc_price: ціна з максимальним об'ємом (Point of Control)
        - value_area_high: верхня межа зони цінності (70% об'єму)
        - value_area_low: нижня межа зони цінності
        - high_volume_nodes: рівні з високим об'ємом
        - low_volume_nodes: рівні з низьким об'ємом
        """
        if len(klines) < 30:
            return None

        # Визначаємо діапазон цін
        all_prices = []
        for k in klines[-50:]:  # останні 50 свічок
            all_prices.extend([k['high'], k['low'], k['close']])

        min_price = min(all_prices)
        max_price = max(all_prices)
        price_range = max_price - min_price

        if price_range == 0:
            return None

        level_size = price_range / num_levels

        # Групуємо об'єми по рівнях
        volume_profile = {}
        for k in klines[-50:]:
            price_level = int((k['close'] - min_price) / level_size)
            price_level = min(price_level, num_levels - 1)  # обмежуємо
            if price_level not in volume_profile:
                volume_profile[price_level] = 0
            volume_profile[price_level] += k['volume']

        # Знаходимо POC (Point of Control) - рівень з максимальним об'ємом
        poc_level = max(volume_profile, key=volume_profile.get)
        poc_price = min_price + (poc_level + 0.5) * level_size

        # Розраховуємо Value Area (70% об'єму)
        total_volume = sum(volume_profile.values())
        target_volume = total_volume * 0.7

        # Сортуємо рівні за об'ємом (від найбільшого до найменшого)
        sorted_levels = sorted(volume_profile.items(), key=lambda x: x[1], reverse=True)

        accumulated = 0
        value_area_levels = []
        for level, vol in sorted_levels:
            value_area_levels.append(level)
            accumulated += vol
            if accumulated >= target_volume:
                break

        value_area_high = min_price + (max(value_area_levels) + 1) * level_size
        value_area_low = min_price + min(value_area_levels) * level_size

        # Вузли з високим/низьким об'ємом
        avg_volume = total_volume / num_levels if num_levels > 0 else 0
        high_volume_nodes = []
        low_volume_nodes = []

        for level, vol in volume_profile.items():
            price = min_price + (level + 0.5) * level_size
            if vol > avg_volume * 1.5:
                high_volume_nodes.append(round(price, 2))
            elif vol < avg_volume * 0.5:
                low_volume_nodes.append(round(price, 2))

        current_price = klines[-1]['close']

        return {
            'poc_price': round(poc_price, 2),
            'value_area_high': round(value_area_high, 2),
            'value_area_low': round(value_area_low, 2),
            'high_volume_nodes': high_volume_nodes[:5],  # максимум 5
            'low_volume_nodes': low_volume_nodes[:5],
            'is_above_value_area': current_price > value_area_high,
            'is_below_value_area': current_price < value_area_low,
            'price_at_poc': abs(current_price - poc_price) < level_size
        }

    async def calculate_vwap(self, klines: List[dict]) -> Dict:
        """
        VWAP - середньозважена ціна за об'ємом

        Використовується інституційними трейдерами для визначення справедливої ціни
        """
        if len(klines) < 20:
            return None

        total_volume = 0
        total_value = 0

        # Використовуємо всі доступні свічки для розрахунку
        for k in klines:
            # Typical price = (high + low + close) / 3
            typical_price = (k['high'] + k['low'] + k['close']) / 3
            total_value += typical_price * k['volume']
            total_volume += k['volume']

        if total_volume == 0:
            return None

        vwap = total_value / total_volume
        current_price = klines[-1]['close']

        # Відхилення від VWAP
        deviation = (current_price - vwap) / vwap * 100 if vwap > 0 else 0

        # Розраховуємо стандартні відхилення для смуг навколо VWAP
        if len(klines) > 10:
            # Розрахунок дисперсії
            variance_sum = 0
            for k in klines[-20:]:
                typical = (k['high'] + k['low'] + k['close']) / 3
                variance_sum += ((typical - vwap) ** 2) * k['volume']

            variance = variance_sum / total_volume if total_volume > 0 else 0
            std_dev = variance ** 0.5

            vwap_upper_2 = vwap + 2 * std_dev
            vwap_lower_2 = vwap - 2 * std_dev
        else:
            vwap_upper_2 = vwap * 1.02
            vwap_lower_2 = vwap * 0.98

        # Сигнали
        above_vwap = current_price > vwap  # Вище справедливої ціни
        below_vwap = current_price < vwap  # Нижче справедливої ціни
        far_above = current_price > vwap_upper_2  # Значно вище (перекуплено)
        far_below = current_price < vwap_lower_2  # Значно нижче (перепродано)
        vwap_rejection = abs(deviation) > 2  # Відхилення >2%

        return {
            'vwap': round(vwap, 2),
            'vwap_upper_2': round(vwap_upper_2, 2),
            'vwap_lower_2': round(vwap_lower_2, 2),
            'deviation_percent': round(deviation, 2),
            'above_vwap': above_vwap,
            'below_vwap': below_vwap,
            'far_above': far_above,
            'far_below': far_below,
            'vwap_rejection': vwap_rejection
        }

    async def get_status(self) -> dict:
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades else 0
        return {
            'id': self.strategy_id,
            'name': self.name,
            'enabled': self.enabled,
            'mode': self.mode,
            'balance': round(self.balance, 2),
            'locked_balance': round(self.locked_balance, 2),
            'available_balance': round(self.available_balance, 2),
            'total_pnl': round(self.total_pnl, 2),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': round(win_rate, 1),
            'symbols': self.symbols,
            'open_positions': self.open_positions,
            'current_prices': self.current_prices,
            'trade_size_usdt': self.trade_size_usdt,
            'take_profit_percent': self.take_profit_percent,
            'stop_loss_percent': self.stop_loss_percent,
            'trailing_stop_percent': self.trailing_stop_percent,
            # Індикатори
            'rsi_period': self.rsi_period,
            'macd_fast': self.macd_fast,
            'macd_slow': self.macd_slow,
            'stoch_rsi_period': self.stoch_rsi_period,
            # Ліміти
            'daily_trades_count': self.daily_trades_count,
            'max_daily_trades': self.max_daily_trades,
            'is_blocked': self._is_blocked,
            'block_reason': self._block_reason,
            'timeframe': self.timeframe
        }

    async def reset(self):
        logger.warning(f"Скидання Scalp стратегії")
        symbols_to_close = list(self.open_positions.keys())
        for symbol in symbols_to_close:
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                try:
                    await self._close_position(symbol, 'reset', price)
                except Exception as e:
                    logger.error(f"Помилка закриття позиції {symbol} при скиданні: {e}")
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        with get_db() as conn:
            conn.execute("DELETE FROM orders WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM balances WHERE strategy_id=?", (self.strategy_id,))
        self._save_balance()

        # Скидаємо ліміти
        await self.reset_limits()

        add_log("INFO", self.name, "Стратегію скинуто")

    async def _save_trade_chart_data(self, order_id: str, symbol: str, entry_time: datetime,
                                     exit_time: datetime = None):
        """Збереження свічок для угоди"""
        try:
            from database.db import save_price_history

            # Визначаємо діапазон свічок
            start_ts = int(entry_time.timestamp() * 1000) - 30 * 60 * 1000  # 30 хв до входу

            if exit_time:
                end_ts = int(exit_time.timestamp() * 1000) + 30 * 60 * 1000  # 30 хв після виходу
            else:
                end_ts = int(entry_time.timestamp() * 1000) + 2 * 3600 * 1000  # 2 години після входу

            duration_min = (end_ts - start_ts) // 60000
            needed = min(max(duration_min + 20, 100), 1000)

            # Отримуємо свічки
            klines = await self.exchange.get_klines(symbol, '1', limit=needed)

            # Сортуємо та фільтруємо
            klines.sort(key=lambda k: k['timestamp'])
            filtered_klines = [k for k in klines if start_ts <= k['timestamp'] <= end_ts]

            # Додаємо time_iso для зручності
            for k in filtered_klines:
                from datetime import datetime
                k['time_iso'] = datetime.utcfromtimestamp(k['timestamp'] / 1000).strftime('%Y-%m-%dT%H:%M:%S')

            # Зберігаємо в БД
            save_price_history(order_id, symbol, filtered_klines)
            logger.info(f"[{symbol}] Збережено {len(filtered_klines)} свічок для угоди {order_id}")

        except Exception as e:
            logger.error(f"Помилка збереження свічок для угоди {order_id}: {e}")


    async def emergency_stop(self):
        logger.warning(f"Екстрена зупинка Scalp стратегії")
        # Створюємо копію ключів, щоб уникнути помилки "dict changed during iteration"
        symbols_to_close = list(self.open_positions.keys())
        for symbol in symbols_to_close:
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                try:
                    await self._close_position(symbol, 'emergency_stop', price)
                except Exception as e:
                    logger.error(f"Помилка закриття позиції {symbol} при екстреній зупинці: {e}")
        await self.stop()