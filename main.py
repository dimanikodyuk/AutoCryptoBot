#!/usr/bin/env python3
"""Головний модуль запуску бота - Flask + окремий WebSocket"""

import asyncio
import signal
import sys
import os
import threading
from pathlib import Path

from config import Config
from database.db import init_db, add_log
from web.app import create_flask_app
from web.websocket_server import run_websocket_server
from telegram_bot.bot import TelegramBot
from trading.engine import TradingEngine
from utils.logger_utils import setup_logger

logger = setup_logger('main')


class CryptoBot:
    def __init__(self):
        self.config = Config()
        self.trading_engine = None
        self.telegram_bot = None
        self.flask_app = None
        self.flask_thread = None
        self.running = True

    async def init(self):
        """Ініціалізація всіх компонентів"""
        logger.info("Ініціалізація Crypto Bot...")

        # Перевірка API ключів
        logger.info(f"📊 Bybit API: {'✅ налаштовано' if self.config.BYBIT_API_KEY else '❌ відсутній'}")

        # Ініціалізація БД
        init_db()
        add_log("INFO", "system", "Бот запускається")

        # Торговий двигун
        self.trading_engine = TradingEngine(self.config)
        await self.trading_engine.init()

        # Telegram бот
        self.telegram_bot = TelegramBot(self.config, self.trading_engine)
        await self.telegram_bot.init()

        # Передаємо telegram_bot в двигун
        self.trading_engine.set_telegram_bot(self.telegram_bot)

        # Запускаємо WebSocket сервер в окремому потоці
        run_websocket_server(self.trading_engine, host='0.0.0.0', port=8765)

        # Flask веб-інтерфейс
        self.flask_app = create_flask_app(self.config, self.trading_engine)
        self.flask_thread = threading.Thread(target=self._run_flask, daemon=True)
        self.flask_thread.start()

        logger.info("Crypto Bot успішно ініціалізовано")

    def _run_flask(self):
        """Запуск Flask в окремому потоці"""
        try:
            port = int(os.environ.get('FLASK_PORT', 8080))
            self.flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"Помилка запуску Flask: {e}")

    async def run(self):
        """Запуск бота"""
        await self.init()

        # Запускаємо стратегії
        await self.trading_engine.start_all_strategies()

        logger.info("🚀 Бот запущено")
        logger.info("🌐 Веб-інтерфейс: http://localhost:8080")
        logger.info("🔌 WebSocket: ws://localhost:8765")
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

        if self.trading_engine:
            await self.trading_engine.shutdown()

        if self.telegram_bot:
            await self.telegram_bot.shutdown()

        add_log("INFO", "system", "Бот завершив роботу")
        logger.info("Бот завершив роботу")
        sys.exit(0)


def main():
    bot = CryptoBot()

    def signal_handler(sig, frame):
        asyncio.create_task(bot.shutdown(sig))

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Отримано сигнал завершення")
    except Exception as e:
        logger.error(f"Критична помилка: {e}")
        import traceback
        traceback.print_exc()
        asyncio.run(bot.shutdown())


if __name__ == "__main__":
    main()