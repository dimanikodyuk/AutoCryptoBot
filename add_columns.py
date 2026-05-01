import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('trading_bot.db')
cursor = conn.cursor()

# Отримуємо ID стратегії
cursor.execute("SELECT id FROM strategies WHERE name = 'tech_analysis'")
result = cursor.fetchone()

if result:
    strategy_id = result[0]

    # Поточна ціна BTC ~ 77300
    current_price = 77300
    entry_price = current_price - 200  # Трохи нижче поточної ціни

    order_id = f"ta_test_{int(datetime.now().timestamp())}"
    opened_at = (datetime.now() - timedelta(minutes=30)).isoformat()

    cursor.execute("""
        INSERT INTO orders 
        (order_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at, signal_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_id, strategy_id, 'BTCUSDT', 'buy', entry_price, 0.0013, 'open', 'Market', opened_at, 'TEST'))

    conn.commit()
    print(f"✅ Створено тестову угоду: {order_id}")
    print(f"   Ціна входу: ${entry_price}")
    print(f"   Час відкриття: {opened_at}")
else:
    print("❌ Стратегію не знайдено")

conn.close()