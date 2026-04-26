import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULT_SETTINGS = {
    "grid": {
        "symbols": ["BTCUSDT", "SOLUSDT"],  # ← ДОДАНО КОМУ
        "grid_levels": 10,
        "order_size_usdt": 50,
        "lower_percent": 20,
        "upper_percent": 20,
        "enabled": False
    },
    "news": {
        "symbols": ["BTC", "SOL"],
        "interval_minutes": 10,
        "sensitivity": "medium",
        "enabled": False
    },
    "scalp": {
        "symbols": ["BTCUSDT", "SOLUSDT"],
        "trade_size_usdt": 20,
        "take_profit_percent": 0.5,
        "stop_loss_percent": 0.25,
        "trailing_stop_percent": 0.3,
        "timeframe": "1",
        "enabled": False
    }
}

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                for key in DEFAULT_SETTINGS:
                    if key not in settings:
                        settings[key] = DEFAULT_SETTINGS[key].copy()
                    else:
                        for subkey in DEFAULT_SETTINGS[key]:
                            if subkey not in settings[key]:
                                settings[key][subkey] = DEFAULT_SETTINGS[key][subkey]
                return settings
        except Exception as e:
            print(f"Помилка завантаження налаштувань: {e}")
            return DEFAULT_SETTINGS.copy()
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Помилка збереження налаштувань: {e}")
        return False

def save_strategy_settings(strategy_name: str, **kwargs):
    settings = load_settings()
    if strategy_name not in settings:
        settings[strategy_name] = DEFAULT_SETTINGS.get(strategy_name, {}).copy()

    for key, value in kwargs.items():
        settings[strategy_name][key] = value

    return save_settings(settings)

def get_strategy_settings(strategy_name: str):
    settings = load_settings()
    result = settings.get(strategy_name, {})
    default = DEFAULT_SETTINGS.get(strategy_name, {})
    for key, value in default.items():
        if key not in result:
            result[key] = value
    return result