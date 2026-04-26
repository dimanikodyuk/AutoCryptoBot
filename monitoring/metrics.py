"""
Prometheus метрики для моніторингу бота
"""

import time
from datetime import datetime
from typing import Dict, List, Optional

from prometheus_client import (
    Counter, Gauge, Histogram, Summary, Info,
    generate_latest, REGISTRY, CollectorRegistry
)

# Створюємо окремий реєстр для наших метрик
registry = CollectorRegistry()

# ============= ТОРГОВІ МЕТРИКИ =============

# PnL метрики
total_pnl_gauge = Gauge(
    'trading_total_pnl',
    'Загальний PnL по всіх стратегіях',
    ['strategy', 'mode'],
    registry=registry
)

daily_pnl_gauge = Gauge(
    'trading_daily_pnl',
    'Денний PnL по стратегії',
    ['strategy'],
    registry=registry
)

# Угоди
trades_counter = Counter(
    'trading_trades_total',
    'Загальна кількість угод',
    ['strategy', 'side', 'status'],
    registry=registry
)

winning_trades_counter = Counter(
    'trading_winning_trades_total',
    'Кількість прибуткових угод',
    ['strategy'],
    registry=registry
)

losing_trades_counter = Counter(
    'trading_losing_trades_total',
    'Кількість збиткових угод',
    ['strategy'],
    registry=registry
)

# Win rate
win_rate_gauge = Gauge(
    'trading_win_rate_percent',
    'Відсоток прибуткових угод',
    ['strategy'],
    registry=registry
)

# Баланс
balance_gauge = Gauge(
    'trading_balance_usdt',
    'Поточний баланс в USDT',
    ['strategy', 'type'],  # type: total, locked, available
    registry=registry
)

# Drawdown
drawdown_gauge = Gauge(
    'trading_drawdown_percent',
    'Поточний drawdown у відсотках',
    ['strategy'],
    registry=registry
)

# Ліміти
daily_trades_gauge = Gauge(
    'trading_daily_trades',
    'Кількість угод сьогодні',
    ['strategy'],
    registry=registry
)

is_blocked_gauge = Gauge(
    'trading_is_blocked',
    'Чи заблокована стратегія (1=так, 0=ні)',
    ['strategy', 'reason'],
    registry=registry
)

# ============= РИНКОВІ МЕТРИКИ =============

current_price_gauge = Gauge(
    'market_current_price',
    'Поточна ціна символу',
    ['symbol'],
    registry=registry
)

price_change_gauge = Gauge(
    'market_price_change_percent',
    'Зміна ціни за 24 години у відсотках',
    ['symbol'],
    registry=registry
)

volume_gauge = Gauge(
    'market_volume_24h',
    'Об\'єм торгів за 24 години',
    ['symbol'],
    registry=registry
)

# ============= СИСТЕМНІ МЕТРИКИ =============

# CPU та RAM
cpu_usage_gauge = Gauge(
    'system_cpu_usage_percent',
    'Використання CPU у відсотках',
    registry=registry
)

ram_usage_gauge = Gauge(
    'system_ram_usage_percent',
    'Використання RAM у відсотках',
    registry=registry
)

ram_used_gauge = Gauge(
    'system_ram_used_bytes',
    'Використана RAM в байтах',
    registry=registry
)

# Диск
disk_usage_gauge = Gauge(
    'system_disk_usage_percent',
    'Використання диска у відсотках',
    registry=registry
)

# Uptime
uptime_gauge = Gauge(
    'system_uptime_seconds',
    'Час роботи бота в секундах',
    registry=registry
)

# ============= МЕТРИКИ СТРАТЕГІЙ =============

# Grid специфічні метрики
grid_active_buys_gauge = Gauge(
    'grid_active_buy_orders',
    'Кількість активних BUY ордерів',
    ['symbol'],
    registry=registry
)

grid_active_sells_gauge = Gauge(
    'grid_active_sell_orders',
    'Кількість активних SELL ордерів',
    ['symbol'],
    registry=registry
)

grid_locked_balance_gauge = Gauge(
    'grid_locked_balance_usdt',
    'Заблокований баланс в USDT',
    ['symbol'],
    registry=registry
)

# Scalp специфічні метрики
scalp_open_positions_gauge = Gauge(
    'scalp_open_positions',
    'Кількість відкритих позицій',
    ['symbol'],
    registry=registry
)

scalp_current_signal_gauge = Gauge(
    'scalp_current_signal',
    'Поточний сигнал (1=buy, 0=neutral, -1=sell)',
    ['symbol'],
    registry=registry
)

# News специфічні метрики
news_sentiment_gauge = Gauge(
    'news_sentiment_score',
    'Сентимент новин (-1=negative, 0=neutral, 1=positive)',
    registry=registry
)

news_articles_count_gauge = Gauge(
    'news_articles_24h',
    'Кількість новин за 24 години',
    registry=registry
)

# ============= WEBHOOK МЕТРИКИ =============

api_requests_counter = Counter(
    'api_requests_total',
    'Кількість API запитів',
    ['endpoint', 'method', 'status'],
    registry=registry
)

api_request_duration_histogram = Histogram(
    'api_request_duration_seconds',
    'Тривалість API запитів',
    ['endpoint'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=registry
)

websocket_connections_gauge = Gauge(
    'websocket_connections',
    'Кількість активних WebSocket з\'єднань',
    registry=registry
)


# ============= ДОПОМІЖНІ ФУНКЦІЇ =============

class MetricsCollector:
    """Клас для збору та оновлення метрик"""

    def __init__(self):
        self.start_time = time.time()

    def update_trading_metrics(self, strategy_name: str, status: dict, mode: str):
        """Оновлення торгових метрик"""
        try:
            # PnL
            total_pnl_gauge.labels(strategy=strategy_name, mode=mode).set(status.get('total_pnl', 0))

            # Баланс
            balance_gauge.labels(strategy=strategy_name, type='total').set(status.get('balance', 0))
            balance_gauge.labels(strategy=strategy_name, type='locked').set(status.get('locked_balance', 0))
            balance_gauge.labels(strategy=strategy_name, type='available').set(status.get('available_balance', 0))

            # Win rate
            win_rate_gauge.labels(strategy=strategy_name).set(status.get('win_rate', 0))

            # Ліміти
            daily_trades_gauge.labels(strategy=strategy_name).set(status.get('daily_trades_count', 0))

            # Блокування
            is_blocked = 1 if status.get('is_blocked', False) else 0
            reason = status.get('block_reason', 'none')
            is_blocked_gauge.labels(strategy=strategy_name, reason=reason).set(is_blocked)

        except Exception as e:
            print(f"Помилка оновлення метрик {strategy_name}: {e}")

    def update_grid_metrics(self, symbol: str, grid_status: dict):
        """Оновлення Grid специфічних метрик"""
        try:
            grid_active_buys_gauge.labels(symbol=symbol).set(grid_status.get('active_buys', 0))
            grid_active_sells_gauge.labels(symbol=symbol).set(grid_status.get('active_sells', 0))
            grid_locked_balance_gauge.labels(symbol=symbol).set(grid_status.get('locked_balance', 0))
        except Exception as e:
            print(f"Помилка оновлення Grid метрик {symbol}: {e}")

    def update_scalp_metrics(self, symbol: str, position_exists: bool, signal: int = 0):
        """Оновлення Scalp специфічних метрик"""
        try:
            scalp_open_positions_gauge.labels(symbol=symbol).set(1 if position_exists else 0)
            scalp_current_signal_gauge.labels(symbol=symbol).set(signal)
        except Exception as e:
            print(f"Помилка оновлення Scalp метрик: {e}")

    def update_news_metrics(self, sentiment: str, articles_count: int):
        """Оновлення News специфічних метрик"""
        try:
            sentiment_score = 1 if sentiment == 'positive' else (-1 if sentiment == 'negative' else 0)
            news_sentiment_gauge.set(sentiment_score)
            news_articles_count_gauge.set(articles_count)
        except Exception as e:
            print(f"Помилка оновлення News метрик: {e}")

    def update_system_metrics(self, cpu_percent: float, ram_percent: float,
                              ram_used: float, disk_percent: float):
        """Оновлення системних метрик"""
        try:
            cpu_usage_gauge.set(cpu_percent)
            ram_usage_gauge.set(ram_percent)
            ram_used_gauge.set(ram_used * 1024 * 1024 * 1024)  # GB -> bytes
            disk_usage_gauge.set(disk_percent)
            uptime_gauge.set(time.time() - self.start_time)
        except Exception as e:
            print(f"Помилка оновлення системних метрик: {e}")

    def update_market_metrics(self, symbol: str, price: float):
        """Оновлення ринкових метрик"""
        try:
            current_price_gauge.labels(symbol=symbol).set(price)
        except Exception as e:
            print(f"Помилка оновлення ринкових метрик: {e}")

    def increment_trade(self, strategy: str, side: str, pnl: float):
        """Збільшення лічильника угод"""
        try:
            status = 'win' if pnl > 0 else 'loss'
            trades_counter.labels(strategy=strategy, side=side, status=status).inc()

            if pnl > 0:
                winning_trades_counter.labels(strategy=strategy).inc()
            else:
                losing_trades_counter.labels(strategy=strategy).inc()
        except Exception as e:
            print(f"Помилка інкременту угод: {e}")


# Глобальний екземпляр збирача метрик
metrics_collector = MetricsCollector()


def get_metrics():
    """Повертає всі метрики в форматі Prometheus"""
    return generate_latest(registry)


def get_metrics_json() -> dict:
    """Повертає метрики в JSON форматі для веб-інтерфейсу"""
    metrics = {}

    # Збираємо всі метрики в словник
    for metric in registry.collect():
        metric_name = metric.name
        metrics[metric_name] = []
        for sample in metric.samples:
            metrics[metric_name].append({
                'name': sample.name,
                'labels': sample.labels,
                'value': sample.value
            })

    return metrics