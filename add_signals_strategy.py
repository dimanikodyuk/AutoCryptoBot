import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_bot.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Встановлюємо DEBUG для всіх модулів
modules = ['main', 'engine', 'exchange', 'grid', 'grid_manager', 'scalp', 'news', 'web', 'telegram', 'database', 'backtest', 'signals']

for module in modules:
    cursor.execute("""
        INSERT OR REPLACE INTO log_settings (module, log_level, updated_at)
        VALUES (?, 'DEBUG', CURRENT_TIMESTAMP)
    """, (module,))
    print(f"✅ {module}: DEBUG")

conn.commit()
conn.close()
print("\n✅ Всі модулі переведено в режим DEBUG")
print("Перезапустіть бота")