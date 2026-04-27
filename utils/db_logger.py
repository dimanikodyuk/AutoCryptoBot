"""
Логгер, який пише в базу даних
"""
import logging
import threading
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent / "trading_bot.db"

# Локальний кеш налаштувань логів
_log_settings_cache = {}
_cache_lock = threading.Lock()


def _get_log_settings():
    """Отримання налаштувань логування для всіх модулів (без імпорту db)"""
    global _log_settings_cache
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT module, log_level FROM log_settings")
        rows = cursor.fetchall()
        conn.close()
        with _cache_lock:
            _log_settings_cache = {row['module']: row['log_level'] for row in rows}
        return _log_settings_cache
    except Exception:
        return {}


def _add_log(level: str, module: str, message: str):
    """Додати лог в БД (без імпорту db)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO logs (level, module, message) VALUES (?, ?, ?)",
            (level, module, message)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def refresh_log_settings():
    """Оновлення кешу налаштувань логів"""
    _get_log_settings()


def get_module_log_level(module: str) -> str:
    """Отримання рівня логування для модуля"""
    with _cache_lock:
        if not _log_settings_cache:
            _get_log_settings()
        return _log_settings_cache.get(module, 'INFO')


class DatabaseLogHandler(logging.Handler):
    """Обробник логів для запису в базу даних"""

    def __init__(self, module: str):
        super().__init__()
        self.module = module
        self._buffer = []
        self._buffer_lock = threading.Lock()
        self._flush_count = 0

    def emit(self, record):
        """Запис логу в БД"""
        try:
            level = record.levelname
            message = self.format(record)

            # Перевіряємо чи потрібно логувати цей рівень
            required_level = get_module_log_level(self.module)
            level_priority = {'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3}

            if level_priority.get(level, 1) < level_priority.get(required_level, 1):
                return

            # Додаємо в буфер
            with self._buffer_lock:
                self._buffer.append((level, message))
                self._flush_count += 1

            # Скидаємо буфер кожні 10 логів або для ERROR
            if self._flush_count >= 10 or level == 'ERROR':
                self._flush_buffer()

        except Exception:
            self.handleError(record)

    def _flush_buffer(self):
        """Скидання буфера в БД"""
        if not self._buffer:
            return

        with self._buffer_lock:
            buffer_copy = self._buffer.copy()
            self._buffer.clear()
            self._flush_count = 0

        for level, message in buffer_copy:
            try:
                _add_log(level, self.module, message)
            except Exception:
                pass

    def flush(self):
        """Примусове скидання буфера"""
        self._flush_buffer()


def setup_db_logger(name: str, level=logging.INFO):
    """
    Налаштування логера, який пише в БД

    Використання:
        logger = setup_db_logger('my_module')
        logger.info("Повідомлення")
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Видаляємо старі обробники
    logger.handlers.clear()

    # Додаємо обробник для БД
    db_handler = DatabaseLogHandler(name)
    db_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(db_handler)

    # Додаємо консольний обробник для налагодження
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(console_handler)

    return logger


def update_log_level(module: str, level: str):
    """
    Оновлення рівня логування для модуля (без перезапуску)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE log_settings SET log_level = ?, updated_at = CURRENT_TIMESTAMP WHERE module = ?",
            (level, module)
        )
        conn.commit()
        conn.close()

        # Оновлюємо кеш
        refresh_log_settings()

        # Оновлюємо рівень для існуючого логера
        logger = logging.getLogger(module)
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR
        }
        logger.setLevel(level_map.get(level, logging.INFO))

        return True
    except Exception as e:
        print(f"Помилка оновлення рівня логування: {e}")
        return False


# Оновлюємо кеш при імпорті
refresh_log_settings()