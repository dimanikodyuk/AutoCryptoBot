import logging
import time
from typing import Dict, List
from config import Config
from database.db import get_db, add_log
from trading.exchange import BybitExchange
from trading.strategies.grid import GridStrategy
from trading.strategies.news import NewsStrategy
from trading.strategies.scalp import ScalpStrategy
from trading.strategies.signals import SignalStrategy
from utils.logger_utils import setup_logger

logger = setup_logger('engine')


class TradingEngine:
    """Двигун для управління стратегіями"""

    def __init__(self, config: Config):
        self.config = config
        self.exchange = None
        self.strategies: Dict[int, object] = {}
        self.active_strategies: List[object] = []
        self.start_time = None

    async def init(self):
        """Ініціалізація"""
        logger.info("Ініціалізація торгового двигуна...")
        self.start_time = time.time()
        self.exchange = BybitExchange(self.config, mode=self.config.DEFAULT_MODE)

        # Завантаження стратегій з БД
        with get_db() as conn:
            strategies = conn.execute(
                "SELECT id, name, enabled, mode, drawdown_limit FROM strategies"
            ).fetchall()

            for s in strategies:
                if s['name'] == 'grid':
                    strategy = GridStrategy(
                        strategy_id=s['id'],
                        name=s['name'],
                        mode=self.config.DEFAULT_MODE,
                        exchange=self.exchange
                    )
                    # Встановлюємо ліміти
                    strategy.max_daily_drawdown = self.config.MAX_DAILY_DRAWDOWN
                    strategy.max_daily_trades = self.config.MAX_DAILY_TRADES
                    strategy.min_balance_for_trading = self.config.MIN_BALANCE_FOR_TRADING

                    strategy.enabled = bool(s['enabled'])
                    self.strategies[s['id']] = strategy
                    logger.info(f"Завантажено Grid стратегію (id={s['id']}, enabled={strategy.enabled})")

                elif s['name'] == 'news':
                    strategy = NewsStrategy(
                        strategy_id=s['id'],
                        name=s['name'],
                        mode=self.config.DEFAULT_MODE,
                        exchange=self.exchange
                    )
                    # Встановлюємо ліміти
                    strategy.max_daily_drawdown = self.config.MAX_DAILY_DRAWDOWN
                    strategy.max_daily_trades = self.config.MAX_DAILY_TRADES
                    strategy.min_balance_for_trading = self.config.MIN_BALANCE_FOR_TRADING

                    strategy.enabled = bool(s['enabled'])
                    self.strategies[s['id']] = strategy
                    logger.info(f"Завантажено News стратегію (id={s['id']}, enabled={strategy.enabled})")

                elif s['name'] == 'scalp':
                    strategy = ScalpStrategy(
                        strategy_id=s['id'],
                        name=s['name'],
                        mode=self.config.DEFAULT_MODE,
                        exchange=self.exchange
                    )
                    # Встановлюємо ліміти
                    strategy.max_daily_drawdown = self.config.MAX_DAILY_DRAWDOWN
                    strategy.max_daily_trades = self.config.MAX_DAILY_TRADES
                    strategy.min_balance_for_trading = self.config.MIN_BALANCE_FOR_TRADING

                    strategy.enabled = bool(s['enabled'])
                    self.strategies[s['id']] = strategy
                    logger.info(f"Завантажено Scalp стратегію (id={s['id']}, enabled={strategy.enabled})")

                elif s['name'] == 'signals':
                    strategy = SignalStrategy(
                        strategy_id=s['id'],
                        name=s['name'],
                        mode=self.config.DEFAULT_MODE,
                        exchange=self.exchange
                    )
                    strategy.max_daily_drawdown = self.config.MAX_DAILY_DRAWDOWN
                    strategy.max_daily_trades = self.config.MAX_DAILY_TRADES
                    strategy.min_balance_for_trading = self.config.MIN_BALANCE_FOR_TRADING
                    strategy.enabled = bool(s['enabled'])
                    self.strategies[s['id']] = strategy
                    logger.info(f"Завантажено Signals стратегію (id={s['id']}, enabled={strategy.enabled})")

        # Запуск WebSocket
        await self.exchange.start_websocket(self.config.SYMBOLS)

        logger.info(f"Торговий двигун ініціалізовано. Знайдено {len(self.strategies)} стратегій")

    async def start_all_strategies(self):
        """Запуск всіх стратегій (активних та неактивних)"""
        for strategy in self.strategies.values():
            # Запускаємо ВСІ стратегії, незалежно від enabled
            await strategy.start()
            self.active_strategies.append(strategy)
            strategy.enabled = True  # Встановлюємо enabled в True
            # Оновлюємо в БД
            with get_db() as conn:
                conn.execute("UPDATE strategies SET enabled = 1 WHERE id = ?", (strategy.strategy_id,))
            logger.info(f"Стратегія {strategy.name} запущена")

    def set_telegram_bot(self, telegram_bot):
        self.telegram_bot = telegram_bot
        for strategy in self.strategies.values():
            strategy.telegram_bot = telegram_bot  # ← Це має бути

    async def stop_strategy(self, strategy_id: int):
        """Зупинка конкретної стратегії"""
        if strategy_id in self.strategies:
            await self.strategies[strategy_id].stop()
            if self.strategies[strategy_id] in self.active_strategies:
                self.active_strategies.remove(self.strategies[strategy_id])
            with get_db() as conn:
                conn.execute("UPDATE strategies SET enabled = 0 WHERE id = ?", (strategy_id,))
            add_log("INFO", self.strategies[strategy_id].name, "Стратегію зупинено")

    async def start_strategy(self, strategy_id: int):
        """Запуск конкретної стратегії"""
        if strategy_id in self.strategies:
            await self.strategies[strategy_id].start()
            if self.strategies[strategy_id] not in self.active_strategies:
                self.active_strategies.append(self.strategies[strategy_id])
            with get_db() as conn:
                conn.execute("UPDATE strategies SET enabled = 1 WHERE id = ?", (strategy_id,))
            add_log("INFO", self.strategies[strategy_id].name, "Стратегію запущено")

    async def emergency_stop_all(self):
        """Екстрена зупинка всіх стратегій"""
        logger.warning("ЕКСТРЕНА ЗУПИНКА ВСІХ СТРАТЕГІЙ")
        for strategy in self.active_strategies:
            await strategy.emergency_stop()
        self.active_strategies.clear()
        with get_db() as conn:
            conn.execute("UPDATE strategies SET enabled = 0")
        add_log("WARNING", "system", "Всі стратегії екстрено зупинено")

    async def get_summary(self) -> dict:
        """Отримання звіту"""
        total_pnl = 0
        total_balance = 0
        strategies_status = []

        for strategy in self.strategies.values():
            status = await strategy.get_status()
            strategies_status.append(status)
            total_pnl += status.get('total_pnl', 0)
            total_balance += status.get('total_balance', status.get('balance', 0))

        return {
            'total_pnl': round(total_pnl, 2),
            'total_balance': round(total_balance, 2),
            'active_strategies': len(self.active_strategies),
            'strategies': strategies_status
        }

    async def shutdown(self):
        """Завершення роботи"""
        logger.info("Завершення роботи торгового двигуна...")
        await self.emergency_stop_all()
        await self.exchange.stop_websocket()  # Закриваємо WebSocket з'єднання

    async def get_orders(self, side: str = 'all', status: str = 'all', limit: int = 50, strategy_name: str = None) -> \
    List[dict]:
        """Отримання ордерів з фільтрами"""
        with get_db() as conn:
            query = """
                SELECT o.*, s.name as strategy_name
                FROM orders o
                LEFT JOIN strategies s ON o.strategy_id = s.id
                WHERE 1=1
            """
            params = []

            if side != 'all':
                query += " AND o.side = ?"
                params.append(side)

            if status != 'all':
                query += " AND o.status = ?"
                params.append(status)

            if strategy_name:
                query += " AND s.name = ?"
                params.append(strategy_name)

            query += " ORDER BY o.opened_at DESC LIMIT ?"
            params.append(limit)

            orders = conn.execute(query, params).fetchall()
            return [dict(order) for order in orders]

    async def get_orders_by_strategy(self, strategy_id: int, limit: int = 100) -> List[dict]:
        """Отримання ордерів по стратегії"""
        with get_db() as conn:
            orders = conn.execute(
                "SELECT * FROM orders WHERE strategy_id = ? ORDER BY opened_at DESC LIMIT ?",
                (strategy_id, limit)
            ).fetchall()
            return [dict(order) for order in orders]

    async def get_pnl_history(self, strategy_id: int, limit: int = 100) -> List[dict]:
        """Отримання історії PnL по стратегії"""
        with get_db() as conn:
            orders = conn.execute("""
                SELECT closed_at, pnl, commission, symbol
                FROM orders 
                WHERE strategy_id = ? AND status = 'closed' AND pnl != 0
                ORDER BY closed_at DESC LIMIT ?
            """, (strategy_id, limit)).fetchall()

            history = []
            for order in orders:
                history.append({
                    'timestamp': order['closed_at'],
                    'pnl': order['pnl'],
                    'symbol': order['symbol']
                })

            return list(reversed(history))

    async def get_db_table(self, table_name: str, limit: int = 100) -> List[dict]:
        """Отримання даних з таблиці БД"""
        allowed_tables = ['orders', 'strategies', 'balances', 'logs', 'system_monitor']
        if table_name not in allowed_tables:
            return []

        with get_db() as conn:
            cursor = conn.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_logs(self, level: str = 'all', limit: int = 100) -> List[dict]:
        """Отримання логів з фільтром"""
        with get_db() as conn:
            if level == 'all':
                cursor = conn.execute("SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,))
            else:
                cursor = conn.execute(
                    "SELECT * FROM logs WHERE level = ? ORDER BY timestamp DESC LIMIT ?",
                    (level, limit)
                )
            logs = cursor.fetchall()
            return [dict(log) for log in logs]

    async def clear_logs(self):
        """Очищення логів"""
        with get_db() as conn:
            conn.execute("DELETE FROM logs")