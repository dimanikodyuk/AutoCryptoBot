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
    Індикатори: EMA9, EMA21, RSI14, Volume
    """

    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        super().__init__(strategy_id, name, mode, exchange)

        # Завантажуємо налаштування з файлу
        saved_settings = get_strategy_settings('scalp')

        self.symbols = saved_settings.get('symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
        self.enabled = saved_settings.get('enabled', False)

        # Параметри торгівлі
        self.trade_size_usdt = saved_settings.get('trade_size_usdt', 20)
        self.take_profit_percent = saved_settings.get('take_profit_percent', 0.5)
        self.stop_loss_percent = saved_settings.get('stop_loss_percent', 0.25)
        self.trailing_stop_percent = saved_settings.get('trailing_stop_percent', 0.3)

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

        # Кеш для індикаторів
        self.ema9_cache: Dict[str, List[float]] = {}
        self.ema21_cache: Dict[str, List[float]] = {}
        self.rsi_cache: Dict[str, List[float]] = {}
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
            # Не використовуємо await для задачі з іншого циклу
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

    async def get_indicators(self, symbol: str) -> dict:
        try:
            klines = await self.exchange.get_klines(symbol, interval='1', limit=50)
            if not klines or len(klines) < 30:
                return None

            closes = [k['close'] for k in klines]
            volumes = [k['volume'] for k in klines]

            ema9 = await self.calculate_ema(closes, 9)
            ema21 = await self.calculate_ema(closes, 21)
            rsi = await self.calculate_rsi(closes, 14)

            current_price = closes[-1]
            self.current_prices[symbol] = current_price

            current_ema9 = ema9[-1] if ema9 else current_price
            current_ema21 = ema21[-1] if ema21 else current_price
            current_rsi = rsi

            avg_volume = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else volumes[-1]
            volume_surge = volumes[-1] > avg_volume * 1.5 if len(volumes) >= 10 else False

            buy_signal = (
                    current_ema9 > current_ema21 and
                    current_rsi < 70 and current_rsi > 30 and
                    volume_surge
            )

            sell_signal = (
                    current_ema9 < current_ema21 and
                    current_rsi < 70 and current_rsi > 30 and
                    volume_surge
            )

            return {
                'price': current_price,
                'ema9': current_ema9,
                'ema21': current_ema21,
                'rsi': current_rsi,
                'volume_surge': volume_surge,
                'buy_signal': buy_signal,
                'sell_signal': sell_signal
            }
        except Exception as e:
            logger.error(f"Помилка отримання індикаторів {symbol}: {e}")
            return None

    async def check_exit_signals(self, symbol: str, position: dict, current_price: float) -> str:
        if current_price > position['highest_price']:
            position['highest_price'] = current_price
        if current_price < position['lowest_price']:
            position['lowest_price'] = current_price

        pnl_percent = (current_price - position['entry_price']) / position['entry_price'] * 100

        trailing_stop_price = position['highest_price'] * (1 - self.trailing_stop_percent / 100)
        if trailing_stop_price > position['entry_price'] and current_price <= trailing_stop_price:
            return 'trailing_stop'

        if pnl_percent >= self.take_profit_percent:
            return 'take_profit'

        if pnl_percent <= -self.stop_loss_percent:
            return 'stop_loss'

        return 'hold'

    async def analyze(self) -> dict:
        if not self.enabled:
            return {'action': 'hold'}

        results = {}

        for symbol in self.symbols:
            try:
                indicators = await self.get_indicators(symbol)
                if not indicators:
                    continue

                price = indicators['price']

                if symbol in self.open_positions:
                    exit_signal = await self.check_exit_signals(symbol, self.open_positions[symbol], price)
                    if exit_signal != 'hold':
                        await self._close_position(symbol, exit_signal, price)
                else:
                    if indicators['buy_signal'] and self.available_balance >= self.trade_size_usdt:
                        await self._open_position(symbol, price)

            except Exception as e:
                logger.error(f"Помилка аналізу {symbol}: {e}")

        return {'action': 'hold', 'results': results}

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

    async def _open_position(self, symbol: str, price: float):
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
            'lowest_price': price
        }
        self.locked_balance += cost
        self._save_order(order_id, symbol, 'buy', price, quantity, 'open')

        add_log("INFO", self.name, f"📈 Відкрито LONG позицію {symbol} @ ${price:.2f}")

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
        del self.open_positions[symbol]

        add_log("INFO", self.name,
                f"📉 Закрито LONG позицію {symbol} @ ${price:.2f} | PnL: ${pnl:.2f} | Причина: {reason}")

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
            'trailing_stop_percent': self.trailing_stop_percent
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
        add_log("INFO", self.name, "Стратегію скинуто")

    async def emergency_stop(self):
        logger.warning(f"Екстрена зупинка Scalp стратегії")
        for symbol in list(self.open_positions.keys()):
            price = self.current_prices.get(symbol, 0)
            if price > 0:
                await self._close_position(symbol, 'emergency_stop', price)
        await self.stop()