import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)


class Config:
    """Конфігурація бота"""

    # Bybit
    BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
    BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')

    # NewsAPI
    NEWS_API_KEY = os.getenv('NEWS_API_KEY', '')

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # Flask
    FLASK_SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')

    # Режим роботи
    DEFAULT_MODE = os.getenv('DEFAULT_MODE', 'simulation')

    # Комісії Bybit Spot
    COMMISSION_MAKER = 0.001
    COMMISSION_TAKER = 0.0018

    # Торгові пари
    SYMBOLS = ['BTCUSDT', 'SOLUSDT']

    # WebSocket URL
    WS_URL = "wss://stream.bybit.com/v5/public/spot"

    # API URLs
    BYBIT_REST_URL = "https://api.bybit.com"

    # ============= ПАРАМЕТРИ БЕЗПЕКИ =============

    # Максимальний відсоток балансу на одну угоду
    MAX_ORDER_PERCENT = 20  # 20%

    # Мінімальний баланс для торгівлі (USDT)
    MIN_BALANCE_FOR_TRADING = 10

    # Максимальна кількість угод на день
    MAX_DAILY_TRADES = 50

    # Максимальний денний drawdown (%)
    MAX_DAILY_DRAWDOWN = 10