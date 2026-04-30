from database.db import get_db

with get_db() as conn:
    # Перевіряємо чи є стратегія
    existing = conn.execute("SELECT id FROM strategies WHERE name = 'tech_analysis'").fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO strategies (id, name, enabled, mode, drawdown_limit) 
            VALUES (5, 'tech_analysis', 0, 'simulation', 10.0)
        """)
        print("✅ Стратегію tech_analysis додано до БД")
    else:
        print(f"ℹ️ Стратегія tech_analysis вже існує (id={existing['id']})")