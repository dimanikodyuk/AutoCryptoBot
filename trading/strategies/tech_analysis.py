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

        # НОВІ ОБМЕЖЕННЯ
        self.max_concurrent_positions = 3  # Максимум 3 відкриті позиції одночасно
        self.min_trade_amount = 10  # Мінімальна сума угоди $10

        # Стан
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        # Поточні позиції
        self.open_positions: Dict[str, dict] = {}
        self.current_prices: Dict[str, float] = {}

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

    @property
    def available_balance(self):
        return self.balance - self.locked_balance

    def get_current_balance(self) -> float:
        return self.balance

    def can_open_new_position(self) -> bool:
        """Перевірка чи можна відкрити нову позицію"""
        # Перевіряємо кількість відкритих позицій
        if len(self.open_positions) >= self.max_concurrent_positions:
            logger.debug(f"[TechAnalysis] Максимум позицій ({self.max_concurrent_positions}) досягнуто")
            return False

        # Перевіряємо доступний баланс
        available = self.available_balance
        if available < self.min_trade_amount:
            logger.debug(f"[TechAnalysis] Недостатньо балансу: доступно ${available:.2f}, потрібно мінімум ${self.min_trade_amount:.2f}")
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
                indicators = await self._get_indicators(symbol)
                if not indicators:
                    continue

                price = indicators['price']
                self.current_prices[symbol] = price

                if symbol in self.open_positions:
                    await self._check_exit(symbol, price)
                else:
                    # Перевіряємо чи можна відкривати нову позицію
                    if not self.can_open_new_position():
                        logger.debug(f"[TechAnalysis] Пропускаємо {symbol}, досягнуто ліміт позицій ({len(self.open_positions)}/{self.max_concurrent_positions})")
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
            current_price = closes[-1]

            ema9 = self._calculate_ema(closes, 9)
            ema21 = self._calculate_ema(closes, 21)
            rsi = self._calculate_rsi(closes)

            current_ema9 = ema9[-1] if ema9 else current_price
            current_ema21 = ema21[-1] if ema21 else current_price

            ema_trend_up = current_ema9 > current_ema21
            rsi_oversold = rsi < 30
            rsi_overbought = rsi > 70

            confidence = 0
            buy_signals = 0
            sell_signals = 0

            if ema_trend_up:
                buy_signals += 1
            else:
                sell_signals += 1

            if rsi_oversold:
                buy_signals += 1
            elif rsi_overbought:
                sell_signals += 1

            if buy_signals > sell_signals:
                confidence = min(95, 50 + (buy_signals - sell_signals) * 25)
                buy_signal = confidence >= self.min_confidence
                sell_signal = False
            elif sell_signals > buy_signals:
                confidence = min(95, 50 + (sell_signals - buy_signals) * 25)
                buy_signal = False
                sell_signal = confidence >= self.min_confidence
            else:
                confidence = 50
                buy_signal = False
                sell_signal = False

            return {
                'price': current_price,
                'ema9': current_ema9,
                'ema21': current_ema21,
                'rsi': round(rsi, 1),
                'confidence': confidence,
                'buy_signal': buy_signal,
                'sell_signal': sell_signal,
                'trend': 'bullish' if ema_trend_up else 'bearish'
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

        # Перевіряємо ще раз чи можна відкривати (для безпеки)
        if not self.can_open_new_position():
            logger.info(f"[TechAnalysis] Пропускаємо відкриття {symbol}, ліміт позицій досягнуто")
            return

        # Розраховуємо розмір угоди (обмежуємо 25% від балансу на одну позицію)
        max_per_position = self.balance * 0.25  # Максимум 25% від загального балансу
        trade_amount = min(
            self.balance * (self.trade_size_percent / 100),
            max_per_position
        )

        # Мінімальна сума угоди
        if trade_amount < self.min_trade_amount:
            trade_amount = self.min_trade_amount
            logger.info(f"[TechAnalysis] Коригуємо суму до мінімальної: ${trade_amount:.2f}")

        quantity = trade_amount / price

        # Перевірка мінімальної кількості
        min_qty = 0.0001 if 'BTC' in symbol else 0.001
        if quantity < min_qty:
            quantity = min_qty
            trade_amount = quantity * price
            logger.info(f"[TechAnalysis] Коригуємо кількість до мінімальної: {quantity}")

        # Фінальна перевірка доступного балансу
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
            logger.info(f"[TechAnalysis] ВІРТУАЛЬНА угода: {side} {quantity:.6f} {symbol} @ ${price:.2f} (сума: ${trade_amount:.2f})")

        self.open_positions[symbol] = {
            'order_id': order_id,
            'entry_price': price,
            'quantity': quantity,
            'side': side,
            'opened_at': datetime.now().isoformat(),
            'signal_type': 'LONG' if is_long else 'SHORT'
        }

        self.locked_balance += trade_amount
        self._save_order(order_id, symbol, side, price, quantity, 'open', 'LONG' if is_long else 'SHORT')

        signal_text = "LONG (КУПІВЛЯ)" if is_long else "SHORT (ПРОДАЖ)"
        logger.info(f"[TechAnalysis] 📈 Відкрито позицію {symbol}: {signal_text} {quantity:.6f} @ ${price:.2f} (сума: ${trade_amount:.2f})")
        logger.info(f"[TechAnalysis] Стан: відкрито {len(self.open_positions)}/{self.max_concurrent_positions} позицій, заблоковано ${self.locked_balance:.2f}, доступно ${self.available_balance:.2f}")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📈 *ВІДКРИТО ПОЗИЦІЮ* (TechAnalysis)\n"
                f"└ Пара: `{symbol}`\n"
                f"└ Тип: {signal_text}\n"
                f"└ Ціна: `${price:.2f}`\n"
                f"└ Кількість: {quantity:.6f}\n"
                f"└ Сума: `${trade_amount:.2f}`\n"
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
        logger.info(f"[TechAnalysis] Стан після закриття: відкрито {len(self.open_positions)} позицій, доступно ${self.available_balance:.2f}")

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
            'max_concurrent_positions': self.max_concurrent_positions,
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
        logger.info(f"Оновлено налаштування TechAnalysis: trade_size={self.trade_size_percent}%, TP={self.take_profit_percent}%, SL={self.stop_loss_percent}%")
        return True