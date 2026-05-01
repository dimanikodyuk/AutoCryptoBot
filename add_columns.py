from database.db import get_db

with get_db() as conn:
    conn.execute('''
    CREATE TABLE IF NOT EXISTS forecasts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy TEXT NOT NULL,
        symbol TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        entry_price REAL NOT NULL,
        target_price REAL NOT NULL,
        confidence REAL NOT NULL,
        explanation TEXT,
        status TEXT DEFAULT 'active',
        success INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        resolved_at TIMESTAMP,
        resolved_price REAL
    )
    ''')
    print("✅ Таблицю forecasts створено")