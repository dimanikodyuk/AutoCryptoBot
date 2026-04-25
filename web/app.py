import asyncio
import json
import os
import time
import threading
import subprocess
import sys
import re
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from functools import wraps
import logging
from utils.logger_utils import setup_logger
# Імпортуємо get_db з database.db
from database.db import get_db

# Налаштовуємо логер
logger = setup_logger('web')
# Глобальний список сповіщень
notification_queue = []

def async_route(f):
    """Декоратор для асинхронних Flask роутів"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(f(*args, **kwargs))
    return wrapped


def create_flask_app(config, trading_engine):
    """Створення Flask додатку"""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = config.FLASK_SECRET_KEY

    # Головна сторінка
    @app.route('/')
    def dashboard():
        return render_template('dashboard.html')

    @app.route('/favicon.ico')
    def favicon():
        return '', 204

    # ============= API для дашборду =============

    @app.route('/api/status')
    @async_route
    async def api_status():
        summary = await trading_engine.get_summary()
        return jsonify(summary)

    @app.route('/api/strategies')
    @async_route
    async def api_strategies():
        strategies_data = []
        for strat_id, strategy in trading_engine.strategies.items():
            status = await strategy.get_status()
            strategies_data.append(status)
        return jsonify(strategies_data)

    @app.route('/api/strategy/<int:strategy_id>/toggle', methods=['POST'])
    @async_route
    async def api_toggle_strategy(strategy_id):
        if strategy_id in trading_engine.strategies:
            strategy = trading_engine.strategies[strategy_id]
            if strategy.enabled:
                await trading_engine.stop_strategy(strategy_id)
            else:
                # При запуску - примусово скидаємо стан grid якщо він не ініціалізований
                if strategy.name == 'grid' and hasattr(strategy, 'grids'):
                    for grid in strategy.grids.values():
                        if not grid.is_initialized and not grid.active_buy_orders:
                            # Отримуємо ціну та ініціалізуємо
                            price = await trading_engine.exchange.get_current_price(grid.symbol)
                            if price > 0:
                                await grid.initialize_grid(price)

                await trading_engine.start_strategy(strategy_id)
            return jsonify({'success': True, 'enabled': strategy.enabled})
        return jsonify({'error': 'Strategy not found'}), 404

    @app.route('/api/strategy/<int:strategy_id>/reset', methods=['POST'])
    @async_route
    async def api_reset_strategy(strategy_id):
        if strategy_id in trading_engine.strategies:
            strategy = trading_engine.strategies[strategy_id]

            # Зупиняємо стратегію
            was_enabled = strategy.enabled
            if was_enabled:
                await trading_engine.stop_strategy(strategy_id)

            # Виконуємо скидання
            await strategy.reset()

            # Додатково очищаємо grid стан
            if hasattr(strategy, 'grids'):
                for grid in strategy.grids.values():
                    grid.is_initialized = False
                    grid.lower_price = None
                    grid.upper_price = None
                    grid.grid_spacing = None
                    grid.active_buy_orders.clear()
                    grid.active_sell_orders.clear()
                    grid.price_history.clear()
                    grid.locked_balance = 0

            # Повертаємо success, але не запускаємо автоматично
            return jsonify({'success': True, 'was_enabled': was_enabled})
        return jsonify({'error': 'Strategy not found'}), 404

    @app.route('/api/reset_all', methods=['POST'])
    @async_route
    async def api_reset_all():
        for strategy in trading_engine.strategies.values():
            await strategy.reset()
        return jsonify({'success': True})

    @app.route('/api/emergency_stop', methods=['POST'])
    @async_route
    async def api_emergency_stop():
        await trading_engine.emergency_stop_all()
        return jsonify({'success': True})

    @app.route('/api/orders')
    @async_route
    async def api_orders():
        side = request.args.get('side', 'all')
        status = request.args.get('status', 'all')
        limit = int(request.args.get('limit', 50))
        strategy_name = request.args.get('strategy', None)

        orders = await trading_engine.get_orders(side, status, limit, strategy_name)
        return jsonify(orders)

    @app.route('/api/send_notification', methods=['POST'])
    @async_route
    async def api_send_notification():
        data = request.get_json()
        notification_queue.append({
            'title': data.get('title'),
            'message': data.get('message'),
            'type': data.get('type', 'info'),
            'timestamp': time.time()
        })
        if len(notification_queue) > 100:
            notification_queue.pop(0)
        return jsonify({'success': True})

    @app.route('/api/notifications')
    @async_route
    async def api_get_notifications():
        # Повертаємо сповіщення та очищаємо
        global notification_queue
        notifications = notification_queue.copy()
        notification_queue = []
        return jsonify({'notifications': notifications})

    @app.route('/api/strategy_data/<strategy_name>')
    @async_route
    async def api_strategy_data(strategy_name):
        for strategy in trading_engine.strategies.values():
            if strategy.name.lower() == strategy_name.lower():
                status = await strategy.get_status()
                orders = await trading_engine.get_orders_by_strategy(strategy.strategy_id)
                pnl_history = await trading_engine.get_pnl_history(strategy.strategy_id)
                return jsonify({
                    'status': status,
                    'orders': orders,
                    'pnl_history': pnl_history
                })
        return jsonify({'error': 'Strategy not found'}), 404

    # ============= Grid API =============

    @app.route('/api/grid_levels/<symbol>')
    @async_route
    async def api_grid_levels_symbol(symbol):
        """Отримання рівнів grid сітки для конкретної пари"""
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'grid' and hasattr(strategy, 'get_grid_levels_for_symbol'):
                data = await strategy.get_grid_levels_for_symbol(symbol)
                return jsonify(data)
        return jsonify({'error': 'Grid strategy not found'}), 404

    @app.route('/api/grid_settings', methods=['POST'])
    @async_route
    async def api_grid_settings():
        """Оновлення налаштувань Grid стратегії"""
        if request.is_json:
            data = request.get_json()
        else:
            data = json.loads(request.data)

        for strategy in trading_engine.strategies.values():
            if strategy.name == 'grid' and hasattr(strategy, 'update_settings'):
                await strategy.update_settings(
                    symbols_list=data.get('symbols_list'),
                    grid_levels=data.get('grid_levels'),
                    order_size_usdt=data.get('order_size_usdt'),
                    lower_percent=data.get('lower_percent'),
                    upper_percent=data.get('upper_percent')
                )
                return jsonify({'success': True})

        return jsonify({'error': 'Strategy not found'}), 404

    # ============= База даних API =============

    @app.route('/api/db_tables_list')
    @async_route
    async def api_db_tables_list():
        """Отримання списку таблиць БД"""
        try:
            with get_db() as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = [row['name'] for row in cursor.fetchall()]
            return jsonify(tables)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/db_table/<table_name>')
    @async_route
    async def api_db_table_data(table_name):
        """Отримання даних з таблиці БД"""
        limit = request.args.get('limit', 50, type=int)

        allowed_tables = ['orders', 'strategies', 'balances', 'logs', 'system_monitor']
        if table_name not in allowed_tables:
            return jsonify({'error': 'Table not allowed'}), 403

        try:
            with get_db() as conn:
                cursor = conn.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT ?", (limit,))
                rows = cursor.fetchall()
                return jsonify([dict(row) for row in rows])
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/db_tables_info')
    @async_route
    async def api_db_tables_info():
        """Отримання детальної інформації про таблиці БД"""
        try:
            with get_db() as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = cursor.fetchall()

                tables_info = []
                for table in tables:
                    table_name = table['name']

                    # Кількість рядків
                    count_cursor = conn.execute(f"SELECT COUNT(*) as count FROM {table_name}")
                    row_count = count_cursor.fetchone()['count']

                    # Останнє оновлення (пошук max id або timestamp)
                    try:
                        last_cursor = conn.execute(
                            f"SELECT MAX(id) as last_id, MAX(opened_at) as last_date FROM {table_name}")
                        last_row = last_cursor.fetchone()
                        last_updated = last_row['last_date'] if last_row and last_row['last_date'] else None
                    except:
                        last_updated = None

                    # Розмір таблиці в БД (приблизно)
                    try:
                        size_cursor = conn.execute(
                            f"SELECT SUM(LENGTH(CAST({table_name} as TEXT))) as size FROM {table_name}")
                        size_info = size_cursor.fetchone()
                    except:
                        size_info = None

                    tables_info.append({
                        'name': table_name,
                        'row_count': row_count,
                        'last_updated': last_updated,
                        'columns': []  # Будемо додавати окремо
                    })

                return jsonify(tables_info)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/db_table_info/<table_name>')
    @async_route
    async def api_db_table_info(table_name):
        """Отримання інформації про структуру таблиці"""
        allowed_tables = ['orders', 'strategies', 'balances', 'logs', 'system_monitor']
        if table_name not in allowed_tables:
            return jsonify({'error': 'Table not allowed'}), 403

        try:
            with get_db() as conn:
                # Отримуємо інформацію про колонки
                cursor = conn.execute(f"PRAGMA table_info({table_name})")
                columns = [{'name': col[1], 'type': col[2]} for col in cursor.fetchall()]

                # Кількість рядків
                count_cursor = conn.execute(f"SELECT COUNT(*) as count FROM {table_name}")
                row_count = count_cursor.fetchone()['count']

                return jsonify({
                    'name': table_name,
                    'columns': columns,
                    'row_count': row_count
                })
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/db_table_data/<table_name>')
    @async_route
    async def api_db_table_data_paginated(table_name):
        """Отримання даних з таблиці БД з пагінацією"""
        allowed_tables = ['orders', 'strategies', 'balances', 'logs', 'system_monitor']
        if table_name not in allowed_tables:
            return jsonify({'error': 'Table not allowed'}), 403

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        search = request.args.get('search', '')
        sort_by = request.args.get('sort_by', 'id')
        sort_order = request.args.get('sort_order', 'desc')

        # Валідація параметрів
        if per_page > 100:
            per_page = 100
        if page < 1:
            page = 1

        try:
            with get_db() as conn:
                # Безпечне сортування
                allowed_sort_columns = ['id', 'opened_at', 'closed_at', 'price', 'pnl', 'timestamp']
                if sort_by not in allowed_sort_columns:
                    sort_by = 'id'
                sort_dir = 'DESC' if sort_order == 'desc' else 'ASC'

                # Побудова запиту з пошуком
                query = f"SELECT * FROM {table_name}"
                params = []

                if search:
                    # Шукаємо в текстових полях
                    query += f" WHERE CAST(id as TEXT) LIKE ? OR CAST(symbol as TEXT) LIKE ? OR CAST(side as TEXT) LIKE ?"
                    search_pattern = f"%{search}%"
                    params = [search_pattern, search_pattern, search_pattern]

                # Отримуємо загальну кількість
                count_query = f"SELECT COUNT(*) as total FROM {table_name}"
                if search:
                    count_query += f" WHERE CAST(id as TEXT) LIKE ? OR CAST(symbol as TEXT) LIKE ? OR CAST(side as TEXT) LIKE ?"
                count_cursor = conn.execute(count_query, params)
                total_rows = count_cursor.fetchone()['total']

                # Пагінація
                offset = (page - 1) * per_page
                query += f" ORDER BY {sort_by} {sort_dir} LIMIT ? OFFSET ?"
                params.extend([per_page, offset])

                cursor = conn.execute(query, params)
                rows = cursor.fetchall()

                return jsonify({
                    'rows': [dict(row) for row in rows],
                    'total': total_rows,
                    'page': page,
                    'per_page': per_page,
                    'total_pages': (total_rows + per_page - 1) // per_page if total_rows > 0 else 1
                })
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    # ============= Логи API =============

    @app.route('/api/logs_file')
    @async_route
    async def api_logs_file():
        limit = request.args.get('limit', 200, type=int)
        level = request.args.get('level', 'all')
        source_filter = request.args.get('strategy', None)

        log_file = Path(__file__).parent.parent / 'logs' / 'bot.log'
        if not log_file.exists():
            return jsonify([])

        logs = []
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines[-limit:]:
                    pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (\w+) - (.+)'
                    match = re.match(pattern, line.strip())
                    if match:
                        log_level = match.group(2)
                        log_source = match.group(3)
                        log_message = match.group(4)

                        if level != 'all' and log_level.upper() != level.upper():
                            continue
                        if source_filter and source_filter.lower() != 'all':
                            if source_filter.lower() not in log_source.lower():
                                continue

                        logs.append({
                            'timestamp': match.group(1),
                            'level': log_level,
                            'source': log_source,
                            'message': log_message
                        })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        return jsonify(logs)

    async def read_log_file(log_file: Path, limit: int, level: str) -> list:
        """Читання одного лог-файлу"""
        logs = []
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines[-limit:]:
                    pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - (\w+) - (.+)'
                    match = re.match(pattern, line.strip())
                    if match:
                        log_level = match.group(2)
                        log_message = match.group(3)

                        if level != 'all' and log_level.upper() != level.upper():
                            continue

                        logs.append({
                            'timestamp': match.group(1),
                            'level': log_level,
                            'source': log_file.stem,
                            'message': log_message
                        })
        except Exception as e:
            logger.error(f"Помилка читання {log_file}: {e}")
        return logs

    # ============= Свічки API =============

    @app.route('/api/klines/<symbol>')
    @async_route
    async def api_klines(symbol):
        """Отримання історичних свічок для графіка"""
        interval = request.args.get('interval', '1')
        limit = request.args.get('limit', 100, type=int)

        klines = await trading_engine.exchange.get_klines(symbol, interval, limit)
        return jsonify(klines)

    @app.route('/api/clear_logs', methods=['POST'])
    @async_route
    async def api_clear_logs():
        """Очищення логів"""
        try:
            with get_db() as conn:
                conn.execute("DELETE FROM logs")
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ============= Режим роботи API =============

    @app.route('/api/mode')
    @async_route
    async def api_get_mode():
        """Отримання поточного режиму роботи"""
        return jsonify({'mode': trading_engine.config.DEFAULT_MODE})

    @app.route('/api/mode', methods=['POST'])
    @async_route
    async def api_set_mode():
        """Зміна режиму роботи (з підтвердженням)"""
        data = request.get_json()
        new_mode = data.get('mode')
        confirm = data.get('confirm', False)

        if new_mode not in ['simulation', 'monitor', 'real']:
            return jsonify({'error': 'Invalid mode'}), 400

        old_mode = trading_engine.config.DEFAULT_MODE

        if new_mode == 'real' and not confirm:
            return jsonify({'need_confirmation': True,
                            'message': '⚠️ УВАГА! Перехід у РЕАЛЬНИЙ режим означає використання реальних коштів. Підтвердіть перехід.'}), 400

        if new_mode == 'real':
            # Перевіряємо наявність API ключів
            if not trading_engine.config.BYBIT_API_KEY or not trading_engine.config.BYBIT_API_SECRET:
                return jsonify({'error': 'API ключі Bybit не налаштовані'}), 400

            # Перевіряємо баланс
            balance = await trading_engine.exchange.get_real_balance('USDT')
            if balance < 10:
                return jsonify(
                    {'error': f'Недостатньо балансу для реальної торгівлі. Доступно: ${balance:.2f}'}), 400

            logger.warning(f"🟢 ПЕРЕХІД У РЕАЛЬНИЙ РЕЖИМ! Баланс: ${balance:.2f}")

        trading_engine.config.DEFAULT_MODE = new_mode
        trading_engine.exchange.mode = new_mode

        # Оновлюємо .env файл
        env_path = Path(__file__).parent.parent / '.env'
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        with open(env_path, 'w', encoding='utf-8') as f:
            for line in lines:
                if line.startswith('DEFAULT_MODE='):
                    f.write(f'DEFAULT_MODE={new_mode}\n')
                else:
                    f.write(line)

        logger.info(f"Режим змінено: {old_mode} -> {new_mode}")

        return jsonify({
            'success': True,
            'mode': new_mode,
            'message': f'Режим змінено на {new_mode}'
        })

    @app.route('/api/balance')
    @async_route
    async def api_get_balance():
        """Отримання реального балансу"""
        try:
            balance = await trading_engine.exchange.get_real_balance('USDT')
            return jsonify({'balance': balance})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/scalp_status')
    @async_route
    async def api_scalp_status():
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'scalp':
                status = await strategy.get_status()
                return jsonify(status)
        return jsonify({'error': 'Scalp strategy not found'}), 404

    @app.route('/api/scalp_settings', methods=['POST'])
    @async_route
    async def api_scalp_settings():
        data = request.get_json()
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'scalp' and hasattr(strategy, 'update_settings'):
                await strategy.update_settings(
                    symbols=data.get('symbols'),
                    trade_size_usdt=data.get('trade_size_usdt'),
                    take_profit_percent=data.get('take_profit_percent'),
                    stop_loss_percent=data.get('stop_loss_percent'),
                    trailing_stop_percent=data.get('trailing_stop_percent')
                )
                return jsonify({'success': True})
        return jsonify({'error': 'Strategy not found'}), 404
    # ============= Система API =============

    @app.route('/api/system_status')
    @async_route
    async def api_system_status():
        """Отримання системного статусу"""
        import psutil

        start_time = getattr(trading_engine, 'start_time', None)
        if not start_time:
            start_time = time.time()
            trading_engine.start_time = start_time

        uptime_seconds = int(time.time() - start_time)
        uptime_str = f"{uptime_seconds // 3600}г {(uptime_seconds % 3600) // 60}х {uptime_seconds % 60}с"

        try:
            return jsonify({
                'cpu_percent': psutil.cpu_percent(interval=0.5),
                'cpu_count': psutil.cpu_count(),
                'ram_percent': psutil.virtual_memory().percent,
                'ram_used_gb': round(psutil.virtual_memory().used / (1024 ** 3), 2),
                'ram_total_gb': round(psutil.virtual_memory().total / (1024 ** 3), 2),
                'disk_percent': psutil.disk_usage('/').percent,
                'disk_used_gb': round(psutil.disk_usage('/').used / (1024 ** 3), 2),
                'disk_total_gb': round(psutil.disk_usage('/').total / (1024 ** 3), 2),
                'uptime': uptime_str,
                'uptime_seconds': uptime_seconds
            })
        except Exception as e:
            return jsonify({'error': str(e), 'uptime': uptime_str}), 500

    # ============= Електроенергія API =============

    @app.route('/api/electricity')
    @async_route
    async def api_electricity():
        """Отримання інформації про витрати електроенергії з точним розрахунком"""
        import psutil
        import os
        from datetime import datetime

        # ============= БАЗОВІ НАЛАШТУВАННЯ =============
        # Базове споживання Raspberry Pi 4 (без навантаження)
        BASE_POWER_WATTS = 3.5  # W (3.5W в режимі очікування)
        MAX_POWER_WATTS = 6.5  # W (6.5W при максимальному навантаженні)

        # ККД блока живлення (за замовчуванням 85%)
        PSU_EFFICIENCY = float(os.getenv('PSU_EFFICIENCY', '0.85'))

        # Втрати в USB кабелі (за замовчуванням 3%)
        CABLE_LOSS = float(os.getenv('CABLE_LOSS', '0.03'))

        # Ціна за кВт⋅год (грн)
        PRICE_PER_KWH = float(os.getenv('ELECTRICITY_PRICE', '4.32'))

        # ============= РОЗРАХУНОК ПОТОЧНОГО СПОЖИВАННЯ =============
        # Отримуємо навантаження CPU
        cpu_percent = psutil.cpu_percent(interval=0.5)
        ram_percent = psutil.virtual_memory().percent

        # Розраховуємо коефіцієнт навантаження
        # CPU: 0-100% → 0.7-1.3, RAM: 0-100% → 0.9-1.1
        cpu_factor = 0.7 + (cpu_percent / 100) * 0.6
        ram_factor = 0.9 + (ram_percent / 100) * 0.2
        load_factor = (cpu_factor + ram_factor) / 2  # Середнє

        # Поточна потужність Raspberry Pi
        rpi_power = BASE_POWER_WATTS + (MAX_POWER_WATTS - BASE_POWER_WATTS) * (load_factor - 0.7) / 0.6
        rpi_power = max(BASE_POWER_WATTS, min(MAX_POWER_WATTS, rpi_power))

        # Додаємо втрати блока живлення та кабелю
        total_power = rpi_power / PSU_EFFICIENCY * (1 + CABLE_LOSS)

        # ============= ЧАС РОБОТИ =============
        start_time = getattr(trading_engine, 'start_time', None)
        if not start_time:
            start_time = time.time()
            trading_engine.start_time = start_time

        uptime_seconds = int(time.time() - start_time)
        uptime_hours = uptime_seconds / 3600
        uptime_days = uptime_hours / 24

        # Розрахунок спожитої енергії (кВт⋅год)
        energy_kwh = (total_power * uptime_hours) / 1000
        cost_uah = energy_kwh * PRICE_PER_KWH

        # ============= ПРОГНОЗ НА МІСЯЦЬ ТА РІК =============
        hours_per_month = 30 * 24
        hours_per_year = 365 * 24

        monthly_energy_kwh = (total_power * hours_per_month) / 1000
        monthly_cost_uah = monthly_energy_kwh * PRICE_PER_KWH

        yearly_energy_kwh = (total_power * hours_per_year) / 1000
        yearly_cost_uah = yearly_energy_kwh * PRICE_PER_KWH

        # ============= ДОДАТКОВА ІНФОРМАЦІЯ =============
        # Середня потужність за весь час роботи
        avg_power = (energy_kwh * 1000) / uptime_hours if uptime_hours > 0 else total_power

        # Оцінка вартості за день
        daily_cost = (total_power * 24 / 1000) * PRICE_PER_KWH

        return jsonify({
            # Поточна інформація
            'current_power': round(total_power, 2),
            'rpi_power': round(rpi_power, 2),
            'psu_efficiency': round(PSU_EFFICIENCY * 100, 1),
            'cable_loss': round(CABLE_LOSS * 100, 1),
            'cpu_load': round(cpu_percent, 1),
            'ram_load': round(ram_percent, 1),

            # Час роботи
            'uptime_hours': round(uptime_hours, 2),
            'uptime_days': round(uptime_days, 2),
            'uptime_seconds': uptime_seconds,

            # Споживання та вартість
            'energy_used_kwh': round(energy_kwh, 4),
            'cost_uah': round(cost_uah, 2),
            'avg_power': round(avg_power, 2),
            'daily_cost': round(daily_cost, 2),

            # Прогноз
            'monthly_energy_kwh': round(monthly_energy_kwh, 2),
            'monthly_cost_uah': round(monthly_cost_uah, 2),
            'yearly_energy_kwh': round(yearly_energy_kwh, 2),
            'yearly_cost_uah': round(yearly_cost_uah, 2),

            # Параметри (для відображення в вебі)
            'price_per_kwh': PRICE_PER_KWH,
            'base_power': BASE_POWER_WATTS,
            'max_power': MAX_POWER_WATTS
        })

    @app.route('/api/strategy/<int:strategy_id>/full_reset', methods=['POST'])
    @async_route
    async def api_full_reset_strategy(strategy_id):
        """Повне скидання стратегії з подальшим перезапуском"""
        if strategy_id in trading_engine.strategies:
            strategy = trading_engine.strategies[strategy_id]

            # Зупиняємо якщо запущена
            was_enabled = strategy.enabled
            if was_enabled:
                await trading_engine.stop_strategy(strategy_id)

            # Виконуємо повне скидання
            await strategy.reset()

            # Примусово скидаємо стан GridInstance
            if hasattr(strategy, 'grids'):
                for grid in strategy.grids.values():
                    grid.is_initialized = False
                    grid.lower_price = None
                    grid.upper_price = None
                    grid.grid_spacing = None
                    grid.active_buy_orders.clear()
                    grid.active_sell_orders.clear()
                    grid.price_history.clear()
                    grid.locked_balance = 0

            # Якщо була запущена - перезапускаємо
            if was_enabled:
                await trading_engine.start_strategy(strategy_id)

            return jsonify({'success': True, 'restarted': was_enabled})
        return jsonify({'error': 'Strategy not found'}), 404

    @app.route('/api/strategy/<int:strategy_id>/force_init', methods=['POST'])
    @async_route
    async def api_force_init_strategy(strategy_id):
        """Примусова ініціалізація сітки без очікування ціни"""
        if strategy_id in trading_engine.strategies:
            strategy = trading_engine.strategies[strategy_id]

            if strategy.name == 'grid' and hasattr(strategy, 'grids'):
                for symbol, grid in strategy.grids.items():
                    # Отримуємо поточну ціну
                    price = await trading_engine.exchange.get_current_price(symbol)
                    if price > 0:
                        # Примусово скидаємо стан
                        grid.is_initialized = False
                        grid.lower_price = None
                        grid.upper_price = None
                        grid.active_buy_orders.clear()
                        grid.active_sell_orders.clear()
                        # Ініціалізуємо
                        await grid.initialize_grid(price)
                        logger.info(f"Примусово ініціалізовано {symbol} за ціною ${price:.2f}")

                return jsonify({'success': True})
        return jsonify({'error': 'Strategy not found'}), 404

    @app.route('/api/strategy/<int:strategy_id>/status', methods=['GET'])
    @async_route
    async def api_strategy_status(strategy_id):
        """Детальний статус стратегії для діагностики"""
        if strategy_id in trading_engine.strategies:
            strategy = trading_engine.strategies[strategy_id]
            status = await strategy.get_status()

            # Додаємо деталі grid
            if strategy.name == 'grid' and hasattr(strategy, 'grids'):
                grid_details = {}
                for symbol, grid in strategy.grids.items():
                    grid_details[symbol] = {
                        'is_initialized': grid.is_initialized,
                        'lower_price': grid.lower_price,
                        'upper_price': grid.upper_price,
                        'buy_orders': len(grid.active_buy_orders),
                        'sell_orders': len(grid.active_sell_orders),
                        'locked_balance': grid.locked_balance,
                        'available_balance': grid.available_balance
                    }
                status['grid_details'] = grid_details

            return jsonify(status)
        return jsonify({'error': 'Strategy not found'}), 404

    # ============= Новинна стратегія API =============

    @app.route('/api/news_status')
    @async_route
    async def api_news_status():
        """Статус новинної стратегії"""
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'news':
                status = await strategy.get_status()
                # Отримуємо останні новини
                articles = []
                if hasattr(strategy, 'last_news') and strategy.last_news:
                    for article in strategy.last_news[:20]:
                        # Визначаємо сентимент для кожної новини
                        title = article.get('title', '').lower()
                        description = article.get('description', '').lower()
                        text = title + ' ' + (description or '')

                        positive_keywords = ['surge', 'rally', 'gain', 'positive', 'bullish', 'record', 'high',
                                             'upgrade', 'approve', 'adoption', 'breakthrough', 'soar', 'pump', 'moon',
                                             'green']
                        negative_keywords = ['drop', 'crash', 'fall', 'negative', 'bearish', 'low', 'decline', 'hack',
                                             'ban', 'scandal', 'fraud', 'crackdown', 'dump', 'red', 'sell', 'panic',
                                             'fud']

                        pos_score = sum(1 for kw in positive_keywords if kw in text)
                        neg_score = sum(1 for kw in negative_keywords if kw in text)

                        if pos_score > neg_score:
                            sentiment = 'positive'
                        elif neg_score > pos_score:
                            sentiment = 'negative'
                        else:
                            sentiment = 'neutral'

                        articles.append({
                            'title': article.get('title', ''),
                            'description': article.get('description', ''),
                            'url': article.get('url', ''),
                            'source': article.get('source', {}).get('name', 'Unknown'),
                            'publishedAt': article.get('publishedAt', ''),
                            'sentiment': sentiment
                        })

                return jsonify({
                    'sentiment': {
                        'overall': status.get('current_sentiment', 'neutral'),
                        'positive': sum(1 for a in articles if a['sentiment'] == 'positive'),
                        'neutral': sum(1 for a in articles if a['sentiment'] == 'neutral'),
                        'negative': sum(1 for a in articles if a['sentiment'] == 'negative')
                    },
                    'articles_count': status.get('last_news_count', 0),
                    'last_update': status.get('last_update'),
                    'articles': articles,
                    'api_key_configured': status.get('api_key_configured', False)
                })

        return jsonify({
            'sentiment': {'overall': 'Нейтральний', 'positive': 0, 'neutral': 0, 'negative': 0},
            'articles_count': 0,
            'last_update': None,
            'articles': [],
            'api_key_configured': False
        })

    @app.route('/api/news_settings', methods=['POST'])
    @async_route
    async def api_news_settings():
        """Збереження налаштувань новинної стратегії"""
        data = request.get_json()
        logger.info(f"📝 Отримано запит на збереження налаштувань новин: {data}")

        for strategy in trading_engine.strategies.values():
            if strategy.name == 'news':
                # Оновлюємо значення
                if 'symbols' in data:
                    strategy.symbols = data['symbols']
                    logger.info(f"   Оновлено symbols: {strategy.symbols}")
                if 'interval_minutes' in data:
                    strategy.interval_minutes = data['interval_minutes']
                    logger.info(f"   Оновлено interval_minutes: {strategy.interval_minutes}")
                if 'sensitivity' in data:
                    strategy.sensitivity = data['sensitivity']
                    logger.info(f"   Оновлено sensitivity: {strategy.sensitivity}")

                # Зберігаємо налаштування
                from config_manager import save_strategy_settings
                result = save_strategy_settings('news',
                                                symbols=strategy.symbols,
                                                interval_minutes=strategy.interval_minutes,
                                                sensitivity=strategy.sensitivity
                                                )
                logger.info(f"   Результат збереження: {result}")

                logger.info("INFO", "news",
                        f"Налаштування оновлено: символи={strategy.symbols}, інтервал={strategy.interval_minutes}, чутливість={strategy.sensitivity}")

                return jsonify({'success': True, 'settings': {
                    'symbols': strategy.symbols,
                    'interval_minutes': strategy.interval_minutes,
                    'sensitivity': strategy.sensitivity
                }})

        return jsonify({'error': 'Strategy not found'}), 404

    @app.route('/api/cancel_order', methods=['POST'])
    @async_route
    async def api_cancel_order():
        data = request.get_json()
        strategy_name = data.get('strategy')
        symbol = data.get('symbol')
        order_id = data.get('order_id')
        if not strategy_name or not order_id:
            return jsonify({'error': 'Missing parameters'}), 400
        for strategy in trading_engine.strategies.values():
            if strategy.name == strategy_name and hasattr(strategy, 'cancel_order'):
                success = await strategy.cancel_order(symbol, order_id)
                return jsonify({'success': success})
        return jsonify({'error': 'Strategy or method not found'}), 404

    # ============= Перезапуск =============

    @app.route('/api/restart', methods=['POST'])
    @async_route
    async def api_restart():
        """Перезапуск бота"""
        def restart():
            time.sleep(1)
            subprocess.Popen([sys.executable, "main.py"])
            sys.exit(0)

        threading.Thread(target=restart).start()
        return jsonify({'success': True})

    return app

