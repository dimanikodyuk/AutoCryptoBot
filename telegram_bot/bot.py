import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from utils.logger_utils import setup_logger

logger = setup_logger('telegram')


class TelegramBot:
    def __init__(self, config, trading_engine):
        self.config = config
        self.trading_engine = trading_engine
        self.application = None
        self._running = False
        self.chat_id = config.TELEGRAM_CHAT_ID

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

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        self._running = True

        logger.info("Telegram бот запущено")
        await self.send_notification("🟢 Бот запущено та готовий до роботи")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.chat_id = update.effective_chat.id
        await update.message.reply_text(
            "🤖 *Crypto Trading Bot*\n\n"
            "Доступні команди:\n"
            "/status - поточний стан\n"
            "/strategies - список стратегій\n"
            "/report - денний звіт\n"
            "/emergency - ЕКСТРЕНА ЗУПИНКА\n"
            "/stop_bot - зупинити бота\n\n"
            f"Режим: {self.config.DEFAULT_MODE}",
            parse_mode='Markdown'
        )
        await self.send_notification(f"👤 Новий користувач підключився: {update.effective_user.username}")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        summary = await self.trading_engine.get_summary()
        message = f"📊 *Стан бота*\n"
        message += f"Режим: {self.config.DEFAULT_MODE}\n"
        message += f"Активних стратегій: {summary['active_strategies']}\n"
        message += f"Загальний PnL: ${summary['total_pnl']:.2f}\n\n"
        for s in summary['strategies']:
            status = "✅" if s['enabled'] else "❌"
            message += f"{status} *{s['name'].upper()}*: PnL ${s.get('total_pnl', 0):.2f}\n"
        await update.message.reply_text(message, parse_mode='Markdown')

    async def strategies_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = "📈 *Стратегії*\n\n"
        for strategy in self.trading_engine.strategies.values():
            status = await strategy.get_status()
            status_icon = "✅ Активна" if status['enabled'] else "❌ Зупинена"
            message += f"• *{status['name']}*: {status_icon}\n"
        message += "\nДля керування використовуй веб-інтерфейс"
        await update.message.reply_text(message, parse_mode='Markdown')

    async def daily_report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send_daily_report()

    async def emergency_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.warning(f"Отримано команду /emergency від {update.effective_user.username}")
        await update.message.reply_text("🛑 *ЕКСТРЕНА ЗУПИНКА* 🛑\nЗакриваю всі позиції...", parse_mode='Markdown')
        await self.trading_engine.emergency_stop_all()
        await update.message.reply_text("✅ Всі стратегії зупинено")

    async def stop_bot_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🛑 Зупинка бота...")
        await self.send_notification("🛑 Бот зупиняється користувачем")
        await self.trading_engine.shutdown()

    # ============= СПОВІЩЕННЯ =============

    async def send_notification(self, message: str, parse_mode: str = None):
        if self.chat_id and self.application and self._running:
            try:
                await self.application.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=parse_mode
                )
            except Exception as e:
                logger.error(f"Помилка відправки сповіщення: {e}")

    async def send_trade_notification(self, strategy: str, symbol: str, side: str,
                                       price: float, quantity: float, pnl: float = None):
        if side == 'buy':
            icon = "📈"
            action = "ВІДКРИТО ПОЗИЦІЮ"
            color = "🟢"
        else:
            icon = "📉"
            action = "ЗАКРИТО ПОЗИЦІЮ"
            color = "🔴"

        message = f"{icon} *{action}*\n"
        message += f"Стратегія: `{strategy}`\n"
        message += f"Пара: `{symbol}`\n"
        message += f"Ціна: `${price:.2f}`\n"
        message += f"Кількість: `{quantity:.6f}`\n"

        if pnl is not None:
            pnl_icon = "✅" if pnl >= 0 else "❌"
            message += f"PnL: {pnl_icon} `${pnl:.2f}`\n"

        message += f"\n⏰ {datetime.now().strftime('%H:%M:%S')}"
        await self.send_notification(message, parse_mode='Markdown')

    async def send_daily_report(self):
        summary = await self.trading_engine.get_summary()

        message = f"📊 *ДЕННИЙ ЗВІТ*\n"
        message += f"📅 {datetime.now().strftime('%d.%m.%Y')}\n\n"
        message += f"💰 Загальний PnL: `${summary['total_pnl']:.2f}`\n\n"

        for s in summary['strategies']:
            status = "✅" if s['enabled'] else "❌"
            message += f"{status} *{s['name'].upper()}*\n"
            message += f"   Баланс: `${s.get('balance', 0):.2f}`\n"
            message += f"   PnL: `${s.get('total_pnl', 0):.2f}`\n"
            message += f"   Угод: `{s.get('total_trades', 0)}`\n\n"

        await self.send_notification(message, parse_mode='Markdown')

    async def send_drawdown_warning(self, strategy: str, drawdown: float, limit: float):
        message = f"⚠️ *УВАГА! DRAWDOWN* ⚠️\n"
        message += f"Стратегія: `{strategy}`\n"
        message += f"Поточний drawdown: `{drawdown:.2f}%`\n"
        message += f"Ліміт: `{limit:.2f}%`\n\n"
        message += f"Рекомендується перевірити налаштування!"
        await self.send_notification(message, parse_mode='Markdown')

    async def send_mode_change(self, old_mode: str, new_mode: str):
        if new_mode == 'real':
            message = f"🔴 *ЗМІНА РЕЖИМУ* 🔴\n"
            message += f"`{old_mode}` ➡️ `{new_mode}`\n\n"
            message += f"⚠️ **УВАГА!** Використовуються РЕАЛЬНІ кошти!"
        else:
            message = f"🟡 *ЗМІНА РЕЖИМУ*\n`{old_mode}` ➡️ `{new_mode}`"
        await self.send_notification(message, parse_mode='Markdown')

    async def send_strategy_status(self, strategy: str, enabled: bool):
        icon = "✅" if enabled else "❌"
        action = "запущено" if enabled else "зупинено"
        await self.send_notification(f"{icon} Стратегія *{strategy}* {action}", parse_mode='Markdown')

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