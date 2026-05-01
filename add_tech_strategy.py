import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_bot.db"


def add_tech_analysis_strategy():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Перевіряємо чи існує стратегія
        cursor.execute("SELECT id FROM strategies WHERE name = 'tech_analysis'")
        exists = cursor.fetchone()

        if exists:
            print("✅ Стратегія tech_analysis вже існує")
        else:
            # Додаємо стратегію
            cursor.execute("""
                INSERT INTO strategies (id, name, enabled, mode, drawdown_limit)
                VALUES (5, 'tech_analysis', 0, 'simulation', 10.0)
            """)
            conn.commit()
            print("✅ Стратегію tech_analysis додано до БД")

    except Exception as e:
        print(f"❌ Помилка: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    add_tech_analysis_strategy()