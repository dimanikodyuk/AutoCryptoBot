import logging
import asyncio
import os
import requests
from datetime import datetime
from pathlib import Path
from typing import List
from dotenv import load_dotenv
from utils.logger_utils import setup_logger
from trading.strategies.base import BaseStrategy
from database.db import get_db, add_log
from config_manager import get_strategy_settings, save_strategy_settings

logger = setup_logger('news')


class NewsStrategy(BaseStrategy):
    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        super().__init__(strategy_id, name, mode, exchange)
        saved = get_strategy_settings('news')
        self.symbols = saved.get('symbols', ['BTC', 'ETH', 'SOL'])
        self.interval_minutes = saved.get('interval_minutes', 10)
        self.sensitivity = saved.get('sensitivity', 'medium')
        self.enabled = saved.get('enabled', False)
        self.trade_size_usdt = saved.get('trade_size_usdt', 20)

        self._last_error_time = None  # ← ДОДАТИ

        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0

        self.current_sentiment = 'neutral'
        self.last_news = []
        self.last_update = None
        self.articles_count = 0
        self._analysis_task = None
        self.news_api_key = None
        self.sentiment_history = []

        # Поточні позиції
        self.open_positions = {}

        self._load_api_key()
        self._load_history()
        logger.info(f"NewsStrategy: {self.symbols}, баланс ${self.balance}")

    @property
    def available_balance(self):
        return self.balance - self.locked_balance

    def get_current_balance(self) -> float:
        return self.balance

    def _load_api_key(self):
        env_path = Path(__file__).parent.parent.parent / '.env'
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            self.news_api_key = os.getenv('NEWS_API_KEY', '')
            if self.news_api_key:
                logger.info("✅ NEWS_API_KEY завантажено")
            else:
                logger.error("❌ NEWS_API_KEY не знайдено")
        else:
            logger.error("❌ .env не знайдено")

    async def start(self):
        await super().start()
        save_strategy_settings('news', enabled=True)
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info("NewsStrategy: цикл аналізу запущено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, True)

    async def stop(self):
        if self._analysis_task:
            self._analysis_task.cancel()
        await super().stop()
        save_strategy_settings('news', enabled=False)
        logger.info("NewsStrategy: зупинено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, False)

    async def _analysis_loop(self):
        while self.enabled:
            try:
                await self.analyze()
            except Exception as e:
                logger.error(f"Помилка аналізу новин: {e}")
            await asyncio.sleep(60)

    def _load_history(self):
        with get_db() as conn:
            # Завантажуємо баланс
            bal = conn.execute(
                "SELECT amount FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                (self.strategy_id,)
            ).fetchone()

            if bal:
                self.balance = bal['amount']
            else:
                self.balance = 100.0
                self._save_balance()

            # Відновлюємо час останнього оновлення новин
            last = conn.execute(
                "SELECT value FROM system_settings WHERE key = 'news_last_update'"
            ).fetchone()
            if last and last['value']:
                try:
                    self.last_update = datetime.fromisoformat(last['value'])
                    logger.info(f"[News] Відновлено час останнього оновлення: {self.last_update}")
                except Exception as e:
                    logger.error(f"[News] Помилка відновлення часу оновлення: {e}")
                    self.last_update = None
            else:
                self.last_update = None

            # Завантажуємо статистику угод
            stats = conn.execute(
                "SELECT SUM(pnl) as pnl, COUNT(*) as cnt FROM orders WHERE strategy_id=? AND status='closed'",
                (self.strategy_id,)
            ).fetchone()

            if stats and stats['pnl']:
                self.total_pnl = stats['pnl']
                self.total_trades = stats['cnt']

            # Завантажуємо кількість прибуткових угод
            win = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE strategy_id=? AND status='closed' AND pnl>0",
                (self.strategy_id,)
            ).fetchone()
            self.winning_trades = win[0] if win else 0

            # Розраховуємо збиткові угоди
            self.losing_trades = self.total_trades - self.winning_trades

            # Оновлюємо денний початковий баланс для drawdown (якщо потрібно)
            if self.daily_start_balance == 0:
                self.daily_start_balance = self.balance
                self.daily_lowest_balance = self.balance

            logger.info(
                f"[News] Завантажено історію: баланс=${self.balance}, PnL=${self.total_pnl}, угод={self.total_trades}")

    def _save_balance(self):
        with get_db() as conn:
            conn.execute("DELETE FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                         (self.strategy_id,))
            conn.execute("INSERT INTO balances (strategy_id, asset, amount, mode, updated_at) VALUES (?,?,?,?,?)",
                         (self.strategy_id, 'USDT', self.balance, self.mode, datetime.now().isoformat()))

    def _save_order(self, order_id: str, symbol: str, side: str, price: float, quantity: float, status: str):
        with get_db() as conn:
            conn.execute("""
                INSERT INTO orders 
                (order_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, self.strategy_id, symbol, side, price, quantity, status, 'Market',
                  datetime.now().isoformat()))

    async def fetch_news(self) -> List[dict]:
        if not self.news_api_key:
            return []

        from datetime import datetime, timedelta

        query = ' OR '.join(f'"{s}" cryptocurrency' for s in self.symbols)
        params = {
            'q': query,
            'language': 'en',
            'sortBy': 'publishedAt',
            'pageSize': 20,
            'apiKey': self.news_api_key
        }

        try:
            resp = requests.get("https://newsapi.org/v2/everything", params=params, timeout=15)

            # Обробка помилки 429 (Too Many Requests)
            if resp.status_code == 429:
                logger.warning("NewsAPI ліміт вичерпано (429). Наступна спроба через 1 годину")
                add_log("WARNING", self.name, "NewsAPI ліміт вичерпано")
                self._last_error_time = datetime.now()
                return []  # Повертаємо пустий список

            if resp.status_code == 200:
                data = resp.json()
                articles = data.get('articles', [])

                # Скидаємо помилку якщо запит успішний
                self._last_error_time = None

                # Фільтруємо новини за останні 2 години
                cutoff_time = datetime.now() - timedelta(hours=2)
                new_articles = []

                for article in articles:
                    published_at = article.get('publishedAt', '')
                    if published_at:
                        try:
                            pub_time = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
                            if pub_time > cutoff_time:
                                new_articles.append(article)
                        except:
                            new_articles.append(article)

                self.articles_count = len(new_articles)
                logger.info(f"Отримано {len(articles)} новин, за останні 2 години: {len(new_articles)}")
                return new_articles
            else:
                logger.error(f"NewsAPI помилка {resp.status_code}")
                return []

        except Exception as e:
            logger.error(f"Помилка запиту: {e}")
            return []

    def analyze_sentiment(self, articles: List[dict]) -> dict:
        pos_kw = ['surge', 'rally', 'gain', 'positive', 'bullish', 'record', 'high', 'upgrade', 'approve', 'adoption',
                  'breakthrough', 'soar', 'pump', 'moon', 'green', 'up', 'growth']
        neg_kw = ['drop', 'crash', 'fall', 'negative', 'bearish', 'low', 'decline', 'hack', 'ban', 'scandal', 'fraud',
                  'crackdown', 'dump', 'red', 'sell', 'panic', 'fud', 'down']
        pos = neg = neu = 0
        for a in articles[:20]:
            text = (a.get('title', '') + ' ' + (a.get('description', '') or '')).lower()
            p = sum(1 for kw in pos_kw if kw in text)
            n = sum(1 for kw in neg_kw if kw in text)
            if p > n:
                pos += 1
            elif n > p:
                neg += 1
            else:
                neu += 1
        total = pos + neg + neu
        if total == 0:
            return {'overall': 'neutral', 'positive': 0, 'neutral': 0, 'negative': 0}
        if pos > neg + 2:
            overall = 'positive'
        elif neg > pos + 2:
            overall = 'negative'
        else:
            overall = 'neutral'
        return {'overall': overall, 'positive': pos, 'neutral': neu, 'negative': neg}

    async def analyze(self):
        if not self.enabled:
            return {'action': 'hold'}

        if not self.can_trade():
            add_log("WARNING", self.name, f"Торгівля заблокована: {self._block_reason}")
            return {'action': 'hold', 'blocked': True, 'reason': self._block_reason}

        # Логуємо поточний стан
        add_log("DEBUG", self.name,
                f"Аналіз новин: enabled={self.enabled}, balance=${self.balance}, open_positions={len(self.open_positions)}")

        need_update = (self.last_update is None or
                       (datetime.now() - self.last_update).seconds > self.interval_minutes * 60)

        if need_update:
            add_log("INFO", self.name, "Початок оновлення новин")
            logger.info("Оновлення новин...")

            articles = await self.fetch_news()

            if articles is None or len(articles) == 0:
                add_log("WARNING", self.name, "Новин не отримано")
                return {'action': 'hold', 'sentiment': self.current_sentiment}

            self.last_news = articles
            self.last_update = datetime.now()

            # Зберігаємо час оновлення в БД
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                    ('news_last_update', self.last_update.isoformat())
                )

            add_log("INFO", self.name, f"Отримано {len(articles)} новин")

            if articles:
                sent = self.analyze_sentiment(articles)
                self.current_sentiment = sent['overall']

                # Логуємо результат аналізу
                add_log("INFO", self.name,
                        f"Сентимент: {self.current_sentiment} (поз:{sent['positive']}, нег:{sent['negative']}, нейтр:{sent['neutral']})")

                # Зберігаємо сентимент
                from database.db import save_sentiment_history
                save_sentiment_history(
                    overall=sent['overall'],
                    positive=sent['positive'],
                    neutral=sent['neutral'],
                    negative=sent['negative'],
                    articles_count=len(articles)
                )

                # Генеруємо сигнал
                signal = self._generate_signal(sent)
                add_log("INFO", self.name, f"Згенеровано сигнал: {signal}")

                # ВИКОНУЄМО СИГНАЛ (раніше цього не було!)
                if signal.get('action') == 'buy':
                    add_log("INFO", self.name, "Виконуємо сигнал КУПІВЛІ")
                    await self._execute_buy_signal()
                elif signal.get('action') == 'sell':
                    add_log("INFO", self.name, "Виконуємо сигнал ПРОДАЖУ")
                    await self._execute_sell_signal()

                return signal

        return {'action': 'hold', 'sentiment': self.current_sentiment}

    def _generate_signal(self, sent):
        mult = {'low': 2.0, 'medium': 1.5, 'high': 1.0}[self.sensitivity]
        thresh = int(4 * mult)

        # СИГНАЛ НА ПОКУПКУ - завжди можна (якщо немає відкритої позиції)
        if sent['positive'] > sent['negative'] + thresh and sent['positive'] >= 3:
            if self.open_positions:
                logger.info("[News] Вже є відкрита позиція, новий BUY сигнал ігнорується")
                return {'action': 'hold', 'reason': 'already_in_position'}

            if self.telegram_bot:
                asyncio.create_task(
                    self.telegram_bot.send_notification(
                        f"📰 *НОВИННИЙ СИГНАЛ*\n"
                        f"└ Дія: `КУПІВЛЯ`\n"
                        f"└ Причина: позитивні новини\n"
                        f"└ Позитивних: {sent['positive']}, Негативних: {sent['negative']}",
                        parse_mode='Markdown'
                    )
                )
            self.increment_daily_trades()
            asyncio.create_task(self._execute_buy_signal())
            return {'action': 'buy', 'reason': 'positive_news'}

        # СИГНАЛ НА ПРОДАЖ - ТІЛЬКИ ЯКЩО Є ВІДКРИТА ПОЗИЦІЯ
        elif sent['negative'] > sent['positive'] + thresh and sent['negative'] >= 3:
            if not self.open_positions:
                logger.info("[News] Немає відкритих позицій, SELL сигнал ігнорується")
                return {'action': 'hold', 'reason': 'no_position_to_sell'}

            if self.telegram_bot:
                asyncio.create_task(
                    self.telegram_bot.send_notification(
                        f"📰 *НОВИННИЙ СИГНАЛ*\n"
                        f"└ Дія: `ПРОДАЖ`\n"
                        f"└ Причина: негативні новини\n"
                        f"└ Позитивних: {sent['positive']}, Негативних: {sent['negative']}",
                        parse_mode='Markdown'
                    )
                )
            self.increment_daily_trades()
            asyncio.create_task(self._execute_sell_signal())
            return {'action': 'sell', 'reason': 'negative_news'}

        return {'action': 'hold'}

    async def _execute_buy_signal(self):
        """Виконання сигналу на покупку"""
        if not self.can_trade(self.trade_size_usdt):
            logger.warning(f"[News] Торгівля заблокована: {self._block_reason}")
            return

        symbol = self.symbols[0] + 'USDT'
        price = await self.exchange.get_current_price(symbol)

        if price > 0:
            await self._open_position(symbol, 'buy', price)

    async def _execute_sell_signal(self):
        """Виконання сигналу на продаж"""
        # ПЕРЕВІРКА: чи є відкриті позиції?
        if not self.open_positions:
            logger.info("[News] Немає відкритих позицій для продажу")
            return

        # ПЕРЕВІРКА: чи можна торгувати
        if not self.can_trade(self.trade_size_usdt):
            logger.warning(f"[News] Торгівля заблокована: {self._block_reason}")
            return

        # Закриваємо ВСІ відкриті позиції
        for symbol in list(self.open_positions.keys()):
            price = await self.exchange.get_current_price(symbol)
            if price > 0:
                await self._close_position(symbol, price)

    async def _open_position(self, symbol: str, side: str, price: float):
        quantity = self.trade_size_usdt / price
        cost = quantity * price

        if self.available_balance < cost:
            logger.warning(f"[News] Недостатньо балансу: потрібно ${cost:.2f}")
            return

        order_id = f"news_{symbol}_{int(datetime.now().timestamp())}_{self.strategy_id}"
        result = await self.exchange.create_order(symbol, side, 'Market', quantity, price)

        if result.get('error'):
            logger.error(f"Помилка відкриття позиції {symbol}: {result}")
            return

        self.open_positions[symbol] = {
            'order_id': order_id,
            'entry_price': price,
            'quantity': quantity,
            'side': side,
            'opened_at': datetime.now()
        }
        self.locked_balance += cost
        self._save_order(order_id, symbol, side, price, quantity, 'open')

        add_log("INFO", self.name, f"📈 Відкрито позицію {symbol} @ ${price:.2f} (новинний сигнал)")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📰 *НОВИННИЙ СИГНАЛ - ВІДКРИТО ПОЗИЦІЮ*\n"
                f"└ Символ: {symbol}\n"
                f"└ Сторона: {side.upper()}\n"
                f"└ Ціна: ${price:.2f}",
                parse_mode='Markdown'
            )

    async def _close_position(self, symbol: str, price: float):
        position = self.open_positions.get(symbol)
        if not position:
            return

        revenue = position['quantity'] * price
        cost = position['quantity'] * position['entry_price']
        commission = revenue * 0.0018 + cost * 0.0018
        pnl = revenue - cost - commission

        result = await self.exchange.create_order(symbol, 'sell', 'Market', position['quantity'], price)

        if result.get('error'):
            logger.error(f"Помилка закриття позиції {symbol}: {result}")
            return

        self.balance += revenue - commission
        self.locked_balance -= cost
        self.total_pnl += pnl
        self.total_trades += 1

        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        with get_db() as conn:
            conn.execute(
                "UPDATE orders SET status = 'closed', closed_at = ?, closed_price = ?, pnl = ?, commission = ? WHERE order_id = ?",
                (datetime.now().isoformat(), price, pnl, commission, position['order_id'])
            )

        del self.open_positions[symbol]

        add_log("INFO", self.name, f"📉 Закрито позицію {symbol} @ ${price:.2f} | PnL: ${pnl:.2f}")

        if self.telegram_bot:
            pnl_icon = "✅" if pnl >= 0 else "❌"
            await self.telegram_bot.send_notification(
                f"📰 *НОВИННИЙ СИГНАЛ - ЗАКРИТО ПОЗИЦІЮ*\n"
                f"└ Символ: {symbol}\n"
                f"└ PnL: {pnl_icon} ${pnl:.2f}",
                parse_mode='Markdown'
            )

    async def execute(self, signal):
        if signal.get('action') == 'buy':
            add_log("INFO", self.name, "Сигнал КУПІВЛІ (позитивні новини)")
        elif signal.get('action') == 'sell':
            add_log("INFO", self.name, "Сигнал ПРОДАЖУ (негативні новини)")

    async def update_settings(self, symbols=None, interval_minutes=None,
                              sensitivity=None, trade_size_usdt=None):
        if symbols is not None:
            self.symbols = symbols
        if interval_minutes is not None:
            self.interval_minutes = interval_minutes
        if sensitivity is not None:
            self.sensitivity = sensitivity
        if trade_size_usdt is not None:
            self.trade_size_usdt = trade_size_usdt

        save_strategy_settings('news',
                               symbols=self.symbols,
                               interval_minutes=self.interval_minutes,
                               sensitivity=self.sensitivity,
                               trade_size_usdt=self.trade_size_usdt)

        add_log("INFO", self.name,
                f"Оновлено налаштування: {self.symbols}, інтервал={self.interval_minutes}, чутливість={self.sensitivity}, розмір=${self.trade_size_usdt}")
        return True

    async def get_status(self):
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades else 0
        return {
            'id': self.strategy_id, 'name': self.name, 'enabled': self.enabled, 'mode': self.mode,
            'balance': round(self.balance, 2), 'locked_balance': round(self.locked_balance, 2),
            'available_balance': round(self.available_balance, 2),
            'total_pnl': round(self.total_pnl, 2), 'total_trades': self.total_trades,
            'winning_trades': self.winning_trades, 'win_rate': round(win_rate, 1),
            'current_sentiment': self.current_sentiment, 'symbols': self.symbols,
            'interval_minutes': self.interval_minutes, 'sensitivity': self.sensitivity,
            'trade_size_usdt': self.trade_size_usdt,
            'last_news_count': self.articles_count,
            'api_key_configured': bool(self.news_api_key),
            'sentiment_history': self.sentiment_history,
            'daily_trades_count': self.daily_trades_count,
            'max_daily_trades': self.max_daily_trades,
            'is_blocked': self._is_blocked,
            'block_reason': self._block_reason
        }

    async def reset(self):
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.last_news = []
        self.last_update = None
        self.articles_count = 0
        self.sentiment_history = []
        self.open_positions = {}
        with get_db() as conn:
            conn.execute("DELETE FROM orders WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM balances WHERE strategy_id=?", (self.strategy_id,))
        self._save_balance()
        await self.reset_limits()
        add_log("INFO", self.name, "Стратегію скинуто")

    async def emergency_stop(self):
        for symbol in list(self.open_positions.keys()):
            price = await self.exchange.get_current_price(symbol)
            if price > 0:
                await self._close_position(symbol, price)
        await self.stop()