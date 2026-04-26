"""
Модуль для бектестингу стратегій
Підтримує тестування на історичних даних
"""

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from utils.logger_utils import setup_logger

logger = setup_logger('backtest')


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


@dataclass
class BacktestOrder:
    """Ордер в бектесті"""
    order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    timestamp: datetime
    status: OrderStatus = OrderStatus.OPEN
    closed_at: Optional[datetime] = None
    pnl: float = 0.0
    pair_id: Optional[str] = None


@dataclass
class BacktestResult:
    """Результат бектесту"""
    strategy_name: str
    start_date: datetime
    end_date: datetime
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    max_drawdown: float
    sharpe_ratio: float
    orders: List[BacktestOrder] = field(default_factory=list)

    def get_summary(self) -> dict:
        return {
            'strategy_name': self.strategy_name,
            'start_date': self.start_date.isoformat(),
            'end_date': self.end_date.isoformat(),
            'initial_balance': round(self.initial_balance, 2),
            'final_balance': round(self.final_balance, 2),
            'total_pnl': round(self.total_pnl, 2),
            'total_pnl_percent': round((self.total_pnl / self.initial_balance) * 100, 2),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': round(self.win_rate, 2),
            'max_drawdown': round(self.max_drawdown, 2),
            'sharpe_ratio': round(self.sharpe_ratio, 2)
        }


class GridBacktest:
    """
    Бектестер для Grid стратегії
    Тестує сіткову торгівлю на історичних даних
    """

    def __init__(self, exchange):
        self.exchange = exchange
        self.commission_rate = 0.0018  # 0.18%

    async def run_backtest(
            self,
            symbol: str,
            start_date: datetime,
            end_date: datetime,
            initial_balance: float = 100.0,
            grid_levels: int = 10,
            order_size_usdt: float = 50,
            lower_percent: float = 20,
            upper_percent: float = 20,
            interval: str = '15'
    ) -> BacktestResult:
        """
        Запуск бектесту Grid стратегії

        Параметри:
        - symbol: торгова пара (наприклад, 'BTCUSDT')
        - start_date: дата початку
        - end_date: дата завершення
        - initial_balance: початковий баланс
        - grid_levels: кількість рівнів сітки
        - order_size_usdt: розмір ордера в USDT
        - lower_percent: нижній діапазон %
        - upper_percent: верхній діапазон %
        - interval: таймфрейм свічок ('1', '5', '15', '60', 'D')
        """

        logger.info(f"Запуск бектесту {symbol} з {start_date.date()} по {end_date.date()}")

        # Отримуємо історичні дані
        klines = await self._get_historical_klines(symbol, start_date, end_date, interval)

        if len(klines) < 100:
            logger.error(f"Недостатньо даних для бектесту: {len(klines)} свічок")
            return BacktestResult(
                strategy_name='grid',
                start_date=start_date,
                end_date=end_date,
                initial_balance=initial_balance,
                final_balance=initial_balance,
                total_pnl=0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0,
                max_drawdown=0,
                sharpe_ratio=0
            )

        # Ініціалізація стану
        balance = initial_balance
        locked_balance = 0.0
        active_buy_orders: Dict[float, BacktestOrder] = {}
        active_sell_orders: Dict[float, BacktestOrder] = {}
        closed_orders: List[BacktestOrder] = []

        # Параметри сітки
        grid_spacing = None
        lower_price = None
        upper_price = None
        is_initialized = False

        # Для розрахунку drawdown
        max_balance = initial_balance
        max_drawdown = 0

        # Для розрахунку Sharpe ratio
        returns = []

        # Проходимо по кожній свічці
        for i, kline in enumerate(klines):
            current_price = kline['close']
            timestamp = datetime.fromtimestamp(kline['timestamp'] / 1000)

            # Ініціалізація сітки на першій свічці
            if not is_initialized:
                lower_price = current_price * (1 - lower_percent / 100)
                upper_price = current_price * (1 + upper_percent / 100)
                grid_spacing = (upper_price - lower_price) / grid_levels
                is_initialized = True

                # Створюємо початкові BUY ордери для всіх рівнів нижче поточної ціни
                buy_prices = []
                for level in range(grid_levels):
                    price = lower_price + level * grid_spacing
                    if price < current_price:
                        buy_prices.append(price)

                # Перевіряємо чи вистачає балансу
                required_balance = order_size_usdt * len(buy_prices)
                if balance >= required_balance:
                    for price in buy_prices:
                        quantity = order_size_usdt / price
                        order = BacktestOrder(
                            order_id=f"bt_buy_{timestamp.timestamp()}_{price}",
                            symbol=symbol,
                            side=OrderSide.BUY,
                            price=price,
                            quantity=quantity,
                            timestamp=timestamp
                        )
                        active_buy_orders[price] = order
                        locked_balance += order_size_usdt
                    balance -= required_balance
                else:
                    logger.warning(
                        f"Недостатньо балансу для ініціалізації: потрібно ${required_balance:.2f}, є ${balance:.2f}")

            # Перевіряємо чи ціна вийшла за межі сітки
            if current_price > upper_price or current_price < lower_price:
                # Перебудова сітки
                lower_price = current_price * (1 - lower_percent / 100)
                upper_price = current_price * (1 + upper_percent / 100)
                grid_spacing = (upper_price - lower_price) / grid_levels

                # Скасовуємо всі активні ордери
                for price, order in list(active_buy_orders.items()):
                    locked_balance -= order_size_usdt
                    balance += order_size_usdt
                    del active_buy_orders[price]

                for price, order in list(active_sell_orders.items()):
                    locked_balance -= order.quantity * order.price
                    balance += order.quantity * order.price
                    del active_sell_orders[price]

                # Створюємо нові BUY ордери
                buy_prices = []
                for level in range(grid_levels):
                    price = lower_price + level * grid_spacing
                    if price < current_price:
                        buy_prices.append(price)

                required_balance = order_size_usdt * len(buy_prices)
                if balance >= required_balance:
                    for price in buy_prices:
                        quantity = order_size_usdt / price
                        order = BacktestOrder(
                            order_id=f"bt_buy_{timestamp.timestamp()}_{price}",
                            symbol=symbol,
                            side=OrderSide.BUY,
                            price=price,
                            quantity=quantity,
                            timestamp=timestamp
                        )
                        active_buy_orders[price] = order
                        locked_balance += order_size_usdt
                    balance -= required_balance

            # Перевіряємо BUY ордери
            for price, order in list(active_buy_orders.items()):
                if current_price <= price:
                    # BUY виконано - створюємо SELL ордер
                    sell_price = price + grid_spacing
                    if sell_price <= upper_price:
                        sell_order = BacktestOrder(
                            order_id=f"bt_sell_{timestamp.timestamp()}_{sell_price}",
                            symbol=symbol,
                            side=OrderSide.SELL,
                            price=sell_price,
                            quantity=order.quantity,
                            timestamp=timestamp,
                            pair_id=order.order_id
                        )
                        active_sell_orders[sell_price] = sell_order

                        # Оновлюємо баланс
                        locked_balance -= order_size_usdt

                        # Видаляємо BUY ордер
                        del active_buy_orders[price]

            # Перевіряємо SELL ордери
            for price, order in list(active_sell_orders.items()):
                if current_price >= price:
                    # SELL виконано - розраховуємо PnL
                    buy_order = None
                    for bo in active_buy_orders.values():
                        if bo.order_id == order.pair_id:
                            buy_order = bo
                            break

                    if buy_order:
                        revenue = order.quantity * price
                        cost = buy_order.quantity * buy_order.price
                        commission = revenue * self.commission_rate + cost * self.commission_rate
                        pnl = revenue - cost - commission

                        # Оновлюємо баланс
                        balance += revenue - commission
                        locked_balance -= cost

                        # Зберігаємо закритий ордер
                        order.status = OrderStatus.CLOSED
                        order.closed_at = timestamp
                        order.pnl = pnl
                        closed_orders.append(order)

                        # Видаляємо SELL ордер
                        del active_sell_orders[price]

                        # Розраховуємо прибутковість для Sharpe ratio
                        returns.append(pnl / (balance + locked_balance) if (balance + locked_balance) > 0 else 0)

                        # Оновлюємо drawdown
                        total_balance = balance + locked_balance
                        if total_balance > max_balance:
                            max_balance = total_balance
                        drawdown = (max_balance - total_balance) / max_balance * 100
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown

            # Оновлюємо drawdown кожні 10 свічок
            if i % 10 == 0:
                total_balance = balance + locked_balance
                if total_balance > max_balance:
                    max_balance = total_balance
                drawdown = (max_balance - total_balance) / max_balance * 100
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

        # Закриваємо всі відкриті позиції за останньою ціною
        last_price = klines[-1]['close']
        for price, order in list(active_buy_orders.items()):
            locked_balance -= order_size_usdt
            balance += order_size_usdt
            order.status = OrderStatus.CANCELLED
            closed_orders.append(order)

        for price, order in list(active_sell_orders.items()):
            locked_balance -= order.quantity * order.price
            balance += order.quantity * order.price
            order.status = OrderStatus.CANCELLED
            closed_orders.append(order)

        # Розрахунок статистики
        final_balance = balance + locked_balance
        total_pnl = final_balance - initial_balance

        closed_trades = [o for o in closed_orders if o.status == OrderStatus.CLOSED]
        total_trades = len(closed_trades)
        winning_trades = len([o for o in closed_trades if o.pnl > 0])
        losing_trades = len([o for o in closed_trades if o.pnl <= 0])
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

        # Розрахунок Sharpe ratio
        if len(returns) > 1:
            avg_return = sum(returns) / len(returns)
            variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
            std_dev = variance ** 0.5
            sharpe_ratio = (avg_return / std_dev) * (252 ** 0.5) if std_dev > 0 else 0
        else:
            sharpe_ratio = 0

        result = BacktestResult(
            strategy_name='grid',
            start_date=start_date,
            end_date=end_date,
            initial_balance=initial_balance,
            final_balance=final_balance,
            total_pnl=total_pnl,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            orders=closed_orders
        )

        logger.info(f"Бектест завершено: PnL=${total_pnl:.2f}, Угод={total_trades}, WinRate={win_rate:.1f}%")

        return result

    async def _get_historical_klines(self, symbol: str, start_date: datetime, end_date: datetime, interval: str) -> \
    List[dict]:
        """Отримання історичних свічок для бектесту"""
        all_klines = []
        current_start = start_date

        interval_minutes = {
            '1': 1, '5': 5, '15': 15, '30': 30,
            '60': 60, '120': 120, '240': 240,
            'D': 1440, 'W': 10080
        }.get(interval, 15)

        while current_start < end_date:
            # Розраховуємо кінець запиту (максимум 1000 свічок за раз)
            diff_minutes = (end_date - current_start).total_seconds() / 60
            limit = min(1000, int(diff_minutes / interval_minutes) + 1)

            if limit < 10:
                break

            # Перетворюємо в мілісекунди для API
            start_ts = int(current_start.timestamp() * 1000)

            # Отримуємо свічки
            klines = await self.exchange.get_klines(symbol, interval, limit)

            if not klines:
                break

            # Фільтруємо за датою
            for k in klines:
                if start_date.timestamp() * 1000 <= k['timestamp'] <= end_date.timestamp() * 1000:
                    all_klines.append(k)

            # Оновлюємо поточну дату
            if klines:
                last_ts = klines[-1]['timestamp']
                current_start = datetime.fromtimestamp(last_ts / 1000) + timedelta(minutes=interval_minutes)
            else:
                break

            # Невелика затримка між запитами
            await asyncio.sleep(0.1)

        # Сортуємо за часом
        all_klines.sort(key=lambda x: x['timestamp'])

        return all_klines

    async def optimize_parameters(
            self,
            symbol: str,
            start_date: datetime,
            end_date: datetime,
            initial_balance: float = 100.0,
            grid_levels_range: List[int] = [5, 10, 15, 20],
            order_size_range: List[float] = [20, 50, 100],
            lower_percent_range: List[float] = [10, 15, 20, 25, 30],
            upper_percent_range: List[float] = [10, 15, 20, 25, 30]
    ) -> List[dict]:
        """
        Оптимізація параметрів Grid стратегії

        Повертає список результатів для різних комбінацій параметрів,
        відсортований за PnL (найкращі перші)
        """
        results = []
        total_combinations = len(grid_levels_range) * len(order_size_range) * len(lower_percent_range) * len(
            upper_percent_range)
        current = 0

        logger.info(f"Запуск оптимізації параметрів ({total_combinations} комбінацій)...")

        for grid_levels in grid_levels_range:
            for order_size in order_size_range:
                for lower_pct in lower_percent_range:
                    for upper_pct in upper_percent_range:
                        current += 1
                        logger.info(f"Тестування {current}/{total_combinations}: "
                                    f"рівні={grid_levels}, розмір=${order_size}, "
                                    f"діапазон={lower_pct}%/{upper_pct}%")

                        result = await self.run_backtest(
                            symbol=symbol,
                            start_date=start_date,
                            end_date=end_date,
                            initial_balance=initial_balance,
                            grid_levels=grid_levels,
                            order_size_usdt=order_size,
                            lower_percent=lower_pct,
                            upper_percent=upper_pct
                        )

                        results.append({
                            'grid_levels': grid_levels,
                            'order_size_usdt': order_size,
                            'lower_percent': lower_pct,
                            'upper_percent': upper_pct,
                            'total_pnl': result.total_pnl,
                            'total_pnl_percent': (result.total_pnl / initial_balance) * 100,
                            'total_trades': result.total_trades,
                            'win_rate': result.win_rate,
                            'max_drawdown': result.max_drawdown,
                            'sharpe_ratio': result.sharpe_ratio
                        })

        # Сортуємо за PnL
        results.sort(key=lambda x: x['total_pnl'], reverse=True)

        logger.info(f"Оптимізацію завершено. Найкращий результат: PnL=${results[0]['total_pnl']:.2f}")

        return results