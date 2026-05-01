"""Стратегія технічного аналізу"""
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from decimal import Decimal
import uuid

from trading.strategies.base import BaseStrategy
from database.db import get_db, add_log
from config_manager import get_strategy_settings, save_strategy_settings

logger = logging.getLogger(__name__)


class TechAnalysisStrategy(BaseStrategy):
    """Стратегія на основі технічного аналізу"""

    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        super().__init__(strategy_id, name, mode, exchange)

        saved = get_strategy_settings('tech_analysis')

        self.symbols = saved.get('symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
        self.enabled = saved.get('enabled', False)
        self.timeframe = saved.get('timeframe', '60')  # 1 година

        # Параметри торгівлі
        self.trade_size_percent = saved.get('trade_size_percent', 50)
        self.take_profit_percent = saved.get('take_profit_percent', 4.0)
        self.stop_loss_percent = saved.get('stop_loss_percent', 2.0)
        self.min_confidence = saved.get('min_confidence', 65.0)

        # ОБМЕЖЕННЯ
        self.max_concurrent_positions = 2
        self.min_trade_amount = 10
        self.cooldown_minutes = 15

        # Стан (ініціалізуємо ДО використання)
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        # Тепер можна використовувати self.balance
        self.max_total_position_value = self.balance * 0.5  # ← тепер працює

        # Поточні позиції
        self.open_positions: Dict[str, dict] = {}
        self.current_prices: Dict[str, float] = {}
        self.last_trade_time: Dict[str, datetime] = {}
        self.last_signal_time: Dict[str, datetime] = {}

        self._analysis_task = None
        self._load_history()

        logger.info(f"TechAnalysisStrategy ініціалізовано для пар: {self.symbols}")
        logger.info(f"Обмеження: макс. позицій={self.max_concurrent_positions}, мін. сума=${self.min_trade_amount}")
    async def start(self):
        await super().start()
        save_strategy_settings('tech_analysis', enabled=True)
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info("TechAnalysisStrategy: цикл аналізу запущено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, True)

    async def stop(self):
        if self._analysis_task:
            self._analysis_task.cancel()
            self._analysis_task = None
        await super().stop()
        save_strategy_settings('tech_analysis', enabled=False)
        logger.info("TechAnalysisStrategy: зупинено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, False)

    async def _save_trade_chart_data(self, order_id: str, symbol: str, opened_at: datetime):
        """Збереження свічок для угоди"""
        try:
            from database.db import save_price_history
            import traceback

            logger.info(f"[SAVE_CHART] Початок збереження для {order_id}")

            # ВИКОРИСТОВУЄМО 1-ХВИЛИННИЙ ТАЙМФРЕЙМ
            klines = await self.exchange.get_klines(symbol, '1', limit=500)

            if not klines:
                logger.error(f"[SAVE_CHART] Немає свічок для {symbol}")
                return

            logger.info(f"[SAVE_CHART] Отримано {len(klines)} свічок (1хв)")
            klines.sort(key=lambda k: k['timestamp'])

            # Беремо останні 300 свічок (5 годин)
            filtered = klines[-300:] if len(klines) > 300 else klines

            for k in filtered:
                k['time_iso'] = datetime.utcfromtimestamp(k['timestamp'] / 1000).strftime('%Y-%m-%dT%H:%M:%S')

            # Видаляємо старі свічки для цього order_id
            with get_db() as conn:
                conn.execute("DELETE FROM price_history WHERE order_id = ?", (order_id,))

            save_price_history(order_id, symbol, filtered)
            logger.info(f"[SAVE_CHART] ✅ Збережено {len(filtered)} свічок для {order_id}")

            # Перевірка
            from database.db import get_price_history
            saved = get_price_history(order_id)
            logger.info(f"[SAVE_CHART] Перевірка: в БД {len(saved)} свічок")

        except Exception as e:
            logger.error(f"[SAVE_CHART] ПОМИЛКА: {e}")
            traceback.print_exc()

    async def _save_candles_for_order(self, order_id: str, symbol: str, entry_price: float, opened_at: datetime):
        """Збереження свічок для угоди (1-хвилинний таймфрейм)"""
        try:
            from database.db import save_price_history
            import traceback

            logger.info(f"📊 [SAVE_CANDLES] Початок для {order_id}, ціна={entry_price}")

            # Видаляємо старі свічки для цього ордера
            with get_db() as conn:
                conn.execute("DELETE FROM price_history WHERE order_id = ?", (order_id,))
                logger.info(f"📊 [SAVE_CANDLES] Видалено старі свічки для {order_id}")

            # Отримуємо свічки за 1 хвилину, останні 400 штук
            klines = await self.exchange.get_klines(symbol, '1', limit=400)

            if not klines:
                logger.error(f"📊 [SAVE_CANDLES] НЕМАЄ СВІЧОК для {symbol}")
                return

            logger.info(f"📊 [SAVE_CANDLES] Отримано {len(klines)} свічок")
            klines.sort(key=lambda k: k['timestamp'])

            # Додаємо time_iso для кожної свічки
            for k in klines:
                k['time_iso'] = datetime.utcfromtimestamp(k['timestamp'] / 1000).strftime('%Y-%m-%dT%H:%M:%S')

            # ВИДАЛЯЄМО ФІЛЬТРАЦІЮ - зберігаємо ВСІ отримані свічки
            # Це гарантує, що точка входу буде в межах графіка
            save_price_history(order_id, symbol, klines)

            logger.info(f"📊 [SAVE_CANDLES] ✅ Збережено ВСІ {len(klines)} свічок для {order_id}")

            # Перевіряємо чи збереглось
            with get_db() as conn:
                count = conn.execute("SELECT COUNT(*) FROM price_history WHERE order_id = ?", (order_id,)).fetchone()[0]
                logger.info(f"📊 [SAVE_CANDLES] Перевірка: в БД {count} свічок")

        except Exception as e:
            logger.error(f"📊 [SAVE_CANDLES] ПОМИЛКА: {e}")
            traceback.print_exc()

    @property
    def available_balance(self):
        return self.balance - self.locked_balance

    def get_current_balance(self) -> float:
        return self.balance

    def can_open_new_position(self, symbol: str = None, required_amount: float = None) -> bool:
        """Перевірка чи можна відкрити нову позицію"""
        # Перевіряємо кількість позицій
        if len(self.open_positions) >= self.max_concurrent_positions:
            logger.debug(f"[TechAnalysis] Максимум позицій ({self.max_concurrent_positions}) досягнуто")
            return False

        # Перевіряємо загальну заблоковану суму
        if self.locked_balance >= self.max_total_position_value:
            logger.debug(f"[TechAnalysis] Перевищено ліміт заблокованих коштів: ${self.locked_balance:.2f} >= ${self.max_total_position_value:.2f}")
            return False

        # Перевіряємо доступний баланс для нової позиції
        if self.available_balance < self.min_trade_amount:
            logger.debug(f"[TechAnalysis] Недостатньо балансу: доступно ${self.available_balance:.2f}")
            return False

        return True

    def _load_history(self):
        """Завантаження історії з БД"""
        with get_db() as conn:
            bal = conn.execute(
                "SELECT amount FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                (self.strategy_id,)
            ).fetchone()
            if bal:
                self.balance = bal['amount']
            else:
                self.balance = 100.0
                self._save_balance()

            orders = conn.execute(
                "SELECT * FROM orders WHERE strategy_id=? AND status='open'",
                (self.strategy_id,)
            ).fetchall()

            for order in orders:
                o = dict(order)
                self.open_positions[o['symbol']] = {
                    'order_id': o['order_id'],
                    'entry_price': o['price'],
                    'quantity': o['quantity'],
                    'side': o['side'],
                    'opened_at': o.get('opened_at', datetime.now().isoformat())
                }
                self.locked_balance += o['quantity'] * o['price']

    def _save_balance(self):
        with get_db() as conn:
            conn.execute("DELETE FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                         (self.strategy_id,))
            conn.execute(
                "INSERT INTO balances (strategy_id, asset, amount, mode, updated_at) VALUES (?,?,?,?,?)",
                (self.strategy_id, 'USDT', self.balance, self.mode, datetime.now().isoformat())
            )

    def _save_order(self, order_id: str, symbol: str, side: str, price: float,
                    quantity: float, status: str, signal_type: str = None):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO orders 
                (order_id, strategy_id, symbol, side, price, quantity, status, 
                 order_type, opened_at, signal_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, self.strategy_id, symbol, side, price, quantity, status,
                  'Market', datetime.now().isoformat(), signal_type))

    async def _analysis_loop(self):
        while self.enabled:
            try:
                await self.analyze()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Помилка аналізу тех. аналізу: {e}")
            await asyncio.sleep(60)  # Кожну хвилину

    async def analyze(self) -> dict:
        if not self.enabled:
            return {'action': 'hold'}

        if not self.can_trade():
            return {'action': 'hold', 'blocked': True, 'reason': self._block_reason}

        signals_generated = []

        for symbol in self.symbols:
            try:
                # Пропускаємо символи, які вже у відкритих позиціях
                if symbol in self.open_positions:
                    # Перевіряємо чи пора закривати
                    await self._check_exit(symbol, self.current_prices.get(symbol, 0))
                    continue

                indicators = await self._get_indicators(symbol)
                if not indicators:
                    continue

                price = indicators['price']
                self.current_prices[symbol] = price

                # Перевіряємо чи можна відкривати нову позицію
                if not self.can_open_new_position(symbol):
                    continue

                if indicators['buy_signal'] and indicators['confidence'] >= self.min_confidence:
                    await self._open_position(symbol, price, indicators)
                    signals_generated.append(f"{symbol}: BUY")
                elif indicators['sell_signal'] and indicators['confidence'] >= self.min_confidence:
                    await self._open_position(symbol, price, indicators, is_long=False)
                    signals_generated.append(f"{symbol}: SELL")

            except Exception as e:
                logger.error(f"Помилка аналізу {symbol}: {e}")

        return {'action': 'hold', 'signals': signals_generated}

    async def _get_indicators(self, symbol: str) -> dict:
        """Отримання індикаторів для символу"""
        try:
            klines = await self.exchange.get_klines(symbol, self.timeframe, limit=100)
            if not klines or len(klines) < 50:
                return None

            closes = [k['close'] for k in klines]
            highs  = [k['high']  for k in klines]
            lows   = [k['low']   for k in klines]
            current_price = closes[-1]

            ema9  = self._calculate_ema(closes, 9)
            ema21 = self._calculate_ema(closes, 21)
            ema50 = self._calculate_ema(closes, 50)
            rsi   = self._calculate_rsi(closes)

            current_ema9  = ema9[-1]  if ema9  else current_price
            current_ema21 = ema21[-1] if ema21 else current_price
            current_ema50 = ema50[-1] if ema50 else current_price

            # --- Тренд вищого порядку (EMA21 vs EMA50) ---
            macro_trend_up   = current_ema21 > current_ema50
            macro_trend_down = current_ema21 < current_ema50

            # --- EMA crossover (EMA9 vs EMA21) ---
            ema_cross_up   = current_ema9 > current_ema21
            ema_cross_down = current_ema9 < current_ema21

            # --- Позиція ціни відносно EMA ---
            # Купівля має сенс коли ціна на відкаті (біля або нижче EMA21), не коли вже на піку
            price_below_ema21 = current_price < current_ema21 * 1.002
            price_above_ema21 = current_price > current_ema21 * 0.998

            # --- RSI зони ---
            rsi_oversold      = rsi < 35   # перепроданість — хороший момент для купівлі
            rsi_near_oversold = rsi < 45
            rsi_overbought    = rsi > 65   # перекупленість — хороший момент для шорту
            rsi_near_ob       = rsi > 55

            # --- Скор КУПІВЛІ ---
            # Купуємо: макротренд вгору + ціна на відкаті + RSI не перекуплений
            buy_score = 0
            if macro_trend_up:        buy_score += 2  # головна умова
            if ema_cross_up:          buy_score += 1  # підтвердження
            if price_below_ema21:     buy_score += 1  # купуємо на відкаті, не на піку
            if rsi_oversold:          buy_score += 2  # сильний сигнал
            elif rsi_near_oversold:   buy_score += 1
            if rsi_overbought:        buy_score -= 3  # не купуємо на піку RSI
            elif rsi_near_ob:         buy_score -= 1

            # --- Скор ПРОДАЖУ (SHORT) ---
            # Шортимо: макротренд вниз + ціна на відскоку + RSI не перепроданий
            sell_score = 0
            if macro_trend_down:      sell_score += 2
            if ema_cross_down:        sell_score += 1
            if price_above_ema21:     sell_score += 1  # продаємо на відскоку
            if rsi_overbought:        sell_score += 2
            elif rsi_near_ob:         sell_score += 1
            if rsi_oversold:          sell_score -= 3  # не шортимо на дні
            elif rsi_near_oversold:   sell_score -= 1

            # --- Розрахунок сигналу ---
            MIN_SCORE = 4
            MAX_SCORE = 6

            if buy_score >= MIN_SCORE and buy_score > sell_score:
                confidence  = min(95, 50 + int((buy_score / MAX_SCORE) * 45))
                buy_signal  = confidence >= self.min_confidence
                sell_signal = False
            elif sell_score >= MIN_SCORE and sell_score > buy_score:
                confidence  = min(95, 50 + int((sell_score / MAX_SCORE) * 45))
                buy_signal  = False
                sell_signal = confidence >= self.min_confidence
            else:
                confidence  = 50
                buy_signal  = False
                sell_signal = False

            trend = 'bullish' if macro_trend_up else ('bearish' if macro_trend_down else 'neutral')

            logger.debug(
                f"[TechAnalysis] {symbol} | price=${current_price:.2f} "
                f"ema9={current_ema9:.2f} ema21={current_ema21:.2f} ema50={current_ema50:.2f} "
                f"rsi={rsi:.1f} | buy_score={buy_score} sell_score={sell_score} "
                f"conf={confidence}% | buy={buy_signal} sell={sell_signal}"
            )

            return {
                'price':       current_price,
                'ema9':        current_ema9,
                'ema21':       current_ema21,
                'ema50':       current_ema50,
                'rsi':         round(rsi, 1),
                'confidence':  confidence,
                'buy_signal':  buy_signal,
                'sell_signal': sell_signal,
                'buy_score':   buy_score,
                'sell_score':  sell_score,
                'trend':       trend,
            }

        except Exception as e:
            logger.error(f"Помилка отримання індикаторів {symbol}: {e}")
            return None

    def _calculate_ema(self, prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []
        result = []
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        result.append(ema)
        for price in prices[period:]:
            ema = price * k + ema * (1 - k)
            result.append(ema)
        return result

    def _calculate_rsi(self, prices: List[float], period=14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(-diff)
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    async def _open_position(self, symbol: str, price: float, indicators: dict, is_long: bool = True):
        """Відкриття позиції (віртуальне або реальне)"""

        # Подвійна перевірка
        if not self.can_open_new_position(symbol):
            logger.info(f"[TechAnalysis] Пропускаємо відкриття {symbol}, ліміт позицій досягнуто")
            return

        # Розраховуємо розмір угоди (обмежуємо 25% від балансу)
        max_per_position = self.balance * 0.25
        trade_amount = min(
            self.balance * (self.trade_size_percent / 100),
            max_per_position
        )

        if trade_amount < self.min_trade_amount:
            trade_amount = self.min_trade_amount

        quantity = trade_amount / price

        # Перевірка мінімальної кількості
        min_qty = 0.0001 if 'BTC' in symbol else 0.001
        if quantity < min_qty:
            quantity = min_qty
            trade_amount = quantity * price

        if self.available_balance < trade_amount:
            logger.warning(f"[TechAnalysis] Недостатньо балансу: потрібно ${trade_amount:.2f}, доступно ${self.available_balance:.2f}")
            return

        side = 'buy' if is_long else 'sell'
        order_id = f"ta_{symbol}_{int(datetime.now().timestamp())}_{self.strategy_id}"

        if self.mode == 'real':
            result = await self.exchange.create_order(symbol, side, 'Market', quantity, price)
            if result.get('error'):
                logger.error(f"[TechAnalysis] Помилка відкриття позиції {symbol}: {result}")
                return
        else:
            logger.info(f"[TechAnalysis] ВІРТУАЛЬНА угода: {side} {quantity:.6f} {symbol} @ ${price:.2f}")

        self.open_positions[symbol] = {
            'order_id': order_id,
            'entry_price': price,
            'quantity': quantity,
            'side': side,
            'opened_at': datetime.now().isoformat(),
            'signal_type': 'LONG' if is_long else 'SHORT'
        }

        self.locked_balance += trade_amount
        self.last_trade_time[symbol] = datetime.now()  # Запам'ятовуємо час угоди
        self._save_order(order_id, symbol, side, price, quantity, 'open', 'LONG' if is_long else 'SHORT')
        await self._save_trade_chart_data(order_id, symbol, datetime.now())
        await self._save_candles_for_order(order_id, symbol, price, datetime.now())

        signal_text = "LONG (КУПІВЛЯ)" if is_long else "SHORT (ПРОДАЖ)"
        logger.info(f"[TechAnalysis] 📈 Відкрито позицію {symbol}: {signal_text} {quantity:.6f} @ ${price:.2f}")
        logger.info(f"[TechAnalysis] Стан: відкрито {len(self.open_positions)}/{self.max_concurrent_positions} позицій")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📈 *ВІДКРИТО ПОЗИЦІЮ* (TechAnalysis)\n"
                f"└ Пара: `{symbol}`\n"
                f"└ Тип: {signal_text}\n"
                f"└ Ціна: `${price:.2f}`\n"
                f"└ Кількість: {quantity:.6f}\n"
                f"└ Відкрито позицій: {len(self.open_positions)}/{self.max_concurrent_positions}\n"
                f"└ RSI: {indicators['rsi']:.1f}\n"
                f"└ Впевненість: {indicators['confidence']:.0f}%",
                parse_mode='Markdown'
            )

        self.increment_daily_trades()
        self.update_balance_for_drawdown()

    async def _check_exit(self, symbol: str, current_price: float):
        """Перевірка виходу з позиції"""
        position = self.open_positions.get(symbol)
        if not position:
            return

        side = position['side']
        entry_price = position['entry_price']

        if side == 'buy':
            pnl_percent = (current_price - entry_price) / entry_price * 100
            if pnl_percent >= self.take_profit_percent:
                await self._close_position(symbol, current_price, "take_profit")
            elif pnl_percent <= -self.stop_loss_percent:
                await self._close_position(symbol, current_price, "stop_loss")
        else:  # sell
            pnl_percent = (entry_price - current_price) / entry_price * 100
            if pnl_percent >= self.take_profit_percent:
                await self._close_position(symbol, current_price, "take_profit")
            elif pnl_percent <= -self.stop_loss_percent:
                await self._close_position(symbol, current_price, "stop_loss")

    async def _close_position(self, symbol: str, price: float, reason: str):
        """Закриття позиції"""
        position = self.open_positions.get(symbol)
        if not position:
            return

        entry_price = position['entry_price']
        quantity = position['quantity']
        side = position['side']
        order_id = position['order_id']

        # Розрахунок PnL
        if side == 'buy':
            gross_pnl = (price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - price) * quantity

        commission_rate = 0.001
        commission = (quantity * entry_price + quantity * price) * commission_rate
        pnl = gross_pnl - commission
        pnl_percent = (pnl / (quantity * entry_price)) * 100 if quantity * entry_price > 0 else 0

        # Видаляємо з відкритих позицій
        del self.open_positions[symbol]

        if self.mode == 'real':
            close_side = 'sell' if side == 'buy' else 'buy'
            result = await self.exchange.create_order(symbol, close_side, 'Market', quantity, price)
            if result.get('error'):
                logger.error(f"[TechAnalysis] Помилка закриття позиції {symbol}: {result}")
                return
        else:
            logger.info(f"[TechAnalysis] ВІРТУАЛЬНЕ закриття: {symbol} @ ${price:.2f}")

        # Оновлюємо баланс
        self.balance += (quantity * price) - commission
        self.locked_balance -= quantity * entry_price
        self.total_pnl += pnl
        self.total_trades += 1

        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        # Оновлюємо ордер в БД
        with get_db() as conn:
            conn.execute("""
                UPDATE orders SET status='closed', closed_at=?, closed_price=?, pnl=?, commission=?
                WHERE order_id=?
            """, (datetime.now().isoformat(), price, pnl, commission, order_id))

        reason_text = "🎯 Take Profit" if reason == "take_profit" else "🛑 Stop Loss"
        pnl_icon = "✅" if pnl >= 0 else "❌"

        logger.info(f"[TechAnalysis] 📉 Закрито позицію {symbol}: {reason_text}, PnL: {pnl_icon} ${pnl:.2f} ({pnl_percent:+.2f}%)")
        logger.info(f"[TechAnalysis] Стан після закриття: відкрито {len(self.open_positions)} позицій")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📉 *ЗАКРИТО ПОЗИЦІЮ* (TechAnalysis)\n"
                f"└ Пара: `{symbol}`\n"
                f"└ Ціна входу: `${entry_price:.2f}`\n"
                f"└ Ціна виходу: `${price:.2f}`\n"
                f"└ PnL: {pnl_icon} `${pnl:.2f}` ({pnl_percent:+.2f}%)\n"
                f"└ Причина: {reason_text}",
                parse_mode='Markdown'
            )

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
            'trade_size_percent': self.trade_size_percent,
            'take_profit_percent': self.take_profit_percent,
            'stop_loss_percent': self.stop_loss_percent,
            'min_confidence': self.min_confidence,
            'timeframe': self.timeframe,
            'daily_trades_count': self.daily_trades_count,
            'max_daily_trades': self.max_daily_trades,
            'is_blocked': self._is_blocked,
            'block_reason': self._block_reason,
            'max_concurrent_positions': self.max_concurrent_positions,  # ← додано
            'settings': {
                'trade_size_percent': self.trade_size_percent,
                'min_confidence': self.min_confidence,
                'stop_loss_percent': self.stop_loss_percent,
                'take_profit_percent': self.take_profit_percent
            }
        }

    async def reset(self):
        logger.warning(f"Скидання TechAnalysis стратегії")
        for symbol in list(self.open_positions.keys()):
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                try:
                    await self._close_position(symbol, price, "reset")
                except Exception as e:
                    logger.error(f"Помилка закриття позиції {symbol} при скиданні: {e}")
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.open_positions.clear()
        self.last_trade_time.clear()
        with get_db() as conn:
            conn.execute("DELETE FROM orders WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM balances WHERE strategy_id=?", (self.strategy_id,))
        self._save_balance()
        await self.reset_limits()
        add_log("INFO", self.name, "Стратегію скинуто")

    async def emergency_stop(self):
        for symbol in list(self.open_positions.keys()):
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                try:
                    await self._close_position(symbol, price, "emergency_stop")
                except Exception as e:
                    logger.error(f"Помилка закриття позиції {symbol} при екстреній зупинці: {e}")
        await self.stop()

    async def execute(self, signal: dict):
        pass

    async def update_settings(self, symbols=None, trade_size_percent=None,
                              take_profit_percent=None, stop_loss_percent=None,
                              min_confidence=None, timeframe=None):
        if symbols is not None:
            self.symbols = symbols
        if trade_size_percent is not None:
            self.trade_size_percent = max(10, min(90, trade_size_percent))
        if take_profit_percent is not None:
            self.take_profit_percent = take_profit_percent
        if stop_loss_percent is not None:
            self.stop_loss_percent = stop_loss_percent
        if min_confidence is not None:
            self.min_confidence = min_confidence
        if timeframe is not None:
            self.timeframe = timeframe

        save_strategy_settings('tech_analysis',
                               symbols=self.symbols,
                               trade_size_percent=self.trade_size_percent,
                               take_profit_percent=self.take_profit_percent,
                               stop_loss_percent=self.stop_loss_percent,
                               min_confidence=self.min_confidence,
                               timeframe=self.timeframe)
        logger.info(f"Оновлено налаштування TechAnalysis: trade_size={self.trade_size_percent}%")
        return True