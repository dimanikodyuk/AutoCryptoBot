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

        self._load_history()

        logger.info(f"ScalpStrategy ініціалізовано для пар: {self.symbols}")

    async def start(self):
        await super().start()
        save_strategy_settings('scalp', enabled=True)
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info("ScalpStrategy: цикл аналізу запущено")

    async def stop(self):
        """Зупинка стратегії"""
        if self._analysis_task:
            self._analysis_task.cancel()
            self._analysis_task = None
        await super().stop()
        save_strategy_settings('scalp', enabled=False)
        logger.info("ScalpStrategy: зупинено")

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
                    self.open_positions[o['symbol']] = {
                        'order_id': o['order_id'],
                        'entry_price': o['price'],
                        'quantity': o['quantity'],
                        'highest_price': o['price'],
                        'lowest_price': o['price']
                    }
                    self.locked_balance += o['quantity'] * o['price']

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

    def _save_order(self, order_id: str, symbol: str, side: str, price: float, quantity: float, status: str):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO orders 
                (order_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, self.strategy_id, symbol, side, price, quantity, status, 'Market',
                  datetime.now().isoformat()))

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
            klines = await self.exchange.get_klines(symbol, interval='1', limit=100)
            if not klines or len(klines) < 50:
                return None

            closes = [k['close'] for k in klines]
            highs = [k['high'] for k in klines]
            lows = [k['low'] for k in klines]
            volumes = [k['volume'] for k in klines]

            # EMA
            ema9 = await self.calculate_ema(closes, 9)
            ema21 = await self.calculate_ema(closes, 21)

            # RSI
            rsi = await self.calculate_rsi(closes, self.rsi_period)

            # MACD
            macd = await self.calculate_macd(closes)

            # StochRSI
            stoch_rsi = await self.calculate_stoch_rsi(closes)

            current_price = closes[-1]
            self.current_prices[symbol] = current_price

            current_ema9 = ema9[-1] if ema9 else current_price
            current_ema21 = ema21[-1] if ema21 else current_price

            # ============= НОВІ ФІЛЬТРИ =============

            # 1. Перевірка, чи ціна не на максимумі (не купуємо на піку)
            highest_20 = max(highs[-20:]) if len(highs) >= 20 else current_price
            lowest_20 = min(lows[-20:]) if len(lows) >= 20 else current_price
            price_position = (current_price - lowest_20) / (
                        highest_20 - lowest_20) * 100 if highest_20 != lowest_20 else 50

            # Не купуємо якщо ціна вище 80% діапазону (близько до максимуму)
            is_at_peak = price_position > 80

            # 2. Перевірка тренду (купуємо тільки на зростаючому тренді)
            # Перевіряємо чи EMA9 вище EMA21 і чи EMA9 зростає
            ema_trend_up = current_ema9 > current_ema21 and (ema9[-2] < ema9[-1] if len(ema9) > 1 else True)

            # 3. Перевірка, що не купуємо після великого зростання
            last_5_change = ((closes[-1] - closes[-6]) / closes[-6] * 100) if len(closes) >= 6 else 0
            too_much_gain = last_5_change > 3  # Зросло більше ніж на 3% за 5 свічок

            # 4. Додатковий фільтр волатильності
            atr = await self._calculate_atr(highs, lows, closes)
            atr_percent = (atr / current_price) * 100 if current_price > 0 else 0
            too_volatile = atr_percent > 2  # Занадто волатильно (>2%)

            # Об'єм
            avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
            volume_surge = volumes[-1] > avg_volume * 1.5 if len(volumes) >= 20 else False

            # Додатковий фільтр об'єму - не купуємо при аномально високому об'ємі (розворот)
            volume_too_high = volumes[-1] > avg_volume * 3 if len(volumes) >= 20 else False

            # Комбінований сигнал на покупку
            buy_signals = []
            sell_signals = []

            # EMA сигнал
            ema_bullish = current_ema9 > current_ema21
            ema_bearish = current_ema9 < current_ema21

            # RSI сигнал
            rsi_bullish = rsi < self.rsi_oversold
            rsi_bearish = rsi > self.rsi_overbought
            rsi_neutral = self.rsi_oversold <= rsi <= self.rsi_overbought

            # MACD сигнал
            macd_bullish = macd['bullish'] or (
                        macd['histogram'] > 0 and macd['histogram'] > abs(macd['histogram'] * 0.5))
            macd_bearish = macd['bearish'] or (
                        macd['histogram'] < 0 and macd['histogram'] < -abs(macd['histogram'] * 0.5))

            # StochRSI сигнал
            stoch_bullish = stoch_rsi['oversold']
            stoch_bearish = stoch_rsi['overbought']

            # Підрахунок сигналів
            if ema_bullish:
                buy_signals.append('ema')
            if rsi_bullish:
                buy_signals.append('rsi')
            if macd_bullish:
                buy_signals.append('macd')
            if stoch_bullish:
                buy_signals.append('stoch_rsi')
            if volume_surge and not volume_too_high:
                buy_signals.append('volume')

            if ema_bearish:
                sell_signals.append('ema')
            if rsi_bearish:
                sell_signals.append('rsi')
            if macd_bearish:
                sell_signals.append('macd')
            if stoch_bearish:
                sell_signals.append('stoch_rsi')

            # Фінальний сигнал з додатковими фільтрами
            # Для покупки: потрібно 3+ сигнали + не на піку + тренд вгору + не перегріто
            buy_signal = (
                    len(buy_signals) >= 3 and
                    rsi_neutral and
                    not is_at_peak and
                    ema_trend_up and
                    not too_much_gain and
                    not too_volatile
            )

            # Сильний сигнал (4+ підтверджень)
            strong_buy = (
                    len(buy_signals) >= 4 and
                    rsi_neutral and
                    not is_at_peak and
                    ema_trend_up and
                    not too_much_gain and
                    not too_volatile
            )

            # Сигнал на продаж (для відкриття SHORT - не використовуємо в скальпінгу поки)
            sell_signal = len(sell_signals) >= 3 and rsi_neutral
            strong_sell = len(sell_signals) >= 4

            # Логування для діагностики
            logger.debug(f"[{symbol}] Сигнали: покупка={len(buy_signals)}/{buy_signals}, "
                         f"позиція ціни={price_position:.1f}%, тренд={ema_trend_up}, "
                         f"перегрів={too_much_gain}, волатильність={atr_percent:.2f}%")

            return {
                'price': current_price,
                'ema9': current_ema9,
                'ema21': current_ema21,
                'rsi': rsi,
                'macd': macd,
                'stoch_rsi': stoch_rsi,
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
            return None

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

        # Трейлінг стоп
        trailing_stop_price = position['highest_price'] * (1 - self.trailing_stop_percent / 100)
        if trailing_stop_price > position['entry_price'] and current_price <= trailing_stop_price:
            return 'trailing_stop'

        # Take profit та Stop loss
        if pnl_percent >= self.take_profit_percent:
            return 'take_profit'
        if pnl_percent <= -self.stop_loss_percent:
            return 'stop_loss'

        return 'hold'

    async def analyze(self) -> dict:
        if not self.enabled:
            return {'action': 'hold'}

        # Перевірка лімітів
        if not self.can_trade(self.trade_size_usdt):
            logger.warning(f"[Scalp] Торгівля заблокована: {self._block_reason}")
            return {'action': 'hold', 'blocked': True, 'reason': self._block_reason}

        results = {}
        signals_generated = []

        for symbol in self.symbols:
            try:
                indicators = await self.get_indicators(symbol)
                if not indicators:
                    continue

                price = indicators['price']
                logger.info(f"[{symbol}] RSI={indicators['rsi']:.1f}, MACD={indicators['macd']['histogram']:.2f}, "
                            f"StochK={indicators['stoch_rsi']['k']:.1f}, Buy={indicators['buy_signals']}, Sell={indicators['sell_signals']}")

                if symbol in self.open_positions:
                    exit_signal = await self.check_exit_signals(symbol, self.open_positions[symbol], price)
                    if exit_signal != 'hold':
                        await self._close_position(symbol, exit_signal, price)
                else:
                    # Перевіряємо чи є вільний баланс
                    if self.available_balance < self.trade_size_usdt:
                        logger.debug(f"[{symbol}] Недостатньо балансу для входу: потрібно ${self.trade_size_usdt:.2f}, "
                                     f"доступно ${self.available_balance:.2f}")
                        continue

                    # Логуємо сигнали
                    if indicators['strong_buy']:
                        logger.info(
                            f"[{symbol}] 🔥 СИЛЬНИЙ СИГНАЛ НА ПОКУПКУ! Підтвердження: {indicators['buy_signals']}")
                        signals_generated.append(f"{symbol}: STRONG_BUY")
                        await self._open_position(symbol, price, strong=True)
                    elif indicators['buy_signal']:
                        logger.info(f"[{symbol}] ✅ Сигнал на покупку! Підтвердження: {indicators['buy_signals']}")
                        signals_generated.append(f"{symbol}: BUY")
                        await self._open_position(symbol, price, strong=False)

            except Exception as e:
                logger.error(f"Помилка аналізу {symbol}: {e}")

        return {'action': 'hold', 'results': results, 'signals': signals_generated}

    async def execute(self, signal: dict):
        pass

    async def update_settings(self, symbols=None, trade_size_usdt=None,
                              take_profit_percent=None, stop_loss_percent=None,
                              trailing_stop_percent=None):
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

        save_strategy_settings('scalp',
                               symbols=self.symbols,
                               trade_size_usdt=self.trade_size_usdt,
                               take_profit_percent=self.take_profit_percent,
                               stop_loss_percent=self.stop_loss_percent,
                               trailing_stop_percent=self.trailing_stop_percent
                               )

        add_log("INFO", self.name, f"Оновлено налаштування: {self.symbols}, розмір=${self.trade_size_usdt}")
        return True

    async def _open_position(self, symbol: str, price: float, strong: bool = False):
        quantity = self.trade_size_usdt / price

        cost = quantity * price

        if self.available_balance < cost:
            logger.warning(f"[{symbol}] Недостатньо балансу для входу: потрібно ${cost:.2f}")
            return

        order_id = f"scalp_{symbol}_{int(datetime.now().timestamp())}_{self.strategy_id}"

        result = await self.exchange.create_order(symbol, 'buy', 'Market', quantity, price)

        if result.get('error'):
            logger.error(f"Помилка відкриття позиції {symbol}: {result}")
            return

        self.open_positions[symbol] = {
            'order_id': order_id,
            'entry_price': price,
            'quantity': quantity,
            'highest_price': price,
            'lowest_price': price,
            'strong_signal': strong
        }
        self.locked_balance += cost
        self._save_order(order_id, symbol, 'buy', price, quantity, 'open')

        signal_type = "🔥 СИЛЬНИЙ СИГНАЛ" if strong else "✅ Звичайний сигнал"
        add_log("INFO", self.name, f"📈 Відкрито LONG позицію {symbol} @ ${price:.2f} ({signal_type})")

        # Оновлюємо лічильники
        self.increment_daily_trades()
        self.update_balance_for_drawdown()

    async def _close_position(self, symbol: str, reason: str, price: float):
        position = self.open_positions.get(symbol)
        if not position:
            return

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

        self._update_order(position['order_id'], pnl=pnl, commission=commission, status='closed')

        reason_text = {
            'take_profit': '🎯 Take Profit',
            'stop_loss': '🛑 Stop Loss',
            'trailing_stop': '📉 Trailing Stop'
        }.get(reason, reason)

        del self.open_positions[symbol]

        add_log("INFO", self.name,
                f"📉 Закрито LONG позицію {symbol} @ ${price:.2f} | PnL: ${pnl:.2f} | {reason_text}")

        # Оновлюємо drawdown
        self.update_balance_for_drawdown()

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
            'block_reason': self._block_reason
        }

    async def reset(self):
        logger.warning(f"Скидання Scalp стратегії")
        for symbol in list(self.open_positions.keys()):
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                await self._close_position(symbol, 'reset', price)
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

    async def emergency_stop(self):
        logger.warning(f"Екстрена зупинка Scalp стратегії")
        for symbol in list(self.open_positions.keys()):
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                await self._close_position(symbol, 'emergency_stop', price)
        await self.stop()