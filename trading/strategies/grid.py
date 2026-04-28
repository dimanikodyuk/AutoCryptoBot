import logging
import asyncio
from datetime import datetime
from typing import Dict, List

from trading.strategies.base import BaseStrategy
from trading.strategies.grid_manager import GridInstance
from database.db import get_db, add_log
from config_manager import get_strategy_settings, save_strategy_settings
from utils.logger_utils import setup_logger

logger = setup_logger('grid')


class GridStrategy(BaseStrategy):
    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        super().__init__(strategy_id, name, mode, exchange)

        saved = get_strategy_settings('grid')
        self.default_grid_levels = saved.get('grid_levels', 10)
        self.default_order_size_usdt = saved.get('order_size_usdt', 50)
        self.default_lower_percent = saved.get('lower_percent', 20)
        self.default_upper_percent = saved.get('upper_percent', 20)
        self.symbols = saved.get('symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
        self.enabled = saved.get('enabled', False)

        self.total_balance = 100.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.telegram_bot = None

        self.grids: Dict[str, GridInstance] = {}
        self._init_grids()
        self._load_all_history()
        self._analysis_task = None
        logger.info(f"GridStrategy: {self.symbols}, баланс ${self.total_balance}")

    async def start(self):
        await super().start()
        self.enabled = True
        save_strategy_settings('grid', enabled=True)
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info("GridStrategy: цикл аналізу запущено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, True)

    async def stop(self):
        self.enabled = False
        if self._analysis_task:
            self._analysis_task.cancel()
        await super().stop()
        save_strategy_settings('grid', enabled=False)
        logger.info("GridStrategy: зупинено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, False)

    async def _analysis_loop(self):
        while self.enabled:
            try:
                await self.analyze()
            except Exception as e:
                logger.error(f"Помилка аналізу: {e}")
            await asyncio.sleep(5)

    def _init_grids(self):
        for s in self.symbols:
            self.grids[s] = GridInstance(
                symbol=s, strategy_id=self.strategy_id, exchange=self.exchange,
                grid_levels=self.default_grid_levels, order_size_usdt=self.default_order_size_usdt,
                lower_percent=self.default_lower_percent, upper_percent=self.default_upper_percent,
                parent_strategy=self
            )

    def _load_all_history(self):
        with get_db() as conn:
            bal = conn.execute("SELECT amount FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                               (self.strategy_id,)).fetchone()
            if bal:
                self.total_balance = bal['amount']
            else:
                self.total_balance = 100.0
                self._save_balance()
            stats = conn.execute(
                "SELECT SUM(pnl) as pnl, COUNT(*) as cnt FROM orders WHERE strategy_id=? AND status='closed'",
                (self.strategy_id,)).fetchone()
            if stats and stats['pnl']:
                self.total_pnl = stats['pnl']
                self.total_trades = stats['cnt']
            win = conn.execute("SELECT COUNT(*) FROM orders WHERE strategy_id=? AND status='closed' AND pnl>0",
                               (self.strategy_id,)).fetchone()
            self.winning_trades = win[0] if win else 0

    def _save_balance(self):
        with get_db() as conn:
            conn.execute("DELETE FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                         (self.strategy_id,))
            conn.execute("INSERT INTO balances (strategy_id, asset, amount, mode, updated_at) VALUES (?,?,?,?,?)",
                         (self.strategy_id, 'USDT', self.total_balance, self.mode, datetime.now().isoformat()))

    async def update_settings(self, symbols_list=None, grid_levels=None, order_size_usdt=None,
                              lower_percent=None, upper_percent=None):
        # Обробка зміни списку символів
        if symbols_list is not None:
            old_symbols = set(self.symbols)
            new_symbols = set(symbols_list)

            # Видаляємо сітки для символів, яких більше немає
            removed_symbols = old_symbols - new_symbols
            for sym in removed_symbols:
                if sym in self.grids:
                    logger.info(f"[Grid] Видаляємо сітку для {sym}")
                    grid = self.grids[sym]
                    # Скасовуємо всі активні ордери
                    for oid in list(grid.active_buy_orders.keys()):
                        await grid.cancel_order(oid)
                    for oid in list(grid.active_sell_orders.keys()):
                        await grid.cancel_order(oid)
                    # Видаляємо з БД всі відкриті ордери
                    with get_db() as conn:
                        conn.execute(
                            "DELETE FROM orders WHERE strategy_id=? AND symbol=? AND status='open'",
                            (self.strategy_id, sym)
                        )
                    # Видаляємо сітку з пам'яті
                    del self.grids[sym]

            # Додаємо нові сітки
            added_symbols = new_symbols - old_symbols
            for sym in added_symbols:
                logger.info(f"[Grid] Додаємо сітку для {sym}")
                self.grids[sym] = GridInstance(
                    symbol=sym, strategy_id=self.strategy_id, exchange=self.exchange,
                    grid_levels=self.default_grid_levels, order_size_usdt=self.default_order_size_usdt,
                    lower_percent=self.default_lower_percent, upper_percent=self.default_upper_percent,
                    parent_strategy=self
                )

            # Оновлюємо список символів
            self.symbols = symbols_list

        # Оновлюємо параметри для всіх існуючих сіток
        for sym, g in self.grids.items():
            if grid_levels is not None:
                g.grid_levels = grid_levels
            if order_size_usdt is not None:
                g.order_size_usdt = order_size_usdt
            if lower_percent is not None:
                g.lower_percent = lower_percent
            if upper_percent is not None:
                g.upper_percent = upper_percent
            await g.update_settings(
                grid_levels=grid_levels,
                order_size_usdt=order_size_usdt,
                lower_percent=lower_percent,
                upper_percent=upper_percent
            )

        # Оновлюємо значення за замовчуванням
        if grid_levels is not None:
            self.default_grid_levels = grid_levels
        if order_size_usdt is not None:
            self.default_order_size_usdt = order_size_usdt
        if lower_percent is not None:
            self.default_lower_percent = lower_percent
        if upper_percent is not None:
            self.default_upper_percent = upper_percent

        # Зберігаємо налаштування
        save_strategy_settings('grid',
                               symbols=self.symbols,
                               grid_levels=self.default_grid_levels,
                               order_size_usdt=self.default_order_size_usdt,
                               lower_percent=self.default_lower_percent,
                               upper_percent=self.default_upper_percent)

        logger.info(f"[Grid] Налаштування оновлено: символи={self.symbols}, "
                    f"рівні={self.default_grid_levels}, розмір=${self.default_order_size_usdt}")
        return True

    async def analyze(self):
        if not self.enabled:
            return {'action': 'hold'}

        # Перевірка лімітів
        if not self.can_trade():
            logger.warning(f"[Grid] Торгівля заблокована: {self._block_reason}"
            add_log("WARNING", self.name, f"Торгівля заблокована: {self._block_reason}")
            return {'action': 'hold', 'blocked': True, 'reason': self._block_reason}

        for sym, grid in self.grids.items():
            price = await self.exchange.get_current_price(sym)
            if price > 0:
                await grid.update_price(price)
                add_log("DEBUG", self.name, f"Оновлено ціну {sym}: ${price:.2f}")

        self.total_pnl = sum(g.total_pnl for g in self.grids.values())
        self.total_trades = sum(g.total_trades for g in self.grids.values())
        self.winning_trades = sum(g.winning_trades for g in self.grids.values())

        add_log("DEBUG", self.name, f"Аналіз завершено: PnL=${self.total_pnl:.2f}, Trades={self.total_trades}")
        return {'action': 'hold'}

    async def execute(self, signal: dict):
        pass

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if symbol in self.grids:
            return await self.grids[symbol].cancel_order(order_id)
        return False

    async def send_notification(self, strategy: str, symbol: str, side: str, price: float, quantity: float,
                                pnl: float = None):
        if self.telegram_bot:
            await self.telegram_bot.send_trade_notification(strategy, symbol, side, price, quantity, pnl)

    async def get_status(self):
        grids_status = {s: g.get_status() for s, g in self.grids.items()}
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades else 0
        return {
            'id': self.strategy_id, 'name': self.name, 'enabled': self.enabled, 'mode': self.mode,
            'symbols': self.symbols, 'balance': round(self.total_balance, 2),
            'total_pnl': round(self.total_pnl, 2), 'total_trades': self.total_trades,
            'winning_trades': self.winning_trades, 'win_rate': round(win_rate, 1),
            'grids': grids_status,
            'default_grid_levels': self.default_grid_levels,
            'default_order_size_usdt': self.default_order_size_usdt,
            'default_lower_percent': self.default_lower_percent,
            'default_upper_percent': self.default_upper_percent,
            # Ліміти
            'daily_trades_count': self.daily_trades_count,
            'max_daily_trades': self.max_daily_trades,
            'is_blocked': self._is_blocked,
            'block_reason': self._block_reason
        }

    def get_current_balance(self) -> float:
        return self.total_balance

    async def get_grid_levels_for_symbol(self, symbol: str):
        if symbol in self.grids:
            price = await self.exchange.get_current_price(symbol)
            g = self.grids[symbol]
            return {
                'symbol': symbol, 'current_price': price, 'lower_price': g.lower_price,
                'upper_price': g.upper_price, 'grid_levels': g.grid_levels,
                'levels': g.get_grid_levels(price),
                'active_buys': len(g.active_buy_orders), 'active_sells': len(g.active_sell_orders),
                'locked_balance': round(g.locked_balance, 2),
                'available_balance': round(g.available_balance, 2),
                'total_balance': round(self.total_balance, 2),
                'total_pnl': g.total_pnl, 'total_trades': g.total_trades,
                'win_rate': (g.winning_trades / g.total_trades * 100) if g.total_trades else 0
            }
        return {'error': 'Symbol not found'}

    async def send_grid_rebalance_notification(self, symbol: str, old_lower: float, old_upper: float,
                                               new_lower: float, new_upper: float, grid_spacing: float,
                                               atr: float, cancelled_orders: int):
        if self.telegram_bot:
            message = (
                f"🔄 *ПЕРЕБУДОВА СІТКИ* 🔄\n"
                f"└ Символ: `{symbol}`\n"
                f"└ Причина: адаптація до волатильності\n"
                f"└ Старий діапазон: `{old_lower:.2f} - {old_upper:.2f}`\n"
                f"└ Новий діапазон: `{new_lower:.2f} - {new_upper:.2f}`\n"
                f"└ ATR: `${atr:.2f}`\n"
                f"└ Крок сітки: `${grid_spacing:.2f}`\n"
                f"└ Скасовано ордерів: {cancelled_orders}"
            )
            await self.telegram_bot.send_notification(message, parse_mode='Markdown')

    async def reset(self):
        # Зупиняємо аналіз
        self.enabled = False
        if self._analysis_task:
            self._analysis_task.cancel()

        # Скасовуємо всі ордери для всіх сіток
        for sym, grid in self.grids.items():
            for oid in list(grid.active_buy_orders.keys()):
                await grid.cancel_order(oid)
            for oid in list(grid.active_sell_orders.keys()):
                await grid.cancel_order(oid)
            grid.total_pnl = 0
            grid.total_trades = 0
            grid.winning_trades = 0
            grid.losing_trades = 0
            grid.locked_balance = 0
            grid.lower_price = None
            grid.upper_price = None
            grid.grid_spacing = None
            grid.is_initialized = False
            grid.price_history = []
            grid.active_buy_orders.clear()
            grid.active_sell_orders.clear()
            grid.closed_pairs = []

        # Скидаємо баланс
        self.total_balance = 100.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0

        # Очищаємо БД
        with get_db() as conn:
            conn.execute("DELETE FROM orders WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM balances WHERE strategy_id=?", (self.strategy_id,))

        # Перестворюємо сітки з новими символами
        self._init_grids()
        self._save_balance()

        # Скидаємо ліміти
        await self.reset_limits()

        add_log("INFO", self.name, "Стратегію скинуто")
        logger.info(f"GridStrategy: повне скидання виконано. Символи: {self.symbols}")

    async def emergency_stop(self):
        await self.stop()