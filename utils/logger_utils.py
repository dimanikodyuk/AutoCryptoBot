import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')


def setup_logger(name: str, level=logging.INFO):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)

    # Файловий обробник
    fh = logging.FileHandler(LOG_DIR / f'{name}.log', encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Консольний обробник
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


def update_log_level(module: str, level: str):
    """
    Оновлення рівня логування для модуля без перезапуску

    Args:
        module: назва модуля (main, engine, grid, scalp, news, web, telegram, database)
        level: рівень логування (INFO, WARNING, ERROR)
    """
    try:
        # Оновлюємо в БД
        from database.db import update_log_settings
        if not update_log_settings(module, level):
            return False

        # Оновлюємо рівень для існуючого логера
        logger = logging.getLogger(module)
        level_map = {
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'DEBUG': logging.DEBUG
        }
        new_level = level_map.get(level, logging.INFO)
        logger.setLevel(new_level)

        print(f"✅ Рівень логування для {module} змінено на {level}")
        return True

    except Exception as e:
        print(f"❌ Помилка оновлення рівня логування для {module}: {e}")
        return False


def refresh_log_settings():
    """Оновлення налаштувань логів (заглушка для сумісності)"""
    pass