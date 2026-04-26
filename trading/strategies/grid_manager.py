import logging
import uuid
import numpy as np
from datetime import datetime
from typing import Dict, List

from utils.logger_utils import setup_logger
from database.db import get_db, add_log

logger = setup_logger('grid_manager')


class GridInstance:
    def __init__(self, symbol: str, strategy_id: int, exchange, grid_levels: int = 10,
                 order_size_usdt: float = 50, lower_percent: float = 20, upper_percent: float = 20,
                 parent_strategy=None):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.exchange = exchange
        self.parent_strategy = parent_strategy
        self.grid_levels = grid_levels
        self.order_size_usdt = order_size_usdt
        self.lower_percent = lower_percent
        self.upper_percent = upper_percent

        self.lower_price = None
        self.upper_price = None
        self.grid_spacing = None

        self.active_buy_orders: Dict[str, dict] = {}
        self.active_sell_orders: Dict[str, dict] = {}
        self.closed_pairs: List[dict] = []

        self.total_pnl = 0.0
        self.total_commission = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.current_price = 0
        self.locked_balance = 0.0

        self.is_initialized = False
        self.last_rebalance_price = 0
        self.price_history = []

        self._load_history()

        for order in self.active_buy_orders.values():
            self.locked_balance += order['quantity'] * order['price']
        for order in self.active_sell_orders.values():
            self.locked_balance += order['quantity'] * order['price']

        if self.active_buy_orders or self.active_sell_orders:
            self._restore_grid_from_orders()

    @property
    def balance(self):
        return self.parent_strategy.total_balance if self.parent_strategy else 0

    @balance.setter
    def balance(self, value):
        if self.parent_strategy:
            self.parent_strategy.total_balance = value

    @property
    def available_balance(self):
        result = self.balance - self.locked_balance
        logger.debug(
            f"[{self.symbol}] available_balance: balance={self.balance}, locked={self.locked_balance}, result={result}")
        return result

    def _load_history(self):
        with get_db() as conn:
            orders = conn.execute(
                "SELECT * FROM orders WHERE strategy_id = ? AND symbol = ? AND status = 'open'",
                (self.strategy_id, self.symbol)
            ).fetchall()
            for order in orders:
                order_dict = dict(order)
                if order_dict['side'] == 'buy':
                    self.active_buy_orders[order_dict['order_id']] = order_dict
                else:
                    self.active_sell_orders[order_dict['order_id']] = order_dict
            logger.info(
                f"[{self.symbol}] Завантажено {len(self.active_buy_orders)} BUY, {len(self.active_sell_orders)} SELL")

    def _restore_grid_from_orders(self):
        try:
            all_prices = []
            for order in list(self.active_buy_orders.values()) + list(self.active_sell_orders.values()):
                all_prices.append(order['price'])
            if all_prices:
                min_price = min(all_prices)
                max_price = max(all_prices)
                self.lower_price = min_price
                self.upper_price = max_price
                self.grid_spacing = (self.upper_price - self.lower_price) / self.grid_levels
                self.is_initialized = True
                logger.info(f"[{self.symbol}] Відновлено сітку з {len(all_prices)} ордерів")
        except Exception as e:
            logger.error(f"[{self.symbol}] Помилка відновлення сітки: {e}")

    def _save_order(self, order_id: str, pair_id: str, side: str, price: float, quantity: float, status: str):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO orders 
                (order_id, pair_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, pair_id, self.strategy_id, self.symbol, side, price, quantity, status, 'Limit',
                  datetime.now().isoformat()))

    def _generate_pair_id(self) -> str:
        return f"pair_{self.symbol}_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}"

    async def _calculate_atr(self) -> float:
        try:
            klines = await self.exchange.get_klines(self.symbol, interval='15', limit=30)
            if not klines or len(klines) < 15:
                return 0

            true_ranges = []
            for i in range(1, len(klines)):
                high = klines[i]['high']
                low = klines[i]['low']
                prev_close = klines[i - 1]['close']

                tr1 = high - low
                tr2 = abs(high - prev_close)
                tr3 = abs(low - prev_close)
                true_range = max(tr1, tr2, tr3)
                true_ranges.append(true_range)

            if not true_ranges:
                return 0

            atr = sum(true_ranges[-14:]) / min(14, len(true_ranges))
            return atr

        except Exception as e:
            logger.error(f"[{self.symbol}] Помилка розрахунку ATR: {e}")
            return 0

    def _calculate_adaptive_range(self, current_price: float, atr: float) -> tuple:
        if atr <= 0:
            return self.lower_percent, self.upper_percent

        atr_percent = (atr / current_price) * 100
        adaptive_range = max(10, min(40, atr_percent * 2.5))

        step_correction = 1.0
        if self.grid_levels > 15:
            step_correction = 0.8
        elif self.grid_levels < 6:
            step_correction = 1.2

        adaptive_range = adaptive_range * step_correction
        adaptive_range = max(8, min(50, adaptive_range))

        logger.info(
            f"[{self.symbol}] Адаптивний діапазон: {adaptive_range:.1f}% (ATR: {atr:.2f}, ATR%: {atr_percent:.2f}%)")
        return adaptive_range, adaptive_range

    async def initialize_grid(self, current_price: float):
        if self.active_buy_orders or self.active_sell_orders:
            logger.info(f"[{self.symbol}] Вже є активні ордери, пропускаємо ініціалізацію")
            self.is_initialized = True
            return

        if self.available_balance < self.order_size_usdt:
            logger.warning(
                f"[{self.symbol}] Недостатньо балансу. Доступно: ${self.available_balance:.2f}, Потрібно: ${self.order_size_usdt:.2f}")
            return

        atr = await self._calculate_atr()
        adaptive_lower, adaptive_upper = self._calculate_adaptive_range(current_price, atr)

        self.current_price = current_price
        self.lower_price = current_price * (1 - adaptive_lower / 100)
        self.upper_price = current_price * (1 + adaptive_upper / 100)
        self.grid_spacing = (self.upper_price - self.lower_price) / self.grid_levels

        logger.info(f"[{self.symbol}] ========== АДАПТИВНА ІНІЦІАЛІЗАЦІЯ СІТКИ ==========")
        logger.info(f"[{self.symbol}] Поточна ціна: ${current_price:.2f}")
        logger.info(f"[{self.symbol}] Діапазон: ${self.lower_price:.2f} - ${self.upper_price:.2f}")
        logger.info(f"[{self.symbol}] Крок сітки: ${self.grid_spacing:.2f}")

        buy_levels_count = 0
        buy_prices = []

        for i in range(self.grid_levels):
            price = self.lower_price + i * self.grid_spacing
            if price < current_price:
                buy_levels_count += 1
                buy_prices.append(price)
            else:
                break

        logger.info(f"[{self.symbol}] Знайдено {buy_levels_count} рівнів нижче ціни:")
        for idx, price in enumerate(buy_prices):
            logger.info(f"[{self.symbol}]   L{idx}: ${price:.2f}")

        logger.info(f"[{self.symbol}] Доступний баланс: ${self.available_balance:.2f}")

        required_total = self.order_size_usdt * buy_levels_count
        logger.info(f"[{self.symbol}] Потрібно всього: ${required_total:.2f}")

        if self.available_balance < required_total:
            logger.warning(
                f"[{self.symbol}] Недостатньо балансу для всіх рівнів! Потрібно ${required_total:.2f}, є ${self.available_balance:.2f}")
            buy_levels_count = int(self.available_balance / self.order_size_usdt)
            buy_prices = buy_prices[:buy_levels_count]
            logger.info(f"[{self.symbol}] Буде створено {buy_levels_count} рівнів")

        orders_created = 0
        for idx, buy_price in enumerate(buy_prices):
            quantity = self.order_size_usdt / buy_price
            cost = quantity * buy_price

            pair_id = self._generate_pair_id()
            order_id = f"buy_{self.symbol}_{int(buy_price)}_{uuid.uuid4().hex[:8]}"
            self.active_buy_orders[order_id] = {
                'order_id': order_id,
                'pair_id': pair_id,
                'symbol': self.symbol,
                'side': 'buy',
                'price': buy_price,
                'quantity': quantity,
                'created_at': datetime.now().isoformat()
            }
            self.locked_balance += cost
            self._save_order(order_id, pair_id, 'buy', buy_price, quantity, 'open')
            orders_created += 1
            logger.info(f"[{self.symbol}] ✅ Створено BUY L{idx} @ ${buy_price:.2f}")

        self.is_initialized = True
        self.last_rebalance_price = current_price
        logger.info(f"[{self.symbol}] Створено {orders_created} BUY ордерів")

    async def cancel_order(self, order_id: str) -> bool:
        order = None
        side = None
        if order_id in self.active_buy_orders:
            order = self.active_buy_orders[order_id]
            side = 'buy'
            del self.active_buy_orders[order_id]
        elif order_id in self.active_sell_orders:
            order = self.active_sell_orders[order_id]
            side = 'sell'
            del self.active_sell_orders[order_id]
        else:
            return False

        if order:
            self.locked_balance -= order['quantity'] * order['price']

        with get_db() as conn:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ?", (order_id,))
        add_log("INFO", "grid", f"[{self.symbol}] Скасовано {side.upper()} ордер @ {order['price']:.2f}")
        return True

    async def update_price(self, price: float):
        self.price_history.append(price)
        if len(self.price_history) > 30:
            self.price_history.pop(0)

        old_price = self.current_price
        self.current_price = price

        if not self.is_initialized and not self.active_buy_orders and not self.active_sell_orders:
            await self.initialize_grid(price)
            return

        if self.lower_price is None or self.upper_price is None:
            await self.initialize_grid(price)
            return

        price_change_pct = abs(price - old_price) / old_price * 100 if old_price > 0 else 0

        if price_change_pct > 3:
            logger.info(f"[{self.symbol}] Значна зміна ціни: {price_change_pct:.2f}%")
            await self.adaptive_rebalance(price)
            return

        if price > self.upper_price or price < self.lower_price:
            logger.info(f"[{self.symbol}] Ціна вийшла за межі сітки")
            await self.adaptive_rebalance(price)
            return

        for oid, order in list(self.active_buy_orders.items()):
            if price <= order['price']:
                await self._on_buy_filled(oid, order, price)

        for oid, order in list(self.active_sell_orders.items()):
            if price >= order['price']:
                await self._on_sell_filled(oid, order, price)

    async def adaptive_rebalance(self, current_price: float):
        logger.info(f"[{self.symbol}] ========== АДАПТИВНЕ РЕБАЛАНСУВАННЯ ==========")

        atr = await self._calculate_atr()
        adaptive_lower, adaptive_upper = self._calculate_adaptive_range(current_price, atr)

        old_lower = self.lower_price
        old_upper = self.upper_price

        cancelled_count = len(self.active_buy_orders) + len(self.active_sell_orders)

        for oid in list(self.active_buy_orders.keys()):
            await self.cancel_order(oid)
        for oid in list(self.active_sell_orders.keys()):
            await self.cancel_order(oid)

        self.is_initialized = False
        self.lower_price = None
        self.upper_price = None

        await self.initialize_grid(current_price)

        if self.parent_strategy and hasattr(self.parent_strategy, 'telegram_bot') and self.parent_strategy.telegram_bot:
            message = (
                f"🔄 *ПЕРЕБУДОВА СІТКИ* 🔄\n"
                f"└ Символ: `{self.symbol}`\n"
                f"└ ATR: `${atr:.2f}`\n"
                f"└ Скасовано ордерів: {cancelled_count}"
            )
            await self.parent_strategy.telegram_bot.send_notification(message, parse_mode='Markdown')

    async def _on_buy_filled(self, order_id: str, buy_order: dict, current_price: float):
        """Обробка виконання BUY ордера - створення SELL ордера"""
        sell_price = buy_order['price'] + self.grid_spacing

        if sell_price > self.upper_price:
            self.locked_balance -= buy_order['quantity'] * buy_order['price']
            if self.parent_strategy:
                self.parent_strategy.increment_daily_trades()
                self.parent_strategy.update_balance_for_drawdown()
            del self.active_buy_orders[order_id]
            return

        sell_order_id = f"sell_{self.symbol}_{int(sell_price)}_{uuid.uuid4().hex[:8]}"
        cost = buy_order['quantity'] * buy_order['price']
        self.locked_balance -= cost
        self.balance -= cost

        # ВАЖЛИВО: використовуємо той самий pair_id
        pair_id = buy_order['pair_id']

        self.active_sell_orders[sell_order_id] = {
            'order_id': sell_order_id,
            'pair_id': pair_id,  # ОДНАКОВИЙ ДЛЯ BUY ТА SELL
            'symbol': self.symbol,
            'side': 'sell',
            'price': sell_price,
            'quantity': buy_order['quantity'],
            'buy_order_id': order_id,
            'created_at': datetime.now().isoformat()
        }
        self._save_order(sell_order_id, pair_id, 'sell', sell_price, buy_order['quantity'], 'open')
        del self.active_buy_orders[order_id]

        add_log("INFO", "grid",
                f"[{self.symbol}] BUY виконано @ {buy_order['price']:.2f}, створено SELL @ {sell_price:.2f} (pair_id={pair_id[:8]}...)")

        if self.parent_strategy and hasattr(self.parent_strategy, 'send_notification'):
            await self.parent_strategy.send_notification('grid', self.symbol, 'buy', buy_order['price'],
                                                         buy_order['quantity'])

        if current_price >= sell_price:
            await self._on_sell_filled(sell_order_id, self.active_sell_orders[sell_order_id], current_price)

    async def _on_sell_filled(self, order_id: str, sell_order: dict, current_price: float):
        """Обробка виконання SELL ордера - розрахунок PnL"""
        buy_order = None
        for bo in self.active_buy_orders.values():
            if bo.get('pair_id') == sell_order['pair_id']:
                buy_order = bo
                break

        if not buy_order:
            logger.warning(f"[{self.symbol}] Не знайдено BUY ордер для pair_id={sell_order['pair_id']}")
            self.locked_balance -= sell_order['quantity'] * sell_order['price']
            if self.parent_strategy:
                self.parent_strategy.increment_daily_trades()
                self.parent_strategy.update_balance_for_drawdown()
            del self.active_sell_orders[order_id]
            return

        buy_commission = buy_order['quantity'] * buy_order['price'] * 0.0018
        sell_commission = sell_order['quantity'] * sell_order['price'] * 0.0018
        total_commission = buy_commission + sell_commission

        revenue = sell_order['quantity'] * sell_order['price']
        cost = buy_order['quantity'] * buy_order['price']
        pnl = revenue - cost - total_commission

        self.balance += revenue - total_commission
        self.total_pnl += pnl
        self.total_trades += 1

        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        with get_db() as conn:
            conn.execute(
                "UPDATE orders SET status = 'closed', closed_at = ?, pnl = ?, commission = ? WHERE order_id = ?",
                (datetime.now().isoformat(), pnl, buy_commission, buy_order['order_id']))
            conn.execute(
                "UPDATE orders SET status = 'closed', closed_at = ?, pnl = ?, commission = ? WHERE order_id = ?",
                (datetime.now().isoformat(), pnl, sell_commission, sell_order['order_id']))

        del self.active_sell_orders[order_id]
        if buy_order['order_id'] in self.active_buy_orders:
            del self.active_buy_orders[buy_order['order_id']]

        self.locked_balance -= sell_order['quantity'] * sell_order['price']

        add_log("INFO", "grid",
                f"[{self.symbol}] SELL виконано @ {sell_order['price']:.2f}, PnL: ${pnl:.2f} (комісія: ${total_commission:.4f})")

        if self.parent_strategy and hasattr(self.parent_strategy, 'send_notification'):
            await self.parent_strategy.send_notification('grid', self.symbol, 'sell', sell_order['price'],
                                                         sell_order['quantity'], pnl)

        if self.parent_strategy:
            self.parent_strategy.increment_daily_trades()
            self.parent_strategy.update_balance_for_drawdown()

        new_buy_price = sell_order['price'] - self.grid_spacing
        if new_buy_price >= self.lower_price:
            new_quantity = self.order_size_usdt / new_buy_price
            new_cost = new_quantity * new_buy_price
            if self.available_balance >= new_cost:
                new_pair_id = self._generate_pair_id()
                new_order_id = f"buy_{self.symbol}_{int(new_buy_price)}_{uuid.uuid4().hex[:8]}"
                self.active_buy_orders[new_order_id] = {
                    'order_id': new_order_id,
                    'pair_id': new_pair_id,
                    'symbol': self.symbol,
                    'side': 'buy',
                    'price': new_buy_price,
                    'quantity': new_quantity,
                    'created_at': datetime.now().isoformat()
                }
                self.locked_balance += new_cost
                self._save_order(new_order_id, new_pair_id, 'buy', new_buy_price, new_quantity, 'open')
                add_log("INFO", "grid", f"[{self.symbol}] Новий BUY @ {new_buy_price:.2f}")

    async def update_settings(self, **kwargs):
        if 'grid_levels' in kwargs:
            self.grid_levels = kwargs['grid_levels']
        if 'order_size_usdt' in kwargs:
            self.order_size_usdt = kwargs['order_size_usdt']
        if 'lower_percent' in kwargs:
            self.lower_percent = kwargs['lower_percent']
        if 'upper_percent' in kwargs:
            self.upper_percent = kwargs['upper_percent']
        self.lower_price = None
        self.upper_price = None
        self.is_initialized = False

    def get_status(self) -> dict:
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        return {
            'symbol': self.symbol,
            'current_price': self.current_price,
            'lower_price': self.lower_price,
            'upper_price': self.upper_price,
            'grid_levels': self.grid_levels,
            'active_buys': len(self.active_buy_orders),
            'active_sells': len(self.active_sell_orders),
            'locked_balance': round(self.locked_balance, 2),
            'available_balance': round(self.available_balance, 2),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate': round(win_rate, 1)
        }

    def get_grid_levels(self, current_price: float) -> List[dict]:
        if not self.lower_price or not self.upper_price or self.grid_spacing is None:
            return []

        levels = []
        active_buy_found = False

        for i in range(self.grid_levels + 1):
            price = self.lower_price + i * self.grid_spacing

            if price < current_price:
                is_active = any(abs(order['price'] - price) < self.grid_spacing / 2
                                for order in self.active_buy_orders.values())

                if is_active and not active_buy_found:
                    level_type = 'active_buy'
                    active_buy_found = True
                elif is_active:
                    level_type = 'buy'
                else:
                    level_type = 'buy'

            elif price > current_price:
                is_active = any(abs(order['price'] - price) < self.grid_spacing / 2
                                for order in self.active_sell_orders.values())
                level_type = 'active_sell' if is_active else 'sell'
            else:
                level_type = 'current'

            levels.append({'level': i, 'price': round(price, 2), 'type': level_type})

        return levels