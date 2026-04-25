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

        self._load_api_key()
        self._load_history()
        logger.info(f"NewsStrategy: {self.symbols}, баланс ${self.balance}")

    @property
    def available_balance(self):
        return self.balance - self.locked_balance

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

    async def stop(self):
        if self._analysis_task:
            self._analysis_task.cancel()
        await super().stop()
        save_strategy_settings('news', enabled=False)
        logger.info("NewsStrategy: зупинено")

    async def _analysis_loop(self):
        while self.enabled:
            try:
                await self.analyze()
            except Exception as e:
                logger.error(f"Помилка аналізу новин: {e}")
            await asyncio.sleep(60)

    def _load_history(self):
        with get_db() as conn:
            bal = conn.execute("SELECT amount FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL", (self.strategy_id,)).fetchone()
            if bal:
                self.balance = bal['amount']
            else:
                self.balance = 100.0
                self._save_balance()
            stats = conn.execute("SELECT SUM(pnl) as pnl, COUNT(*) as cnt FROM orders WHERE strategy_id=? AND status='closed'", (self.strategy_id,)).fetchone()
            if stats and stats['pnl']:
                self.total_pnl = stats['pnl']
                self.total_trades = stats['cnt']
            win = conn.execute("SELECT COUNT(*) FROM orders WHERE strategy_id=? AND status='closed' AND pnl>0", (self.strategy_id,)).fetchone()
            self.winning_trades = win[0] if win else 0

    def _save_balance(self):
        with get_db() as conn:
            conn.execute("DELETE FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL", (self.strategy_id,))
            conn.execute("INSERT INTO balances (strategy_id, asset, amount, mode, updated_at) VALUES (?,?,?,?,?)",
                         (self.strategy_id, 'USDT', self.balance, self.mode, datetime.now().isoformat()))

    async def fetch_news(self) -> List[dict]:
        if not self.news_api_key:
            return []
        query = ' OR '.join(f'"{s}" cryptocurrency' for s in self.symbols)
        params = {'q': query, 'language': 'en', 'sortBy': 'publishedAt', 'pageSize': 20, 'apiKey': self.news_api_key}
        try:
            resp = requests.get("https://newsapi.org/v2/everything", params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                articles = data.get('articles', [])
                self.articles_count = len(articles)
                logger.info(f"Отримано {len(articles)} новин")
                return articles
            else:
                logger.error(f"NewsAPI помилка {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"Помилка запиту: {e}")
            return []

    def analyze_sentiment(self, articles: List[dict]) -> dict:
        pos_kw = ['surge', 'rally', 'gain', 'positive', 'bullish', 'record', 'high', 'upgrade', 'approve', 'adoption', 'breakthrough', 'soar', 'pump', 'moon', 'green', 'up', 'growth']
        neg_kw = ['drop', 'crash', 'fall', 'negative', 'bearish', 'low', 'decline', 'hack', 'ban', 'scandal', 'fraud', 'crackdown', 'dump', 'red', 'sell', 'panic', 'fud', 'down']
        pos = neg = neu = 0
        for a in articles[:20]:
            text = (a.get('title','') + ' ' + (a.get('description','') or '')).lower()
            p = sum(1 for kw in pos_kw if kw in text)
            n = sum(1 for kw in neg_kw if kw in text)
            if p > n: pos += 1
            elif n > p: neg += 1
            else: neu += 1
        total = pos+neg+neu
        if total == 0:
            return {'overall': 'neutral', 'positive':0, 'neutral':0, 'negative':0}
        if pos > neg+2: overall='positive'
        elif neg > pos+2: overall='negative'
        else: overall='neutral'
        return {'overall': overall, 'positive': pos, 'neutral': neu, 'negative': neg}

    async def analyze(self):
        if not self.enabled:
            return {'action':'hold'}
        need_update = (self.last_update is None or (datetime.now()-self.last_update).seconds > self.interval_minutes*60)
        if need_update:
            logger.info("Оновлення новин...")
            articles = await self.fetch_news()
            self.last_news = articles
            self.last_update = datetime.now()
            if articles:
                sent = self.analyze_sentiment(articles)
                self.current_sentiment = sent['overall']
                self.sentiment_history.append({'timestamp': datetime.now().isoformat(), 'overall': self.current_sentiment, **sent})
                if len(self.sentiment_history)>50: self.sentiment_history = self.sentiment_history[-50:]
                add_log("INFO", self.name, f"Сентимент: {self.current_sentiment} (поз:{sent['positive']}, нег:{sent['negative']})")
                signal = self._generate_signal(sent)
                return signal
        return {'action':'hold', 'sentiment':self.current_sentiment}

    def _generate_signal(self, sent):
        mult = {'low':2.0, 'medium':1.5, 'high':1.0}[self.sensitivity]
        thresh = int(4*mult)
        if sent['positive'] > sent['negative'] + thresh and sent['positive'] >= 3:
            return {'action':'buy', 'reason':'positive_news'}
        elif sent['negative'] > sent['positive'] + thresh and sent['negative'] >= 3:
            return {'action':'sell', 'reason':'negative_news'}
        return {'action':'hold'}

    async def execute(self, signal):
        if signal.get('action') == 'buy':
            add_log("INFO", self.name, "Сигнал КУПІВЛІ (позитивні новини)")
        elif signal.get('action') == 'sell':
            add_log("INFO", self.name, "Сигнал ПРОДАЖУ (негативні новини)")

    async def get_status(self):
        win_rate = (self.winning_trades/self.total_trades*100) if self.total_trades else 0
        return {
            'id': self.strategy_id, 'name': self.name, 'enabled': self.enabled, 'mode': self.mode,
            'balance': round(self.balance,2), 'locked_balance': round(self.locked_balance,2),
            'available_balance': round(self.available_balance,2),
            'total_pnl': round(self.total_pnl,2), 'total_trades': self.total_trades,
            'winning_trades': self.winning_trades, 'win_rate': round(win_rate,1),
            'current_sentiment': self.current_sentiment, 'symbols': self.symbols,
            'interval_minutes': self.interval_minutes, 'sensitivity': self.sensitivity,
            'last_news_count': self.articles_count, 'api_key_configured': bool(self.news_api_key),
            'sentiment_history': self.sentiment_history
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
        with get_db() as conn:
            conn.execute("DELETE FROM orders WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM balances WHERE strategy_id=?", (self.strategy_id,))
        self._save_balance()
        add_log("INFO", self.name, "Стратегію скинуто")

    async def emergency_stop(self):
        await self.stop()