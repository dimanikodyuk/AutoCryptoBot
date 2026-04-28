# fix_signals_table.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_bot.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Перевіряємо чи є колонка order_id
cursor.execute("PRAGMA table_info(signals)")
columns = [col[1] for col in cursor.fetchall()]

if 'order_id' not in columns:
    print("Додаємо колонку order_id...")
    cursor.execute("ALTER TABLE signals ADD COLUMN order_id TEXT")
    print("✅ Колонку order_id додано")
else:
    print("✅ Колонка order_id вже існує")

conn.commit()
conn.close()