import sqlite3
from contextlib import contextmanager
from pathlib import Path
from utils.logger_utils import setup_logger
# Шлях до БД в корені проєкту
DB_PATH = Path(__file__).parent.parent / "trading_bot.db"

logger = setup_logger('database')

@contextmanager
def get_db():
    """Отримати з'єднання з БД"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Ініціалізація всіх таблиць з міграцією"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Таблиця стратегій
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                enabled INTEGER DEFAULT 0,
                mode TEXT DEFAULT 'simulation',
                drawdown_limit REAL DEFAULT 0.1,
                config TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблиця ордерів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE NOT NULL,
                pair_id TEXT,
                strategy_id INTEGER,
                symbol TEXT,
                side TEXT,
                price REAL,
                quantity REAL,
                status TEXT,
                order_type TEXT,
                pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                closed_price REAL,
                FOREIGN KEY (strategy_id) REFERENCES strategies(id)
            )
        ''')

        # Міграція: додаємо closed_price якщо колонки немає
        cursor.execute("PRAGMA table_info(orders)")
        order_columns = [col[1] for col in cursor.fetchall()]
        if 'closed_price' not in order_columns:
            print("Додаємо колонку closed_price до таблиці orders...")
            cursor.execute("ALTER TABLE orders ADD COLUMN closed_price REAL")

        # Таблиця балансів - ДОДАЄМО КОЛОНКУ symbol
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id INTEGER,
                asset TEXT,
                symbol TEXT,
                amount REAL,
                mode TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (strategy_id) REFERENCES strategies(id)
            )
        ''')

        # Міграція для існуючої таблиці balances (додаємо колонку symbol якщо немає)
        cursor.execute("PRAGMA table_info(balances)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'symbol' not in columns:
            print("Додаємо колонку symbol до таблиці balances...")
            cursor.execute("ALTER TABLE balances ADD COLUMN symbol TEXT")

        # Таблиця логів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level TEXT,
                strategy TEXT,
                message TEXT
            )
        ''')

        # Таблиця моніторингу
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_monitor (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                cpu_usage REAL,
                ram_usage REAL,
                ram_total REAL,
                disk_usage REAL,
                temperature REAL,
                uptime_seconds INTEGER
            )
        ''')

        # Додавання стратегій за замовчуванням
        cursor.execute("SELECT COUNT(*) FROM strategies")
        if cursor.fetchone()[0] == 0:
            strategies = ['grid', 'news', 'scalp']
            for s in strategies:
                cursor.execute(
                    "INSERT INTO strategies (name, enabled, mode) VALUES (?, ?, ?)",
                    (s, 0, 'simulation')
                )

        # Індекси
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_pair ON orders(pair_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)')

        print("База даних успішно ініціалізована")


def add_log(level: str, strategy: str, message: str):
    """Додати лог в БД"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO logs (level, strategy, message) VALUES (?, ?, ?)",
            (level, strategy, message)
        )