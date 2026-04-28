# migrate_orders.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_bot.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Перевіряємо існуючі колонки
cursor.execute("PRAGMA table_info(orders)")
columns = [col[1] for col in cursor.fetchall()]

# Додаємо відсутні колонки
if 'stop_loss' not in columns:
    cursor.execute("ALTER TABLE orders ADD COLUMN stop_loss REAL")
    print("✅ Додано колонку stop_loss")

if 'take_profits' not in columns:
    cursor.execute("ALTER TABLE orders ADD COLUMN take_profits TEXT")
    print("✅ Додано колонку take_profits")

if 'signal_type' not in columns:
    cursor.execute("ALTER TABLE orders ADD COLUMN signal_type TEXT")
    print("✅ Додано колонку signal_type")

if 'partial_closes' not in columns:
    cursor.execute("ALTER TABLE orders ADD COLUMN partial_closes TEXT")
    print("✅ Додано колонку partial_closes")

conn.commit()
conn.close()
print("✅ Міграцію завершено")