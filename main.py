#!/usr/bin/env python3
"""Головний модуль запуску бота - Flask + окремий WebSocket"""

import asyncio
import signal
import sys
import os
import threading
from pathlib import Path
from datetime import datetime, timedelta
from config import Config
from database.db import init_db, add_log
from web.app import create_flask_app
from web.websocket_server import run_websocket_server
from telegram_bot.bot import TelegramBot
from trading.engine import TradingEngine
from utils.logger_utils import setup_logger
from monitoring.power_monitor import power_monitor

# Імпорти для стратегії технічного аналізу
#from backend.strategies.tech_analysis_strategy import TechAnalysisStrategy
#from backend.api.routes_tech_analysis import tech_analysis_bp, init_tech_strategy

logger = setup_logger('main')


class CryptoBot:
    def __init__(self):
        self.config = Config()
        self.trading_engine = None
        self.telegram_bot = None
        self.flask_app = None
        self.flask_thread = None
        self.running = True
        #self.tech_strategy = None  # Стратегія технічного аналізу
        self.forecast_check_task = None  # Завдання перевірки прогнозів

    async def init(self):
        """Ініціалізація всіх компонентів"""
        logger.info("Ініціалізація Crypto Bot...")

        # Перевірка API ключів
        logger.info(f"📊 Bybit API: {'✅ налаштовано' if self.config.BYBIT_API_KEY else '❌ відсутній'}")
        logger.info(f"📊 Telegram Bot: {'✅ налаштовано' if self.config.TELEGRAM_BOT_TOKEN else '❌ відсутній'}")
        logger.info(f"📊 News API: {'✅ налаштовано' if self.config.NEWS_API_KEY else '❌ відсутній'}")

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

        # Ініціалізація стратегії технічного аналізу
        logger.info("📊 Ініціалізація стратегії технічного аналізу...")
        logger.info("📊 Ініціалізація стратегії технічного аналізу...")
        #self.tech_strategy = TechAnalysisStrategy(self.trading_engine.get_db())
        #await self.tech_strategy.initialize()

        # Запускаємо WebSocket сервер в окремому потоці
        run_websocket_server(self.trading_engine, host='0.0.0.0', port=8765)

        # Flask веб-інтерфейс
        self.flask_app = create_flask_app(self.config, self.trading_engine)

        # Реєструємо blueprint для стратегії технічного аналізу
        #self.flask_app.register_blueprint(tech_analysis_bp)

        # Ініціалізуємо глобальний екземпляр стратегії для API
        #(self.tech_strategy)
        #logger.info("✅ API маршрути для технічного аналізу зареєстровано")

        self.flask_thread = threading.Thread(target=self._run_flask, daemon=True)
        self.flask_thread.start()

        # Запускаємо моніторинг електроенергії
        await power_monitor.start()

        # Запускаємо фоновий цикл для перевірки прогнозів
        self.forecast_check_task = asyncio.create_task(self._forecast_checker())

        logger.info("✅ Crypto Bot успішно ініціалізовано")
        logger.info("🌐 Веб-інтерфейс: http://localhost:8080")
        logger.info("🔌 WebSocket: ws://localhost:8765")
        logger.info("📱 Telegram бот активний (команди: /start, /status, /balance, /positions)")
        logger.info("💡 Для сигналів: перешліть повідомлення боту з коментарем '!signal', 'сигнал', 'signal'")
        logger.info("📊 Стратегія технічного аналізу доступна на вкладці 'Тех. Аналіз'")

    def _run_flask(self):
        """Запуск Flask в окремому потоці"""
        try:
            port = int(os.environ.get('FLASK_PORT', 8080))
            self.flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"Помилка запуску Flask: {e}")

    async def _forecast_checker(self):
        """Фоновий цикл для перевірки активних прогнозів"""
        logger.info("🔄 Запущено фоновий цикл перевірки прогнозів (кожні 5 хвилин)")
        while self.running:
            try:
                await asyncio.sleep(300)  # Кожні 5 хвилин

                # Отримуємо стратегію tech_analysis з trading_engine
                tech_strategy = None
                if self.trading_engine:
                    for strategy in self.trading_engine.strategies.values():
                        if strategy.name == 'tech_analysis':
                            tech_strategy = strategy
                            break

                if tech_strategy and tech_strategy.enabled:
                    # Перевіряємо чи є метод check_forecasts
                    if hasattr(tech_strategy, 'check_forecasts'):
                        await tech_strategy.check_forecasts()
                        logger.debug("Перевірку прогнозів виконано")
                    else:
                        # Якщо методу немає, просто логуємо
                        logger.debug("Метод check_forecasts не реалізовано в tech_analysis")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Помилка в перевірці прогнозів: {e}")

    async def _market_analyzer(self):
        """Фоновий цикл для аналізу ринку (кожні 15 хвилин)"""
        logger.info("🔄 Запущено фоновий цикл аналізу ринку (кожні 15 хвилин)")
        while self.running:
            try:
                await asyncio.sleep(900)  # Кожні 15 хвилин

                # Отримуємо стратегію tech_analysis з trading_engine
                tech_strategy = None
                if self.trading_engine:
                    for strategy in self.trading_engine.strategies.values():
                        if strategy.name == 'tech_analysis':
                            tech_strategy = strategy
                            break

                if tech_strategy and tech_strategy.enabled:
                    await self._analyze_all_symbols(tech_strategy)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Помилка в аналізі ринку: {e}")

    async def _analyze_all_symbols(self, tech_strategy):
        """Аналіз всіх символів та створення прогнозів"""
        try:
            for symbol in tech_strategy.symbols:
                try:
                    # Отримуємо поточну ціну
                    current_price = await self.trading_engine.exchange.get_current_price(symbol)
                    if current_price <= 0:
                        continue

                    # Отримуємо індикатори для аналізу
                    if hasattr(tech_strategy, '_get_indicators'):
                        indicators = await tech_strategy._get_indicators(symbol)
                        if not indicators:
                            continue

                        # Визначаємо сигнал
                        signal = 'long' if indicators.get('buy_signal') else 'short' if indicators.get(
                            'sell_signal') else 'neutral'
                        confidence = indicators.get('confidence', 0)

                        # Розраховуємо цільову ціну
                        target_price = current_price
                        if signal == 'long':
                            target_price = current_price * (1 + tech_strategy.take_profit_percent / 100)
                        elif signal == 'short':
                            target_price = current_price * (1 - tech_strategy.take_profit_percent / 100)

                        # Формуємо пояснення
                        explanation = [
                            f"📊 Тренд: {indicators.get('trend', 'neutral')}",
                            f"📈 RSI: {indicators.get('rsi', 50):.1f}",
                            f"🎯 Впевненість: {confidence:.0f}%"
                        ]

                        # Якщо є сигнал і впевненість достатня - створюємо прогноз
                        if signal != 'neutral' and confidence >= tech_strategy.min_confidence:
                            # Перевіряємо чи немає вже активного прогнозу
                            has_active = False
                            if hasattr(tech_strategy, 'forecasts'):
                                for forecast in tech_strategy.forecasts:
                                    if forecast.get('symbol') == symbol and forecast.get('status') == 'active':
                                        has_active = True
                                        break

                            if not has_active:
                                # Створюємо прогноз
                                forecast = {
                                    'id': int(datetime.now().timestamp()),
                                    'symbol': symbol,
                                    'signal_type': signal,
                                    'entry_price': current_price,
                                    'target_price': target_price,
                                    'confidence': confidence,
                                    'explanation': ' | '.join(explanation),
                                    'status': 'active',
                                    'success': None,
                                    'created_at': datetime.now().isoformat(),
                                    'expires_at': (datetime.now() + timedelta(hours=12)).isoformat(),
                                    'resolved_at': None,
                                    'resolved_price': None
                                }

                                if hasattr(tech_strategy, 'forecasts'):
                                    tech_strategy.forecasts.append(forecast)

                                logger.info(
                                    f"📊 Створено новий прогноз для {symbol}: {signal} з впевненістю {confidence:.0f}%")

                                # Відправляємо сповіщення в Telegram
                                if self.telegram_bot:
                                    await self.telegram_bot.send_notification(
                                        f"📊 *НОВИЙ ПРОГНОЗ* (TechAnalysis)\n"
                                        f"└ Пара: `{symbol}`\n"
                                        f"└ Сигнал: {'🟢 LONG' if signal == 'long' else '🔴 SHORT'}\n"
                                        f"└ Ціна входу: `${current_price:.2f}`\n"
                                        f"└ Ціль: `${target_price:.2f}`\n"
                                        f"└ Впевненість: {confidence:.0f}%\n"
                                        f"└ Час: до {explanation[2]}",
                                        parse_mode='Markdown'
                                    )

                                # Якщо стратегія активна - виконуємо угоду
                                if tech_strategy.enabled and signal in ['long', 'short']:
                                    await self._execute_forecast_trade(symbol, {
                                        'signal': signal,
                                        'current_price': current_price,
                                        'confidence': confidence,
                                        'target_price': target_price,
                                        'explanation': explanation
                                    }, tech_strategy)

                except Exception as e:
                    logger.error(f"Помилка аналізу {symbol}: {e}")

        except Exception as e:
            logger.error(f"Помилка аналізу всіх символів: {e}")

    async def _execute_forecast_trade(self, symbol: str, analysis_result: dict, tech_strategy):
        """Виконання угоди на основі прогнозу"""
        try:
            current_price = analysis_result.get('current_price', 0)
            if not current_price:
                klines = await self.trading_engine.get_klines(symbol, "1", 1)
                if klines:
                    current_price = klines[0]['close']

            if current_price <= 0:
                return

            # Розраховуємо розмір угоди
            trade_size = tech_strategy.balance * (tech_strategy.trade_size_percent / 100)

            if trade_size < 10:
                logger.warning(f"Недостатньо коштів для угоди {symbol}: баланс ${tech_strategy.balance:.2f}")
                return

            quantity = trade_size / current_price

            # Мінімальна кількість
            min_qty = 0.0001 if 'BTC' in symbol else 0.001
            if quantity < min_qty:
                quantity = min_qty
                trade_size = quantity * current_price

            # Виконуємо угоду
            side = 'buy' if analysis_result['signal'] == 'long' else 'sell'

            if self.trading_engine.config.DEFAULT_MODE == 'real':
                # Реальна торгівля
                result = await self.trading_engine.exchange.create_order(symbol, side, 'Market', quantity,
                                                                         current_price)
                if result.get('error'):
                    logger.error(f"Помилка виконання угоди {symbol}: {result}")
                    return
            else:
                # Симуляція
                logger.info(f"[SIMULATION] {side.upper()} {quantity:.6f} {symbol} @ ${current_price:.2f}")

            # Зберігаємо ордер в БД
            order_id = f"ta_{symbol}_{int(datetime.now().timestamp())}"
            tech_strategy._save_order(order_id, symbol, side, current_price, quantity, 'open',
                                      'LONG' if analysis_result['signal'] == 'long' else 'SHORT')

            # Додаємо до відкритих позицій
            tech_strategy.open_positions[symbol] = {
                'order_id': order_id,
                'entry_price': current_price,
                'quantity': quantity,
                'side': side,
                'opened_at': datetime.now().isoformat(),
                'signal_type': 'LONG' if analysis_result['signal'] == 'long' else 'SHORT'
            }

            tech_strategy.locked_balance += trade_size

            logger.info(
                f"✅ Виконано {analysis_result['signal'].upper()} угоду для {symbol}: ${trade_size:.2f} @ ${current_price:.2f}")

            if self.telegram_bot:
                await self.telegram_bot.send_notification(
                    f"📈 *ВІДКРИТО ПОЗИЦІЮ* (Прогноз)\n"
                    f"└ Пара: `{symbol}`\n"
                    f"└ Тип: {'🟢 LONG' if analysis_result['signal'] == 'long' else '🔴 SHORT'}\n"
                    f"└ Ціна: `${current_price:.2f}`\n"
                    f"└ Сума: `${trade_size:.2f}`\n"
                    f"└ Впевненість: {analysis_result.get('confidence', 0):.0f}%",
                    parse_mode='Markdown'
                )

        except Exception as e:
            logger.error(f"Помилка виконання угоди для {symbol}: {e}")

    async def run(self):
        """Запуск бота"""
        await self.init()

        # Запускаємо стратегії (включаючи технічного аналізу)
        await self.trading_engine.start_all_strategies()

        # Запускаємо додаткові фонові задачі
        market_analyzer_task = asyncio.create_task(self._market_analyzer())

        logger.info("🚀 Бот запущено та готовий до роботи")

        while self.running:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Помилка в головному циклі: {e}")
                await asyncio.sleep(5)

    async def shutdown(self, signum=None, frame=None):
        """Завершення роботи"""
        if signum:
            logger.info(f"Отримано сигнал завершення: {signum}")

        logger.info("Завершення роботи бота...")
        self.running = False

        # Зупиняємо перевірку прогнозів
        if self.forecast_check_task and not self.forecast_check_task.done():
            self.forecast_check_task.cancel()
            try:
                await self.forecast_check_task
            except asyncio.CancelledError:
                pass

        # Зупиняємо моніторинг електроенергії
        try:
            await power_monitor.stop()
        except Exception as e:
            logger.error(f"Помилка зупинки power_monitor: {e}")

        # Зупиняємо торговий двигун
        if self.trading_engine:
            await self.trading_engine.shutdown()

        # Зупиняємо Telegram бота
        if self.telegram_bot:
            await self.telegram_bot.shutdown()

        add_log("INFO", "system", "Бот завершив роботу")
        logger.info("Бот завершив роботу")

        # Невелика затримка перед виходом
        await asyncio.sleep(1)
        sys.exit(0)


def main():
    bot = CryptoBot()

    def signal_handler(sig, frame):
        """Обробник сигналів - створює задачу для завершення"""
        try:
            # Отримуємо поточний цикл подій
            loop = asyncio.get_running_loop()
            loop.create_task(bot.shutdown(sig))
        except RuntimeError:
            # Якщо цикл не запущений, створюємо новий
            asyncio.run(bot.shutdown(sig))

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
        try:
            asyncio.run(bot.shutdown())
        except:
            pass


if __name__ == "__main__":
    main()























