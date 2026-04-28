import sqlite3
from contextlib import contextmanager
from pathlib import Path
from utils.logger_utils import setup_logger
from datetime import datetime, timezone, timedelta

DB_PATH = Path(__file__).parent.parent / "trading_bot.db"

logger = setup_logger('database')

# Отримуємо локальний час (Київ UTC+3)
KYIV_TZ = timezone(timedelta(hours=3))



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
        # Таблиця історії сентименту новин
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS news_sentiment_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        overall TEXT,
                        positive INTEGER,
                        neutral INTEGER,
                        negative INTEGER,
                        articles_count INTEGER
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

        # Таблиця логів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level TEXT,
                module TEXT,
                message TEXT
            )
        ''')

        # Таблиця налаштувань логування
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS log_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT UNIQUE NOT NULL,
                log_level TEXT DEFAULT 'INFO',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблиця системних налаштувань
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблиця price_history
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                time_iso TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
        ''')

        # Індекси для price_history
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_price_history_order_id ON price_history(order_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_price_history_symbol ON price_history(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_price_history_created_at ON price_history(created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_price_history_timestamp ON price_history(timestamp)')

        # Індекси для логів
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_module ON logs(module)')

        # Міграція: додаємо closed_price
        cursor.execute("PRAGMA table_info(orders)")
        order_columns = [col[1] for col in cursor.fetchall()]
        if 'closed_price' not in order_columns:
            print("Додаємо колонку closed_price до таблиці orders...")
            cursor.execute("ALTER TABLE orders ADD COLUMN closed_price REAL")

        # Таблиця балансів
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

        cursor.execute("PRAGMA table_info(balances)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'symbol' not in columns:
            print("Додаємо колонку symbol до таблиці balances...")
            cursor.execute("ALTER TABLE balances ADD COLUMN symbol TEXT")

        # Таблиця сигналів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                strategy_id INTEGER,
                symbol TEXT,
                signal_type TEXT,
                entry_price REAL,
                entry_limit REAL,
                stop_loss REAL,
                take_profits TEXT,
                trade_size_usdt REAL,
                status TEXT,
                created_at TIMESTAMP,
                closed_at TIMESTAMP,
                total_pnl REAL DEFAULT 0,
                FOREIGN KEY (strategy_id) REFERENCES strategies(id)
            )
        ''')

        # Індекси для signals
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)')

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
            strategies = ['grid', 'news', 'scalp', 'signals']
            for s in strategies:
                cursor.execute(
                    "INSERT INTO strategies (name, enabled, mode) VALUES (?, ?, ?)",
                    (s, 0, 'simulation')
                )

        # Додавання налаштувань логів за замовчуванням
        default_modules = ['main', 'engine', 'exchange', 'grid', 'grid_manager', 'scalp', 'news', 'web', 'telegram',
                           'database', 'backtest']
        for module in default_modules:
            cursor.execute(
                "INSERT OR IGNORE INTO log_settings (module, log_level) VALUES (?, ?)",
                (module, 'INFO')
            )

        # Додавання системних налаштувань
        cursor.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            ('log_retention_days', '7')
        )

        # Індекси для orders
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_pair ON orders(pair_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol)')

        print("База даних успішно ініціалізована")

def get_local_now():
    """Повертає поточний час у часовій зоні Києва"""
    return datetime.now(KYIV_TZ).replace(tzinfo=None)

def get_local_now_str():
    """Повертає поточний час у форматі 'YYYY-MM-DD HH:MM:SS'"""
    return datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S')


def add_log(level: str, module: str, message: str):
    """Додати лог в БД з локальним часом - тепер зберігає ВСІ рівні"""
    try:
        # Отримуємо налаштований рівень для модуля
        log_level_config = get_log_settings().get(module, 'INFO')

        # Рівні пріоритету
        levels = {'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3}
        current_priority = levels.get(level, 1)
        config_priority = levels.get(log_level_config, 1)

        # Пропускаємо якщо рівень нижче налаштованого
        if current_priority < config_priority:
            return

        with get_db() as conn:
            conn.execute(
                "INSERT INTO logs (timestamp, level, module, message) VALUES (?, ?, ?, ?)",
                (get_local_now_str(), level, module, message)
            )
    except Exception as e:
        print(f"Помилка логування: {e}")


# ============= ФУНКЦІЇ ДЛЯ ЛОГІВ =============

def get_logs(module: str = None, level: str = None, limit: int = 100, offset: int = 0) -> list:
    """Отримання логів з фільтрами"""
    with get_db() as conn:
        query = "SELECT * FROM logs WHERE 1=1"
        params = []

        if module and module != 'all':
            query += " AND module = ?"
            params.append(module)

        if level and level != 'all':
            query += " AND level = ?"
            params.append(level)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_logs_count(module: str = None, level: str = None) -> int:
    """Отримання кількості логів"""
    with get_db() as conn:
        query = "SELECT COUNT(*) as count FROM logs WHERE 1=1"
        params = []

        if module and module != 'all':
            query += " AND module = ?"
            params.append(module)

        if level and level != 'all':
            query += " AND level = ?"
            params.append(level)

        cursor = conn.execute(query, params)
        return cursor.fetchone()['count']


def cleanup_old_logs(days: int = 7) -> int:
    """Видалення старих логів (старше days днів)"""
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM logs WHERE timestamp < datetime('now', '-' || ? || ' days')",
            (days,)
        )
        deleted = result.rowcount
        if deleted > 0:
            logger.info(f"Видалено {deleted} старих логів (старше {days} днів)")
        return deleted


def get_log_settings() -> dict:
    """Отримання налаштувань логування для всіх модулів"""
    with get_db() as conn:
        cursor = conn.execute("SELECT module, log_level FROM log_settings")
        rows = cursor.fetchall()
        return {row['module']: row['log_level'] for row in rows}


def update_log_settings(module: str, level: str) -> bool:
    """Оновлення налаштувань логування для модуля"""
    if level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
        return False

    with get_db() as conn:
        conn.execute(
            "UPDATE log_settings SET log_level = ?, updated_at = CURRENT_TIMESTAMP WHERE module = ?",
            (level, module)
        )
        return True


def get_log_retention_days() -> int:
    """Отримання кількості днів зберігання логів"""
    with get_db() as conn:
        cursor = conn.execute("SELECT value FROM system_settings WHERE key = 'log_retention_days'")
        row = cursor.fetchone()
        if row:
            return int(row['value'])
        return 7


def set_log_retention_days(days: int) -> bool:
    """Встановлення кількості днів зберігання логів"""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            ('log_retention_days', str(days))
        )
        return True


# ============= ФУНКЦІЇ ДЛЯ PRICE_HISTORY =============

def save_price_history(order_id: str, symbol: str, klines: list):
    """Збереження історії цін для угоди"""
    if not klines:
        return

    with get_db() as conn:
        for k in klines:
            conn.execute("""
                INSERT OR REPLACE INTO price_history 
                (order_id, symbol, timestamp, time_iso, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id, symbol, k['timestamp'], k.get('time_iso', ''),
                k['open'], k['high'], k['low'], k['close'], k['volume']
            ))
        logger.info(f"Збережено {len(klines)} свічок для угоди {order_id}")


def get_price_history(order_id: str) -> list:
    """Отримання збереженої історії цін для угоди"""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT * FROM price_history WHERE order_id = ? ORDER BY timestamp",
            (order_id,)
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def save_sentiment_history(overall: str, positive: int, neutral: int, negative: int, articles_count: int):
    """Збереження історії сентименту"""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO news_sentiment_history (overall, positive, neutral, negative, articles_count)
            VALUES (?, ?, ?, ?, ?)
        """, (overall, positive, neutral, negative, articles_count))

def get_sentiment_history(limit: int = 50) -> list:
    """Отримання історії сентименту"""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT * FROM news_sentiment_history 
            ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def cleanup_old_price_history(max_days: int = 3):
    """Видалення старих записів price_history (старше max_days днів)"""
    with get_db() as conn:
        result = conn.execute("""
            DELETE FROM price_history 
            WHERE created_at < datetime('now', '-' || ? || ' days')
        """, (max_days,))
        deleted = result.rowcount
        if deleted > 0:
            logger.info(f"Видалено {deleted} старих записів price_history (старше {max_days} днів)")
        return deleted