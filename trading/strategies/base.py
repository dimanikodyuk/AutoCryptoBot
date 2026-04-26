import logging
from abc import ABC, abstractmethod
from datetime import datetime, date
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Базовий клас для всіх стратегій"""

    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        self.strategy_id = strategy_id
        self.name = name
        self.mode = mode
        self.exchange = exchange
        self.enabled = False

        # Ліміти (з config.py)
        self.max_daily_drawdown = 10.0  # % (за замовчуванням)
        self.max_daily_trades = 50  # кількість угод
        self.min_balance_for_trading = 10.0  # USDT

        # Стан лімітів
        self.daily_trades_count = 0
        self.daily_start_balance = 0.0
        self.daily_lowest_balance = 0.0
        self.last_reset_date = None

        # Стан стратегії
        self._is_blocked = False
        self._block_reason = None

    async def start(self):
        """Запуск стратегії"""
        self.enabled = True
        self._check_and_reset_daily_limits()
        logger.info(f"Стратегія {self.name} запущена")

    async def stop(self):
        """Зупинка стратегії"""
        self.enabled = False
        logger.info(f"Стратегія {self.name} зупинена")

    @abstractmethod
    async def analyze(self) -> dict:
        """Аналіз ринку"""
        pass

    @abstractmethod
    async def execute(self, signal: dict):
        """Виконання сигналу"""
        pass

    async def get_status(self) -> dict:
        """Отримання статусу"""
        return {
            'id': self.strategy_id,
            'name': self.name,
            'enabled': self.enabled,
            'mode': self.mode,
            'is_blocked': self._is_blocked,
            'block_reason': self._block_reason
        }

    # ============= МЕТОДИ ЛІМІТІВ =============

    def _check_and_reset_daily_limits(self):
        """Перевірка та скидання денних лімітів"""
        today = date.today()

        if self.last_reset_date != today:
            # Скидаємо денні ліміти
            self.daily_trades_count = 0
            self.daily_start_balance = self.get_current_balance()
            self.daily_lowest_balance = self.daily_start_balance
            self.last_reset_date = today
            self._is_blocked = False
            self._block_reason = None
            logger.info(f"[{self.name}] Денні ліміти скинуто. Початковий баланс: ${self.daily_start_balance:.2f}")

    def get_current_balance(self) -> float:
        """Отримання поточного балансу (перевизначається в дочірніх класах)"""
        return getattr(self, 'balance', 0.0) + getattr(self, 'total_balance', 0.0)

    def check_daily_drawdown(self) -> bool:
        """
        Перевірка денного drawdown
        Повертає True якщо ліміт перевищено
        """
        current_balance = self.get_current_balance()

        # Оновлюємо найнижчий баланс
        if current_balance < self.daily_lowest_balance:
            self.daily_lowest_balance = current_balance

        # Розраховуємо drawdown від початку дня
        if self.daily_start_balance > 0:
            drawdown_percent = (self.daily_start_balance - self.daily_lowest_balance) / self.daily_start_balance * 100
        else:
            drawdown_percent = 0

        if drawdown_percent >= self.max_daily_drawdown:
            self._is_blocked = True
            self._block_reason = f"Перевищено денний drawdown: {drawdown_percent:.1f}% > {self.max_daily_drawdown}%"
            logger.warning(f"[{self.name}] ❌ {self._block_reason}")
            return True

        return False

    def check_daily_trades_limit(self) -> bool:
        """
        Перевірка кількості денних угод
        Повертає True якщо ліміт перевищено
        """
        if self.daily_trades_count >= self.max_daily_trades:
            self._is_blocked = True
            self._block_reason = f"Перевищено денний ліміт угод: {self.daily_trades_count} >= {self.max_daily_trades}"
            logger.warning(f"[{self.name}] ❌ {self._block_reason}")
            return True

        return False

    def check_min_balance(self, required_amount: float = None) -> bool:
        """
        Перевірка мінімального балансу
        Повертає True якщо балансу недостатньо
        """
        current_balance = self.get_current_balance()

        if required_amount:
            if current_balance < required_amount:
                self._is_blocked = True
                self._block_reason = f"Недостатньо балансу: потрібно ${required_amount:.2f}, є ${current_balance:.2f}"
                logger.warning(f"[{self.name}] ❌ {self._block_reason}")
                return True

        if current_balance < self.min_balance_for_trading:
            self._is_blocked = True
            self._block_reason = f"Баланс нижче мінімального: ${current_balance:.2f} < ${self.min_balance_for_trading:.2f}"
            logger.warning(f"[{self.name}] ❌ {self._block_reason}")
            return True

        return False

    def can_trade(self, required_amount: float = None) -> bool:
        """
        Перевірка чи можна торгувати
        Повертає True якщо всі ліміти в порядку
        """
        # Оновлюємо денні ліміти
        self._check_and_reset_daily_limits()

        # Перевіряємо чи не заблокована стратегія
        if self._is_blocked:
            logger.debug(f"[{self.name}] Торгівля заблокована: {self._block_reason}")
            return False

        # Перевіряємо drawdown
        if self.check_daily_drawdown():
            return False

        # Перевіряємо кількість угод
        if self.check_daily_trades_limit():
            return False

        # Перевіряємо баланс
        if self.check_min_balance(required_amount):
            return False

        return True

    def increment_daily_trades(self):
        """Збільшення лічильника денних угод"""
        self.daily_trades_count += 1
        logger.info(f"[{self.name}] Денних угод: {self.daily_trades_count}/{self.max_daily_trades}")

        # Превентивне попередження
        if self.daily_trades_count >= self.max_daily_trades * 0.8:
            remaining = self.max_daily_trades - self.daily_trades_count
            logger.warning(f"[{self.name}] ⚠️ Залишилось {remaining} денних угод до ліміту")

    def update_balance_for_drawdown(self):
        """Оновлення балансу для drawdown (викликати після кожної угоди)"""
        current_balance = self.get_current_balance()
        if current_balance < self.daily_lowest_balance:
            self.daily_lowest_balance = current_balance

            # Перевіряємо чи не перевищили ліміт
            if self.daily_start_balance > 0:
                drawdown_percent = (
                                               self.daily_start_balance - self.daily_lowest_balance) / self.daily_start_balance * 100
                if drawdown_percent >= self.max_daily_drawdown * 0.9:
                    logger.warning(
                        f"[{self.name}] ⚠️ Наближення до ліміту drawdown: {drawdown_percent:.1f}% / {self.max_daily_drawdown}%")

    async def reset_limits(self):
        """Примусове скидання лімітів"""
        self.daily_trades_count = 0
        self.daily_start_balance = self.get_current_balance()
        self.daily_lowest_balance = self.daily_start_balance
        self.last_reset_date = date.today()
        self._is_blocked = False
        self._block_reason = None
        logger.info(f"[{self.name}] Ліміти примусово скинуто. Баланс: ${self.daily_start_balance:.2f}")