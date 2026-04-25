import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# Базовий формат для всіх логів
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
    # Консольний обробник (опціонально)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger