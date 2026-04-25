import logging
import uuid
from datetime import datetime
from typing import Dict, List
from utils.logger_utils import setup_logger
from database.db import get_db, add_log

logger = setup_logger('grid_manager')


class GridInstance:
    """Окремий екземпляр Grid стратегії для однієї пари"""

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
        return self.balance - self.locked_balance

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
            logger.info(f"[{self.symbol}] Завантажено {len(self.active_buy_orders)} BUY, {len(self.active_sell_orders)} SELL")

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

    async def initialize_grid(self, current_price: float):
        if self.active_buy_orders or self.active_sell_orders:
            logger.info(f"[{self.symbol}] Вже є активні ордери, пропускаємо ініціалізацію")
            self.is_initialized = True
            return

        self.current_price = current_price
        self.lower_price = current_price * (1 - self.lower_percent / 100)
        self.upper_price = current_price * (1 + self.upper_percent / 100)
        self.grid_spacing = (self.upper_price - self.lower_price) / self.grid_levels

        logger.info(f"[{self.symbol}] Ініціалізація сітки: діапазон {self.lower_price:.2f} - {self.upper_price:.2f}")

        current_level = int((current_price - self.lower_price) / self.grid_spacing)
        current_level = max(0, min(current_level, self.grid_levels))
        buy_levels_count = current_level

        orders_created = 0
        for i in range(buy_levels_count):
            buy_price = self.lower_price + i * self.grid_spacing
            quantity = self.order_size_usdt / buy_price
            cost = quantity * buy_price

            if self.available_balance >= cost:
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
                add_log("INFO", "grid", f"[{self.symbol}] Створено BUY @ {buy_price:.2f} (заблоковано ${cost:.2f})")
            else:
                logger.warning(f"[{self.symbol}] Недостатньо доступного балансу (потрібно ${cost:.2f}, є ${self.available_balance:.2f})")
                break

        self.is_initialized = True
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

        if side == 'buy':
            self.locked_balance -= order['quantity'] * order['price']
        else:
            self.locked_balance -= order['quantity'] * order['price']

        with get_db() as conn:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ?", (order_id,))
        add_log("INFO", "grid", f"[{self.symbol}] Скасовано {side.upper()} ордер @ {order['price']:.2f}")
        return True

    async def update_price(self, price: float):
        self.current_price = price
        if not self.is_initialized and not self.active_buy_orders and not self.active_sell_orders:
            await self.initialize_grid(price)
            return

        if not self.is_initialized and (self.active_buy_orders or self.active_sell_orders):
            self.is_initialized = True

        if self.lower_price is None:
            return

        if price > self.upper_price or price < self.lower_price:
            logger.info(f"[{self.symbol}] Ціна {price} вийшла за межі, ребаланс...")
            await self.rebalance(price)
            return

        for oid, order in list(self.active_buy_orders.items()):
            if price <= order['price']:
                await self._on_buy_filled(oid, order, price)

        for oid, order in list(self.active_sell_orders.items()):
            if price >= order['price']:
                await self._on_sell_filled(oid, order, price)

    async def _on_buy_filled(self, order_id: str, buy_order: dict, current_price: float):
        sell_price = buy_order['price'] + self.grid_spacing
        if sell_price > self.upper_price:
            self.locked_balance -= buy_order['quantity'] * buy_order['price']
            del self.active_buy_orders[order_id]
            return

        sell_order_id = f"sell_{self.symbol}_{int(sell_price)}_{uuid.uuid4().hex[:8]}"
        cost = buy_order['quantity'] * buy_order['price']
        self.locked_balance -= cost
        self.balance -= cost

        self.active_sell_orders[sell_order_id] = {
            'order_id': sell_order_id,
            'pair_id': buy_order['pair_id'],
            'symbol': self.symbol,
            'side': 'sell',
            'price': sell_price,
            'quantity': buy_order['quantity'],
            'buy_order_id': order_id,
            'created_at': datetime.now().isoformat()
        }
        self._save_order(sell_order_id, buy_order['pair_id'], 'sell', sell_price, buy_order['quantity'], 'open')
        del self.active_buy_orders[order_id]
        add_log("INFO", "grid", f"[{self.symbol}] BUY виконано @ {buy_order['price']:.2f}, створено SELL @ {sell_price:.2f}")

        # Сповіщення
        if self.parent_strategy and hasattr(self.parent_strategy, 'send_notification'):
            await self.parent_strategy.send_notification('grid', self.symbol, 'buy', buy_order['price'], buy_order['quantity'])

        if current_price >= sell_price:
            await self._on_sell_filled(sell_order_id, self.active_sell_orders[sell_order_id], current_price)

    async def _on_sell_filled(self, order_id: str, sell_order: dict, current_price: float):
        buy_order = None
        for bo in self.active_buy_orders.values():
            if bo.get('pair_id') == sell_order['pair_id']:
                buy_order = bo
                break
        if not buy_order:
            self.locked_balance -= sell_order['quantity'] * sell_order['price']
            del self.active_sell_orders[order_id]
            return

        buy_commission = sell_order['quantity'] * sell_order['price'] * 0.0018
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
            conn.execute("UPDATE orders SET status = 'closed', closed_at = ?, pnl = ?, commission = ? WHERE order_id = ?",
                         (datetime.now().isoformat(), pnl, buy_commission, buy_order['order_id']))
            conn.execute("UPDATE orders SET status = 'closed', closed_at = ?, pnl = ?, commission = ? WHERE order_id = ?",
                         (datetime.now().isoformat(), pnl, sell_commission, sell_order['order_id']))

        del self.active_sell_orders[order_id]
        if buy_order['order_id'] in self.active_buy_orders:
            del self.active_buy_orders[buy_order['order_id']]
        self.locked_balance -= sell_order['quantity'] * sell_order['price']
        add_log("INFO", "grid", f"[{self.symbol}] SELL виконано @ {sell_order['price']:.2f}, PnL: ${pnl:.2f}")

        # Сповіщення
        if self.parent_strategy and hasattr(self.parent_strategy, 'send_notification'):
            await self.parent_strategy.send_notification('grid', self.symbol, 'sell', sell_order['price'], sell_order['quantity'], pnl)

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

    async def rebalance(self, current_price: float):
        for oid in list(self.active_buy_orders.keys()):
            await self.cancel_order(oid)
        for oid in list(self.active_sell_orders.keys()):
            await self.cancel_order(oid)
        self.is_initialized = False
        await self.initialize_grid(current_price)

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
        if not self.lower_price or not self.upper_price:
            return []
        levels = []
        for i in range(self.grid_levels + 1):
            price = self.lower_price + i * self.grid_spacing
            base_type = 'neutral'
            if price < current_price:
                base_type = 'buy'
            elif price > current_price:
                base_type = 'sell'
            is_active_buy = any(abs(order['price'] - price) < self.grid_spacing / 2 for order in self.active_buy_orders.values())
            is_active_sell = any(abs(order['price'] - price) < self.grid_spacing / 2 for order in self.active_sell_orders.values())
            if is_active_buy:
                level_type = 'active_buy'
            elif is_active_sell:
                level_type = 'active_sell'
            else:
                level_type = base_type
            levels.append({'level': i, 'price': round(price, 2), 'type': level_type})
        return levels