# add_signals_to_db.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_bot.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Перевіряємо чи є стратегія
cursor.execute("SELECT id, name FROM strategies")
existing = cursor.fetchall()
print("Існуючі стратегії:", existing)

# Додаємо signals якщо немає
cursor.execute("""
    INSERT OR IGNORE INTO strategies (name, enabled, mode, drawdown_limit) 
    VALUES ('signals', 1, 'simulation', 10)
""")

# Перевіряємо результат
cursor.execute("SELECT id, name, enabled FROM strategies")
strategies = cursor.fetchall()
print("\n📋 Стратегії після додавання:")
for s in strategies:
    print(f"  ID: {s[0]}, Name: {s[1]}, Enabled: {s[2]}")

conn.commit()
conn.close()
print("\n✅ Готово! Перезапустіть бота.")