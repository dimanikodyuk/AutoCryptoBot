#!/usr/bin/env python3
"""Головний модуль запуску бота"""

import asyncio
import logging
import signal
import threading
import os
import sys
from pathlib import Path

from config import Config
from database.db import init_db, add_log, cleanup_old_price_history
from web.app import create_flask_app
from telegram_bot.bot import TelegramBot
from trading.engine import TradingEngine
from utils.logger_utils import setup_logger

# Налаштування логування
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = setup_logger('main')


class CryptoBot:
    def __init__(self):
        self.config = Config()
        self.trading_engine = None
        self.telegram_bot = None
        self.flask_app = None
        self.flask_thread = None
        self.running = True
        self._cleanup_task = None

    async def init(self):
        """Ініціалізація всіх компонентів"""
        logger.info("Ініціалізація Crypto Bot...")

        # Перевірка API ключів
        logger.info(
            f"📊 NewsAPI Key: {'✅ ' + self.config.NEWS_API_KEY[:10] + '...' if self.config.NEWS_API_KEY else '❌ відсутній'}")
        logger.info(
            f"📊 Telegram Token: {'✅ ' + self.config.TELEGRAM_BOT_TOKEN[:10] + '...' if self.config.TELEGRAM_BOT_TOKEN else '❌ відсутній'}")
        logger.info(f"📊 Bybit API: {'✅ налаштовано' if self.config.BYBIT_API_KEY else '❌ відсутній'}")

        # Ініціалізація БД
        init_db()
        add_log("INFO", "system", "Бот запускається")

        # Очищення старої price_history при запуску
        try:
            cleanup_old_price_history(max_days=3)
            logger.info("Очищення старих даних price_history виконано")
        except Exception as e:
            logger.error(f"Помилка очищення price_history: {e}")

        # Торговий двигун
        self.trading_engine = TradingEngine(self.config)
        await self.trading_engine.init()

        # Telegram бот
        self.telegram_bot = TelegramBot(self.config, self.trading_engine)
        await self.telegram_bot.init()

        # Передаємо telegram_bot в двигун (після створення)
        self.trading_engine.set_telegram_bot(self.telegram_bot)

        # Flask веб-інтерфейс (в окремому потоці)
        self.flask_app = create_flask_app(self.config, self.trading_engine)
        self.flask_thread = threading.Thread(target=self._run_flask, daemon=True)
        self.flask_thread.start()

        # Запускаємо фоновий процес очищення
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info("Crypto Bot успішно ініціалізовано")

    def _run_flask(self):
        """Запуск Flask в окремому потоці"""
        try:
            port = int(os.environ.get('FLASK_PORT', 8080))
            self.flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"Помилка запуску Flask: {e}")

    async def _cleanup_loop(self):
        """Фоновий процес очищення старих даних (раз на добу)"""
        while self.running:
            try:
                await asyncio.sleep(86400)  # 24 години
                cleanup_old_price_history(max_days=3)
                logger.info("🔄 Автоматичне очищення price_history виконано")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Помилка в циклі очищення: {e}")

    async def run(self):
        """Запуск бота"""
        await self.init()

        # Запускаємо стратегії, які були активні при минулому запуску
        await self.trading_engine.start_all_strategies()

        logger.info("Бот запущено")
        logger.info("🌐 Flask веб-інтерфейс: http://localhost:8080")
        logger.info("📱 Telegram бот активний")

        while self.running:
            try:
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Помилка в головному циклі: {e}")
                await asyncio.sleep(5)

    async def shutdown(self, signum=None, frame=None):
        """Завершення роботи"""
        if signum:
            logger.info(f"Отримано сигнал завершення: {signum}")

        logger.info("Завершення роботи бота...")
        self.running = False

        # Зупиняємо фоновий процес
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Зупиняємо торговий двигун
        if self.trading_engine:
            await self.trading_engine.shutdown()

        # Зупиняємо Telegram бота
        if self.telegram_bot:
            await self.telegram_bot.shutdown()

        add_log("INFO", "system", "Бот завершив роботу")
        logger.info("Бот завершив роботу")

        # Примусовий вихід
        sys.exit(0)


def main():
    bot = CryptoBot()

    def signal_handler(sig, frame):
        """Обробник сигналів"""
        asyncio.create_task(bot.shutdown(sig))

    # Реєстрація обробників сигналів
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Отримано сигнал завершення від клавіатури")
    except Exception as e:
        logger.error(f"Критична помилка: {e}")
        import traceback
        traceback.print_exc()
        asyncio.run(bot.shutdown())


if __name__ == "__main__":
    main()