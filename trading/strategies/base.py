import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Базовий клас для всіх стратегій"""

    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        self.strategy_id = strategy_id
        self.name = name
        self.mode = mode
        self.exchange = exchange
        self.enabled = False

    async def start(self):
        """Запуск стратегії"""
        self.enabled = True
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
            'mode': self.mode
        }