import logging
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from utils.logger_utils import setup_logger

logger = setup_logger('telegram')


class TelegramBot:
    def __init__(self, config, trading_engine):
        self.config = config
        self.trading_engine = trading_engine
        self.application = None
        self._running = False
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.waiting_for_signal = set()  # множина chat_id, які чекають на сигнал

    async def init(self):
        if not self.config.TELEGRAM_BOT_TOKEN:
            logger.warning("TELEGRAM_BOT_TOKEN не задано")
            return

        self.application = Application.builder().token(self.config.TELEGRAM_BOT_TOKEN).build()

        # Команди
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("strategies", self.strategies_command))
        self.application.add_handler(CommandHandler("emergency", self.emergency_command))
        self.application.add_handler(CommandHandler("stop_bot", self.stop_bot_command))
        self.application.add_handler(CommandHandler("report", self.daily_report_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("balance", self.balance_command))
        self.application.add_handler(CommandHandler("positions", self.positions_command))

        # Обробник всіх текстових повідомлень (для сигналів)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        self._running = True

        logger.info("Telegram бот запущено")
        await self.send_notification("🟢 Бот запущено та готовий до роботи")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.chat_id = update.effective_chat.id
        await update.message.reply_text(
            "🤖 Crypto Trading Bot\n\n"
            "💡 Як використовувати:\n"
            "1. Напишіть `!signal`\n"
            "2. Перешліть мені повідомлення з сигналом\n"
            "3. Я розпізнаю сигнал і відкрию позицію\n\n"
            "📋 Формат сигналу:\n"
            "🟢 LONG - $BTC\n"
            "- Entry: 50000\n"
            "- SL: 49000\n"
            "🎯 TP1: 51000\n"
            "🎯 TP2: 52000\n\n"
            "🔴 SHORT - $ETH\n"
            "- Entry: 3000\n"
            "- SL: 3100\n"
            "🎯 TP1: 2900\n"
            "🎯 TP2: 2800\n\n"
            "📊 Команди:\n"
            "/status - стан бота\n"
            "/balance - мій баланс\n"
            "/positions - відкриті позиції\n"
            "/emergency - екстрена зупинка\n\n"
            f"⚙️ Режим: {self.config.DEFAULT_MODE}"
        )
        await self.send_notification(f"👤 Новий користувач підключився: {update.effective_user.username}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Crypto Trading Bot - Допомога\n\n"
            "📊 Команди:\n"
            "/status - показати поточний стан бота\n"
            "/strategies - список стратегій та їх статус\n"
            "/report - денний звіт по PnL\n"
            "/emergency - ЕКСТРЕНА ЗУПИНКА всіх стратегій\n"
            "/stop_bot - повна зупинка бота\n"
            "/balance - мій баланс\n"
            "/positions - відкриті позиції\n"
            "/help - ця довідка\n\n"
            "📈 Стратегії:\n"
            "- Grid: сіткова торгівля\n"
            "- Scalp: скальпінг з MACD/RSI/StochRSI\n"
            "- News: торгівля за новинами\n"
            "- Signals: ручні сигнали\n\n"
            f"⚙️ Поточний режим: {self.config.DEFAULT_MODE}"
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        summary = await self.trading_engine.get_summary()
        message = f"📊 Стан бота\n"
        message += f"🎮 Режим: {self.config.DEFAULT_MODE}\n"
        message += f"📈 Активних стратегій: {summary['active_strategies']}\n"
        message += f"💰 Загальний PnL: ${summary['total_pnl']:.2f}\n"
        message += f"💵 Загальний баланс: ${summary['total_balance']:.2f}\n\n"
        message += f"┌ Деталі по стратегіях:\n"
        for s in summary['strategies']:
            status_icon = "🟢" if s['enabled'] else "🔴"
            pnl_icon = "📈" if s.get('total_pnl', 0) >= 0 else "📉"
            message += f"├ {status_icon} {s['name'].upper()}: {pnl_icon} ${s.get('total_pnl', 0):.2f}\n"
        await update.message.reply_text(message)

    async def strategies_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = "📈 Стратегії\n\n"
        for strategy in self.trading_engine.strategies.values():
            status = await strategy.get_status()
            status_icon = "✅ Активна" if status['enabled'] else "❌ Зупинена"
            message += f"• {status['name'].upper()}: {status_icon}\n"
            message += f"  └ PnL: ${status.get('total_pnl', 0):.2f} | Угоди: {status.get('total_trades', 0)}\n"
        message += "\nДля керування використовуй веб-інтерфейс"
        await update.message.reply_text(message)

    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_balance(update.effective_chat.id)

    async def positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_positions(update.effective_chat.id)

    async def daily_report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send_daily_report()

    async def emergency_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.warning(f"Отримано команду /emergency від {update.effective_user.username}")
        await update.message.reply_text("🛑 ЕКСТРЕНА ЗУПИНКА 🛑\nЗакриваю всі позиції...")
        await self.trading_engine.emergency_stop_all()
        await update.message.reply_text("✅ Всі стратегії зупинено")

    async def stop_bot_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🛑 Зупинка бота...")
        await self.send_notification("🛑 Бот зупиняється користувачем")
        await self.trading_engine.shutdown()

    # ============= ОБРОБНИК ПОВІДОМЛЕНЬ (СИГНАЛИ) =============

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Обробник всіх повідомлень - шукає сигнали в пересланих повідомленнях
        """
        message = update.effective_message
        if not message:
            return

        text = (message.text or message.caption or '').strip().lower()
        chat_id = message.chat_id

        logger.info(f"📨 Отримано повідомлення: {text[:100] if text else 'None'}")

        # ============= КЛЮЧОВЕ СЛОВО ДЛЯ АКТИВАЦІЇ РЕЖИМУ СИГНАЛУ =============
        if text in ['!signal', 'сигнал', 'signal', '!sig', 'sig']:
            # Активуємо режим очікування сигналу
            self.waiting_for_signal.add(chat_id)
            await self.send_notification(
                "📡 *РЕЖИМ ПРИЙОМУ СИГНАЛУ АКТИВОВАНО*\n\n"
                "Тепер перешліть мені повідомлення з сигналом.\n"
                "Я розпізнаю його автоматично.\n\n"
                "⏰ Режим активний 60 секунд",
                parse_mode='Markdown'
            )
            # Автоматичне вимкнення через 60 секунд
            asyncio.create_task(self._disable_signal_mode(chat_id, 60))
            return

        # ============= ПЕРЕСЛАНЕ ПОВІДОМЛЕННЯ (СИГНАЛ) =============
        if chat_id in self.waiting_for_signal:
            # Це сигнал, бо ми в режимі очікування
            original_text = message.text or message.caption

            if original_text:
                await self._process_signal(original_text, chat_id, "telegram")
                # Вимикаємо режим після обробки
                self.waiting_for_signal.discard(chat_id)
            else:
                await self.send_notification("❌ Не вдалося отримати текст сигналу")
                self.waiting_for_signal.discard(chat_id)
            return

        # ============= ПЕРЕСЛАНЕ ПОВІДОМЛЕННЯ (БЕЗ КЛЮЧОВОГО СЛОВА) =============
        if message.forward_from_chat or message.forward_from:
            source = "групи" if message.forward_from_chat else "користувача"
            await self.send_notification(
                f"📨 Переслане повідомлення отримано з {source}\n\n"
                f"Щоб створити сигнал, спочатку напишіть `!signal`",
                parse_mode='Markdown'
            )
            return

    async def _disable_signal_mode(self, chat_id: int, delay: int):
        """Автоматичне вимкнення режиму сигналу через delay секунд"""
        await asyncio.sleep(delay)
        if chat_id in self.waiting_for_signal:
            self.waiting_for_signal.discard(chat_id)
            await self.send_notification("⏰ Режим прийому сигналу вимкнено (таймаут)")

    async def _process_signal(self, signal_text: str, chat_id: int, source: str):
        """
        Обробка знайденого сигналу
        """
        logger.info(f"🔍 Розпізнавання сигналу: {signal_text[:200]}")

        # Знаходимо стратегію signals
        signals_strategy = None
        for strategy in self.trading_engine.strategies.values():
            if strategy.name == 'signals':
                signals_strategy = strategy
                break

        if not signals_strategy:
            await self.send_notification("❌ Стратегія сигналів не знайдена")
            return

        # Парсимо текст
        parsed = signals_strategy.parse_signal_text(signal_text)

        if not parsed:
            await self.send_notification(
                f"❌ *НЕ ВДАЛОСЯ РОЗПІЗНАТИ СИГНАЛ*\n\n"
                f"📝 Отриманий текст:\n```\n{signal_text[:300]}\n```\n\n"
                f"📋 *Потрібен формат:*\n"
                f"• LONG або SHORT (🟢/🔴)\n"
                f"• Символ: $ZETA або ZETA\n"
                f"• Entry: ціна (0.05737)\n"
                f"• SL: стоп-лосс (0.04813)\n"
                f"• TP1, TP2, TP3... (0.06559)",
                parse_mode='Markdown'
            )
            return

        # Додаємо сигнал
        signal = await signals_strategy.add_signal(parsed)

        if signal:
            await self.send_notification(
                f"✅ *СИГНАЛ ПРИЙНЯТО!*\n\n"
                f"🎯 Тип: {signal.signal_type}\n"
                f"💰 Символ: {signal.symbol}\n"
                f"💵 Entry: ${signal.entry_price:.6f}\n"
                f"🛑 SL: ${signal.stop_loss:.6f}\n"
                f"🎯 TP: {', '.join([f'${tp:.6f}' for tp in signal.take_profits[:3]])}\n\n"
                f"📊 Позиція відкрита! Слідкуйте в веб-інтерфейсі",
                parse_mode='Markdown'
            )
        else:
            await self.send_notification("❌ Помилка створення сигналу. Можливо недостатньо балансу?")

    # ============= ВНУТРІШНІ МЕТОДИ ДЛЯ СТАНУ =============

    async def _send_status(self, chat_id: int):
        """Відправка статусу"""
        signals_strategy = None
        for strategy in self.trading_engine.strategies.values():
            if strategy.name == 'signals':
                signals_strategy = strategy
                break

        if signals_strategy:
            status = await signals_strategy.get_status()
            await self.send_notification(
                f"📊 *СТАН СТРАТЕГІЇ СИГНАЛІВ*\n\n"
                f"💰 Баланс: ${status['balance']:.2f}\n"
                f"📈 PnL: ${status['total_pnl']:.2f}\n"
                f"📊 Угод: {status['total_trades']}\n"
                f"🏆 Win Rate: {status['win_rate']}%\n"
                f"🟢 Активних сигналів: {len(status['active_signals'])}",
                parse_mode='Markdown'
            )

    async def _send_balance(self, chat_id: int):
        """Відправка балансу"""
        signals_strategy = None
        for strategy in self.trading_engine.strategies.values():
            if strategy.name == 'signals':
                signals_strategy = strategy
                break

        if signals_strategy:
            status = await signals_strategy.get_status()
            await self.send_notification(
                f"💼 *МІЙ БАЛАНС*\n\n"
                f"💰 Доступно: ${status['available_balance']:.2f}\n"
                f"🔒 Заблоковано: ${status['locked_balance']:.2f}\n"
                f"💵 Всього: ${status['balance']:.2f}\n"
                f"📈 PnL: ${status['total_pnl']:.2f}",
                parse_mode='Markdown'
            )

    async def _send_positions(self, chat_id: int):
        """Відправка відкритих позицій"""
        signals_strategy = None
        for strategy in self.trading_engine.strategies.values():
            if strategy.name == 'signals':
                signals_strategy = strategy
                break

        if signals_strategy:
            status = await signals_strategy.get_status()
            if status['active_signals']:
                text = "📊 *ВІДКРИТІ ПОЗИЦІЇ*\n\n"
                for s in status['active_signals']:
                    text += f"└ {s['type']} *{s['symbol']}*\n"
                    text += f"   ├ Entry: ${s['entry_price']:.6f}\n"
                    text += f"   ├ SL: ${s['stop_loss']:.6f}\n"
                    text += f"   ├ TP: {', '.join([f'${tp:.6f}' for tp in s['take_profits'][:3]])}\n"
                    text += f"   └ Прогрес: {s['partial_closes']}/{s['total_tp']}\n\n"
                await self.send_notification(text, parse_mode='Markdown')
            else:
                await self.send_notification("📭 Немає відкритих позицій")

    # ============= СПОВІЩЕННЯ =============

    async def send_notification(self, message: str, parse_mode: str = None):
        """Відправка сповіщення в Telegram"""
        if self.chat_id and self.application and self._running:
            try:
                await self.application.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=parse_mode
                )
                logger.info(f"Сповіщення відправлено: {message[:50]}...")
            except Exception as e:
                logger.error(f"Помилка відправки сповіщення: {e}")

    async def send_trade_notification(self, strategy: str, symbol: str, side: str,
                                       price: float, quantity: float, pnl: float = None):
        if side == 'buy':
            icon = "📈"
            action = "ВІДКРИТО ПОЗИЦІЮ"
        else:
            icon = "📉"
            action = "ЗАКРИТО ПОЗИЦІЮ"

        message = f"{icon} {action}\n"
        message += f"Стратегія: {strategy}\n"
        message += f"Пара: {symbol}\n"
        message += f"Ціна: ${price:.2f}\n"
        message += f"Кількість: {quantity:.6f}\n"

        if pnl is not None:
            pnl_icon = "✅" if pnl >= 0 else "❌"
            message += f"PnL: {pnl_icon} ${pnl:.2f}\n"

        message += f"\n⏰ {datetime.now().strftime('%H:%M:%S')}"
        await self.send_notification(message)

    async def send_daily_report(self):
        summary = await self.trading_engine.get_summary()

        message = f"📊 ДЕННИЙ ЗВІТ\n"
        message += f"📅 {datetime.now().strftime('%d.%m.%Y')}\n\n"
        message += f"💰 Загальний PnL: ${summary['total_pnl']:.2f}\n\n"

        for s in summary['strategies']:
            status = "✅" if s['enabled'] else "❌"
            message += f"{status} {s['name'].upper()}\n"
            message += f"   └ Баланс: ${s.get('balance', 0):.2f} | PnL: ${s.get('total_pnl', 0):.2f} | Угоди: {s.get('total_trades', 0)}\n"

        await self.send_notification(message)

    async def send_strategy_status(self, strategy: str, enabled: bool):
        icon = "✅" if enabled else "❌"
        action = "запущено" if enabled else "зупинено"
        await self.send_notification(f"{icon} Стратегія {strategy} {action}")

    async def shutdown(self):
        self._running = False
        if self.application:
            await self.send_notification("🟠 Бот завершує роботу")
            try:
                if hasattr(self.application, 'updater') and self.application.updater:
                    await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            except Exception as e:
                logger.error(f"Помилка: {e}")
        logger.info("Telegram бот завершив роботу")