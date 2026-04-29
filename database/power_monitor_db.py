"""
Окрема база даних для моніторингу електроенергії
Не очищується при скиданні стратегій
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from contextlib import contextmanager

POWER_DB_PATH = Path(__file__).parent.parent / "power_monitor.db"


@contextmanager
def get_power_db():
    """Отримання з'єднання з БД моніторингу"""
    conn = sqlite3.connect(POWER_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_power_db():
    """Ініціалізація таблиць моніторингу"""
    with get_power_db() as conn:
        cursor = conn.cursor()

        # Таблиця для збереження сесій (періодів роботи)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS power_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                total_energy_kwh REAL DEFAULT 0,
                total_cost_uah REAL DEFAULT 0,
                avg_power_watts REAL DEFAULT 0,
                total_uptime_seconds INTEGER DEFAULT 0
            )
        ''')

        # Таблиця для щогодинних записів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS power_hourly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                power_watts REAL NOT NULL,
                energy_kwh REAL NOT NULL,
                cost_uah REAL NOT NULL,
                cpu_percent REAL,
                ram_percent REAL,
                session_id TEXT,
                UNIQUE(timestamp, session_id)
            )
        ''')

        # Таблиця для щоденних агрегатів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS power_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE NOT NULL,
                total_energy_kwh REAL DEFAULT 0,
                total_cost_uah REAL DEFAULT 0,
                avg_power_watts REAL DEFAULT 0,
                max_power_watts REAL DEFAULT 0,
                min_power_watts REAL DEFAULT 0,
                total_hours REAL DEFAULT 0,
                session_ids TEXT DEFAULT ''
            )
        ''')

        # Таблиця для налаштувань
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS power_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Індекси
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hourly_timestamp ON power_hourly(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hourly_session ON power_hourly(session_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sessions_start ON power_sessions(start_time)')

        # Додаємо налаштування за замовчуванням
        default_settings = {
            'base_power_watts': '3.5',
            'max_power_watts': '6.5',
            'psu_efficiency': '0.85',
            'cable_loss': '0.03',
            'electricity_price': '4.32',
            'last_session_id': ''
        }

        for key, value in default_settings.items():
            cursor.execute('''
                INSERT OR IGNORE INTO power_settings (key, value) VALUES (?, ?)
            ''', (key, value))

        print("✅ Базу даних моніторингу електроенергії ініціалізовано")


def get_current_session() -> Optional[Dict]:
    """Отримання поточної активної сесії"""
    with get_power_db() as conn:
        cursor = conn.execute('''
            SELECT * FROM power_sessions 
            WHERE end_time IS NULL 
            ORDER BY start_time DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        return dict(row) if row else None


def start_new_session(session_id: str) -> bool:
    """Початок нової сесії"""
    with get_power_db() as conn:
        # Закриваємо попередню сесію
        conn.execute('''
            UPDATE power_sessions 
            SET end_time = ?, total_uptime_seconds = 
                (strftime('%s', 'now') - strftime('%s', start_time))
            WHERE end_time IS NULL
        ''', (datetime.now().isoformat(),))

        # Створюємо нову сесію
        conn.execute('''
            INSERT INTO power_sessions (session_id, start_time)
            VALUES (?, ?)
        ''', (session_id, datetime.now().isoformat()))

        # Оновлюємо last_session_id в налаштуваннях
        conn.execute('''
            UPDATE power_settings SET value = ? WHERE key = 'last_session_id'
        ''', (session_id,))

        return True


def end_current_session() -> Optional[Dict]:
    """Завершення поточної сесії"""
    with get_power_db() as conn:
        session = get_current_session()
        if not session:
            return None

        # Розраховуємо підсумки сесії
        uptime_seconds = int((datetime.now() - datetime.fromisoformat(session['start_time'])).total_seconds())

        cursor = conn.execute('''
            SELECT 
                SUM(energy_kwh) as total_energy,
                SUM(cost_uah) as total_cost,
                AVG(power_watts) as avg_power
            FROM power_hourly
            WHERE session_id = ?
        ''', (session['session_id'],))

        stats = cursor.fetchone()

        conn.execute('''
            UPDATE power_sessions 
            SET end_time = ?,
                total_energy_kwh = ?,
                total_cost_uah = ?,
                avg_power_watts = ?,
                total_uptime_seconds = ?
            WHERE session_id = ?
        ''', (
            datetime.now().isoformat(),
            stats['total_energy'] or 0,
            stats['total_cost'] or 0,
            stats['avg_power'] or 0,
            uptime_seconds,
            session['session_id']
        ))

        return dict(
            conn.execute('SELECT * FROM power_sessions WHERE session_id = ?', (session['session_id'],)).fetchone())


def add_power_record(
        timestamp: datetime,
        power_watts: float,
        energy_kwh: float,
        cost_uah: float,
        cpu_percent: float = None,
        ram_percent: float = None,
        session_id: str = None
) -> bool:
    """Додавання запису про споживання"""
    with get_power_db() as conn:
        conn.execute('''
            INSERT OR REPLACE INTO power_hourly 
            (timestamp, power_watts, energy_kwh, cost_uah, cpu_percent, ram_percent, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (timestamp.isoformat(), power_watts, energy_kwh, cost_uah, cpu_percent, ram_percent, session_id))
        return True


def update_daily_aggregates() -> None:
    """Оновлення денних агрегатів"""
    with get_power_db() as conn:
        # Отримуємо дані за останні 30 днів
        cursor = conn.execute('''
            SELECT 
                date(timestamp) as date,
                SUM(energy_kwh) as total_energy,
                SUM(cost_uah) as total_cost,
                AVG(power_watts) as avg_power,
                MAX(power_watts) as max_power,
                MIN(power_watts) as min_power,
                COUNT(*) as hours,
                GROUP_CONCAT(DISTINCT session_id) as sessions
            FROM power_hourly
            WHERE timestamp >= date('now', '-30 days')
            GROUP BY date(timestamp)
        ''')

        for row in cursor.fetchall():
            conn.execute('''
                INSERT OR REPLACE INTO power_daily 
                (date, total_energy_kwh, total_cost_uah, avg_power_watts, 
                 max_power_watts, min_power_watts, total_hours, session_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                row['date'], row['total_energy'] or 0, row['total_cost'] or 0,
                row['avg_power'] or 0, row['max_power'] or 0, row['min_power'] or 0,
                row['hours'] or 0, row['sessions'] or ''
            ))


def get_power_history(days: int = 30) -> List[Dict]:
    """Отримання історії споживання"""
    with get_power_db() as conn:
        # Отримуємо денні агрегати з power_hourly
        cursor = conn.execute('''
            SELECT 
                date(timestamp) as date,
                SUM(energy_kwh) as total_energy_kwh,
                SUM(cost_uah) as total_cost_uah,
                AVG(power_watts) as avg_power_watts,
                MAX(power_watts) as max_power_watts,
                MIN(power_watts) as min_power_watts,
                COUNT(*) as total_hours
            FROM power_hourly
            WHERE timestamp >= date('now', '-' || ? || ' days')
            GROUP BY date(timestamp)
            ORDER BY date DESC
        ''', (days,))

        rows = cursor.fetchall()

        if rows:
            return [dict(row) for row in rows]

        # Якщо немає даних в power_hourly, беремо з power_daily
        cursor = conn.execute('''
            SELECT * FROM power_daily 
            WHERE date >= date('now', '-' || ? || ' days')
            ORDER BY date DESC
        ''', (days,))
        return [dict(row) for row in cursor.fetchall()]


def get_power_summary() -> Dict:
    """Отримання загальної статистики (включаючи поточну сесію)"""
    with get_power_db() as conn:
        # Загальна статистика за весь час (включаючи незавершену сесію)
        total = conn.execute('''
            SELECT 
                SUM(energy_kwh) as total_energy,
                SUM(cost_uah) as total_cost,
                AVG(power_watts) as avg_power,
                COUNT(DISTINCT session_id) as total_sessions,
                SUM(total_uptime_seconds) / 3600.0 as total_hours
            FROM (
                SELECT energy_kwh, cost_uah, power_watts, session_id, total_uptime_seconds FROM power_hourly
                UNION ALL
                SELECT total_energy_kwh, total_cost_uah, avg_power_watts, session_id, total_uptime_seconds FROM power_sessions
            )
        ''').fetchone()

        # Якщо немає даних в power_hourly, беремо з поточної сесії
        if total['total_energy'] is None:
            # Отримуємо поточну сесію
            current = conn.execute('''
                SELECT total_energy_kwh, total_cost_uah, avg_power_watts, total_uptime_seconds
                FROM power_sessions 
                WHERE end_time IS NULL
                ORDER BY start_time DESC LIMIT 1
            ''').fetchone()

            if current:
                total = {
                    'total_energy': current['total_energy_kwh'] or 0,
                    'total_cost': current['total_cost_uah'] or 0,
                    'avg_power': current['avg_power_watts'] or 0,
                    'total_sessions': 1,
                    'total_hours': (current['total_uptime_seconds'] or 0) / 3600.0
                }
            else:
                total = {
                    'total_energy': 0,
                    'total_cost': 0,
                    'avg_power': 0,
                    'total_sessions': 0,
                    'total_hours': 0
                }

        # Статистика за поточний місяць
        current_month = conn.execute('''
            SELECT 
                SUM(energy_kwh) as month_energy,
                SUM(cost_uah) as month_cost
            FROM power_hourly
            WHERE timestamp >= date('now', 'start of month')
        ''').fetchone()

        # Статистика за поточний рік
        current_year = conn.execute('''
            SELECT 
                SUM(energy_kwh) as year_energy,
                SUM(cost_uah) as year_cost
            FROM power_hourly
            WHERE timestamp >= date('now', 'start of year')
        ''').fetchone()

        return {
            'total_energy_kwh': round(total['total_energy'] or 0, 2),
            'total_cost_uah': round(total['total_cost'] or 0, 2),
            'avg_power_watts': round(total['avg_power'] or 0, 2),
            'total_sessions': total['total_sessions'] or 0,
            'total_hours': round(total['total_hours'] or 0, 2),
            'month_energy_kwh': round(current_month['month_energy'] or 0, 2),
            'month_cost_uah': round(current_month['month_cost'] or 0, 2),
            'year_energy_kwh': round(current_year['year_energy'] or 0, 2),
            'year_cost_uah': round(current_year['year_cost'] or 0, 2)
        }


def get_power_settings() -> Dict:
    """Отримання налаштувань"""
    with get_power_db() as conn:
        cursor = conn.execute('SELECT key, value FROM power_settings')
        return {row['key']: row['value'] for row in cursor.fetchall()}


def update_power_settings(settings: Dict) -> bool:
    """Оновлення налаштувань"""
    with get_power_db() as conn:
        for key, value in settings.items():
            conn.execute('''
                INSERT OR REPLACE INTO power_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, str(value)))
        return True