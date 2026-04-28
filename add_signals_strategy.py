# add_signals_table.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_bot.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Створюємо таблицю signals
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
        order_id TEXT,
        FOREIGN KEY (strategy_id) REFERENCES strategies(id)
    )
''')

# Додаємо індекси
cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)')

print("✅ Таблицю signals створено")

# Перевіряємо
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals'")
if cursor.fetchone():
    print("✅ Таблиця signals існує")
else:
    print("❌ Помилка: таблиця не створена")

conn.commit()
conn.close()