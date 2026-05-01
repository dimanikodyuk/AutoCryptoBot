import sqlite3
from datetime import datetime

conn = sqlite3.connect('trading_bot.db')
cursor = conn.cursor()

# Видаляємо всі старі свічки
cursor.execute("DELETE FROM price_history")

# Видаляємо всі відкриті угоди тех. аналізу
cursor.execute("""
    DELETE FROM orders 
    WHERE strategy_id = (SELECT id FROM strategies WHERE name = 'tech_analysis') 
    AND status = 'open'
""")

# Створюємо нову угоду з поточною ціною
cursor.execute("SELECT id FROM strategies WHERE name = 'tech_analysis'")
strategy_id = cursor.fetchone()[0]

# Поточна ціна ~ 77300
entry_price = 77300
order_id = f"fresh_{int(datetime.now().timestamp())}"
opened_at = datetime.now().isoformat()

cursor.execute("""
    INSERT INTO orders 
    (order_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at, signal_type)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (order_id, strategy_id, 'BTCUSDT', 'buy', entry_price, 0.0013, 'open', 'Market', opened_at, 'FRESH'))

conn.commit()
print(f"✅ Створено FRESH угоду: {order_id} @ ${entry_price}")
print(f"   Час: {opened_at}")
conn.close()