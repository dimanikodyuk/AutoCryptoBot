# fix_power_db.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "power_monitor.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Перевіряємо існуючі колонки в power_sessions
cursor.execute("PRAGMA table_info(power_sessions)")
columns = [col[1] for col in cursor.fetchall()]
print("Існуючі колонки в power_sessions:", columns)

# Додаємо відсутні колонки
if 'total_uptime_seconds' not in columns:
    cursor.execute("ALTER TABLE power_sessions ADD COLUMN total_uptime_seconds INTEGER DEFAULT 0")
    print("✅ Додано колонку total_uptime_seconds")

if 'avg_power_watts' not in columns:
    cursor.execute("ALTER TABLE power_sessions ADD COLUMN avg_power_watts REAL DEFAULT 0")
    print("✅ Додано колонку avg_power_watts")

if 'total_energy_kwh' not in columns:
    cursor.execute("ALTER TABLE power_sessions ADD COLUMN total_energy_kwh REAL DEFAULT 0")
    print("✅ Додано колонку total_energy_kwh")

if 'total_cost_uah' not in columns:
    cursor.execute("ALTER TABLE power_sessions ADD COLUMN total_cost_uah REAL DEFAULT 0")
    print("✅ Додано колонку total_cost_uah")

# Перевіряємо колонки в power_hourly
cursor.execute("PRAGMA table_info(power_hourly)")
hourly_columns = [col[1] for col in cursor.fetchall()]
print("\nІснуючі колонки в power_hourly:", hourly_columns)

if 'energy_kwh' not in hourly_columns:
    cursor.execute("ALTER TABLE power_hourly ADD COLUMN energy_kwh REAL DEFAULT 0")
    print("✅ Додано колонку energy_kwh в power_hourly")

if 'cost_uah' not in hourly_columns:
    cursor.execute("ALTER TABLE power_hourly ADD COLUMN cost_uah REAL DEFAULT 0")
    print("✅ Додано колонку cost_uah в power_hourly")

conn.commit()
conn.close()

print("\n✅ Міграцію БД завершено")