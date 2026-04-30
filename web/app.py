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
from database.db import get_db, get_price_history
from datetime import datetime
from web.hooks import register_webhook_routes
from web.webhook_routes import register_webhook_routes
from backend.api.routes_tech_analysis import tech_analysis_bp, init_trading_engine

logger = setup_logger('web')

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

    # Ініціалізуємо trading_engine для blueprint tech_analysis
    init_trading_engine(trading_engine)

    # ============= MIDDLEWARE ДЛЯ МЕТРИК API =============
    @app.before_request
    def before_request():
        request.start_time = time.time()

    @app.after_request
    def after_request(response):
        from monitoring.metrics import api_requests_counter, api_request_duration_histogram

        if hasattr(request, 'start_time'):
            duration = time.time() - request.start_time
            endpoint = request.endpoint or 'unknown'
            method = request.method

            api_requests_counter.labels(endpoint=endpoint, method=method, status=response.status_code).inc()
            api_request_duration_histogram.labels(endpoint=endpoint).observe(duration)

        return response

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
                if strategy.name == 'grid' and hasattr(strategy, 'grids'):
                    for grid in strategy.grids.values():
                        if not grid.is_initialized and not grid.active_buy_orders:
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
            was_enabled = strategy.enabled
            if was_enabled:
                await trading_engine.stop_strategy(strategy_id)
            await strategy.reset()
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

    @app.route('/api/trade_chart/<order_id>')
    @async_route
    async def api_trade_chart(order_id):
        """Отримання даних для графіка угоди"""
        try:
            logger.info(f"Запит графіка для ордера: {order_id}")

            from database.db import get_price_history
            saved_klines = get_price_history(order_id)

            with get_db() as conn:
                cursor = conn.execute(
                    "SELECT o.*, s.name as strategy_name FROM orders o "
                    "LEFT JOIN strategies s ON o.strategy_id = s.id "
                    "WHERE o.order_id = ?",
                    (order_id,)
                )
                order = cursor.fetchone()

                if not order:
                    return jsonify({'error': 'Order not found'}), 404

                order_dict = dict(order)
                strategy_name = order_dict.get('strategy_name', 'unknown')

                col_info = conn.execute("PRAGMA table_info(orders)").fetchall()
                has_closed_price = any(c[1] == 'closed_price' for c in col_info)

                tf_param = request.args.get('tf', '1')
                if saved_klines and len(saved_klines) > 0 and tf_param == '1':
                    entry_point = {
                        'price': float(order_dict['price']),
                        'timestamp': order_dict['opened_at'],
                        'quantity': float(order_dict['quantity'])
                    }

                    exit_point = None
                    if order_dict['status'] == 'closed':
                        closed_price = None
                        if has_closed_price and order_dict.get('closed_price'):
                            closed_price = float(order_dict['closed_price'])

                        if not closed_price and order_dict.get('pnl') is not None and order_dict.get('quantity'):
                            qty = float(order_dict['quantity'])
                            entry_p = float(order_dict['price'])
                            pnl = float(order_dict.get('pnl', 0))
                            commission = float(order_dict.get('commission', 0))
                            if qty > 0:
                                cost = qty * entry_p
                                revenue = cost + pnl + commission
                                closed_price = revenue / qty

                        if closed_price:
                            exit_point = {
                                'price': closed_price,
                                'timestamp': order_dict.get('closed_at') or order_dict['opened_at'],
                                'quantity': float(order_dict['quantity'])
                            }

                    return jsonify({
                        'order': order_dict,
                        'entry_point': entry_point,
                        'exit_point': exit_point,
                        'klines': saved_klines,
                        'symbol': order_dict['symbol'],
                        'from_db': True
                    })

                logger.info(f"Збережених свічок немає, завантажуємо з Bybit для угоди {order_id}")

                entry_point = None
                exit_point = None

                if strategy_name == 'scalp':
                    entry_point = {
                        'price': float(order_dict['price']),
                        'timestamp': order_dict['opened_at'],
                        'quantity': float(order_dict['quantity'])
                    }

                    if order_dict['status'] == 'closed':
                        closed_price = None
                        if has_closed_price and order_dict.get('closed_price'):
                            closed_price = float(order_dict['closed_price'])

                        if not closed_price and order_dict.get('pnl') is not None and order_dict.get('quantity'):
                            qty = float(order_dict['quantity'])
                            entry_p = float(order_dict['price'])
                            pnl = float(order_dict.get('pnl', 0))
                            commission = float(order_dict.get('commission', 0))
                            if qty > 0:
                                cost = qty * entry_p
                                revenue = cost + pnl + commission
                                closed_price = revenue / qty

                        if closed_price:
                            exit_point = {
                                'price': closed_price,
                                'timestamp': order_dict.get('closed_at') or order_dict['opened_at'],
                                'quantity': float(order_dict['quantity'])
                            }

                else:
                    pair_order = None
                    if order_dict.get('pair_id'):
                        cursor = conn.execute(
                            "SELECT * FROM orders WHERE pair_id = ? AND order_id != ?",
                            (order_dict['pair_id'], order_id)
                        )
                        pair_order = cursor.fetchone()
                        if pair_order:
                            pair_order = dict(pair_order)

                    entry_point = {
                        'price': float(order_dict['price']),
                        'timestamp': order_dict['opened_at'],
                        'quantity': float(order_dict['quantity'])
                    }

                    if order_dict['side'] == 'buy':
                        if pair_order and pair_order['side'] == 'sell':
                            exit_point = {
                                'price': float(pair_order['price']),
                                'timestamp': pair_order['opened_at'],
                                'quantity': float(pair_order['quantity'])
                            }
                    else:
                        if pair_order and pair_order['side'] == 'buy':
                            exit_point = {
                                'price': float(pair_order['price']),
                                'timestamp': pair_order['opened_at'],
                                'quantity': float(pair_order['quantity'])
                            }

                symbol = order_dict['symbol']
                from datetime import datetime, timezone

                tf = request.args.get('tf', '1')
                valid_tfs = {'1', '3', '5', '15', '30', '60', '120', '240', 'D'}
                if tf not in valid_tfs:
                    tf = '1'
                tf_minutes = {'1': 1, '3': 3, '5': 5, '15': 15, '30': 30,
                              '60': 60, '120': 120, '240': 240, 'D': 1440}.get(tf, 1)

                def parse_dt(ts_str):
                    if not ts_str:
                        return None
                    try:
                        dt = datetime.fromisoformat(str(ts_str).replace('Z', '+00:00'))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt
                    except Exception:
                        return None

                entry_dt = parse_dt(entry_point['timestamp'])
                if entry_dt is None:
                    return jsonify({'error': 'Не вдалося розпарсити час відкриття угоди'}), 400

                exit_dt = parse_dt(exit_point['timestamp']) if exit_point else None

                BEFORE_SECS = 30 * tf_minutes * 60
                AFTER_SECS = 30 * tf_minutes * 60

                start_ts_ms = int((entry_dt.timestamp() - BEFORE_SECS) * 1000)
                if exit_dt:
                    end_ts_ms = int((exit_dt.timestamp() + AFTER_SECS) * 1000)
                else:
                    end_ts_ms = int((entry_dt.timestamp() + 2 * tf_minutes * 3600 / 60) * 1000)

                duration_candles = (end_ts_ms - start_ts_ms) // (tf_minutes * 60000)
                needed = min(max(duration_candles + 20, 100), 1000)

                klines_raw = await trading_engine.exchange.get_klines(symbol, tf, limit=needed)
                klines_raw.sort(key=lambda k: k['timestamp'])

                filtered_klines = [
                    k for k in klines_raw
                    if start_ts_ms <= k['timestamp'] <= end_ts_ms
                ]

                if len(filtered_klines) < 5:
                    filtered_klines = klines_raw
                    out_of_range = True
                else:
                    out_of_range = False

                klines_out = []
                for k in filtered_klines:
                    k_copy = dict(k)
                    k_copy['time_iso'] = datetime.utcfromtimestamp(k['timestamp'] / 1000).strftime('%Y-%m-%dT%H:%M:%S')
                    klines_out.append(k_copy)

                return jsonify({
                    'order': order_dict,
                    'entry_point': entry_point,
                    'exit_point': exit_point,
                    'klines': klines_out,
                    'symbol': symbol,
                    'out_of_range': out_of_range,
                    'from_db': False
                })

        except Exception as e:
            logger.error(f"Помилка отримання даних графіка: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

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

    # ============= МЕТРИКИ ТА ГРАФІКИ API =============

    @app.route('/api/pnl_history')
    @async_route
    async def api_pnl_history():
        strategy = request.args.get('strategy', 'all')
        days = int(request.args.get('days', 7))

        from datetime import datetime, timedelta
        start_date = datetime.now() - timedelta(days=days)

        with get_db() as conn:
            if strategy == 'all':
                cursor = conn.execute("""
                    SELECT date(closed_at) as date, SUM(pnl) as daily_pnl
                    FROM orders 
                    WHERE status = 'closed' AND closed_at >= ?
                    GROUP BY date(closed_at)
                    ORDER BY date
                """, (start_date.isoformat(),))
            else:
                cursor = conn.execute("""
                    SELECT date(closed_at) as date, SUM(pnl) as daily_pnl
                    FROM orders o
                    JOIN strategies s ON o.strategy_id = s.id
                    WHERE o.status = 'closed' AND s.name = ? AND o.closed_at >= ?
                    GROUP BY date(closed_at)
                    ORDER BY date
                """, (strategy, start_date.isoformat()))

            rows = cursor.fetchall()
            history = []
            for row in rows:
                history.append({
                    'date': row['date'],
                    'pnl': row['daily_pnl'] or 0
                })

            return jsonify({'history': history})

    @app.route('/api/trades_distribution')
    @async_route
    async def api_trades_distribution():
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT s.name as strategy, COUNT(*) as count
                FROM orders o
                JOIN strategies s ON o.strategy_id = s.id
                WHERE o.status = 'closed'
                GROUP BY s.name
            """)
            rows = cursor.fetchall()
            return jsonify([dict(row) for row in rows])

    @app.route('/api/win_rate_stats')
    @async_route
    async def api_win_rate_stats():
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT 
                    s.name as strategy,
                    COUNT(CASE WHEN o.pnl > 0 THEN 1 END) as wins,
                    COUNT(CASE WHEN o.pnl <= 0 THEN 1 END) as losses,
                    COUNT(*) as total
                FROM orders o
                JOIN strategies s ON o.strategy_id = s.id
                WHERE o.status = 'closed'
                GROUP BY s.name
            """)
            rows = cursor.fetchall()
            result = []
            for row in rows:
                win_rate = (row['wins'] / row['total'] * 100) if row['total'] > 0 else 0
                result.append({
                    'strategy': row['strategy'],
                    'wins': row['wins'],
                    'losses': row['losses'],
                    'total': row['total'],
                    'win_rate': round(win_rate, 2)
                })
            return jsonify(result)

    @app.route('/api/balance_history')
    @async_route
    async def api_balance_history():
        days = int(request.args.get('days', 30))

        from datetime import datetime, timedelta
        start_date = datetime.now() - timedelta(days=days)

        with get_db() as conn:
            cursor = conn.execute("""
                SELECT 
                    date(closed_at) as date,
                    SUM(pnl) as daily_pnl
                FROM orders 
                WHERE status = 'closed' AND closed_at >= ?
                GROUP BY date(closed_at)
                ORDER BY date
            """, (start_date.isoformat(),))

            rows = cursor.fetchall()

            balance = 100.0
            history = []
            for row in rows:
                balance += row['daily_pnl'] or 0
                history.append({
                    'date': row['date'],
                    'balance': round(balance, 2),
                    'daily_pnl': round(row['daily_pnl'] or 0, 2)
                })

            return jsonify({'history': history})

    # ============= ЛОГИ API (БД) =============

    @app.route('/api/logs')
    @async_route
    async def api_logs():
        from database.db import get_logs, get_logs_count

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        module = request.args.get('module', 'all')
        level = request.args.get('level', 'all')

        if per_page > 100:
            per_page = 100
        if page < 1:
            page = 1

        offset = (page - 1) * per_page
        total = get_logs_count(module, level)

        logs = get_logs(module, level, per_page, offset)

        return jsonify({
            'logs': logs,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page if total > 0 else 1
        })

    @app.route('/api/log_modules')
    @async_route
    async def api_log_modules():
        from database.db import get_db

        with get_db() as conn:
            cursor = conn.execute("SELECT DISTINCT module FROM logs ORDER BY module")
            modules = [row['module'] for row in cursor.fetchall()]

        from database.db import get_log_settings
        settings = get_log_settings()
        for module in settings.keys():
            if module not in modules:
                modules.append(module)

        return jsonify(sorted(modules))

    @app.route('/api/log_settings')
    @async_route
    async def api_log_settings():
        from database.db import get_log_settings, get_log_retention_days

        return jsonify({
            'modules': get_log_settings(),
            'retention_days': get_log_retention_days()
        })

    @app.route('/api/log_settings', methods=['POST'])
    @async_route
    async def api_update_log_settings():
        from database.db import update_log_settings, set_log_retention_days
        from utils.logger_utils import update_log_level

        data = request.get_json()

        if 'module' in data and 'level' in data:
            success = update_log_level(data['module'], data['level'])
            return jsonify({'success': success})

        if 'retention_days' in data:
            days = int(data['retention_days'])
            if 1 <= days <= 90:
                set_log_retention_days(days)
                return jsonify({'success': True, 'retention_days': days})

        return jsonify({'error': 'Invalid parameters'}), 400

    @app.route('/api/clear_logs', methods=['POST'])
    @async_route
    async def api_clear_logs():
        try:
            with get_db() as conn:
                conn.execute("DELETE FROM logs")
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/cleanup_logs', methods=['POST'])
    @async_route
    async def api_cleanup_logs():
        from database.db import cleanup_old_logs

        data = request.get_json()
        days = data.get('retention_days', 7)

        deleted = cleanup_old_logs(days)
        return jsonify({'success': True, 'deleted': deleted})

    # ============= Grid API =============

    @app.route('/api/grid_levels/<symbol>')
    @async_route
    async def api_grid_levels_symbol(symbol):
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'grid' and hasattr(strategy, 'get_grid_levels_for_symbol'):
                data = await strategy.get_grid_levels_for_symbol(symbol)
                return jsonify(data)
        return jsonify({'error': 'Grid strategy not found'}), 404

    @app.route('/api/grid_settings', methods=['POST'])
    @async_route
    async def api_grid_settings():
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

    @app.route('/api/scalp_settings', methods=['POST'])
    @async_route
    async def api_scalp_settings():
        data = request.get_json()
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'scalp' and hasattr(strategy, 'update_settings'):
                await strategy.update_settings(
                    symbols=data.get('symbols'),
                    timeframe=data.get('timeframe'),
                    trade_size_usdt=data.get('trade_size_usdt'),
                    take_profit_percent=data.get('take_profit_percent'),
                    stop_loss_percent=data.get('stop_loss_percent'),
                    trailing_stop_percent=data.get('trailing_stop_percent')
                )
                return jsonify({'success': True})
        return jsonify({'error': 'Strategy not found'}), 404

    @app.route('/api/dashboard/settings', methods=['GET'])
    @async_route
    async def api_dashboard_settings():
        from config_manager import get_dashboard_settings
        settings = get_dashboard_settings()
        return jsonify(settings)

    @app.route('/api/dashboard/settings', methods=['POST'])
    @async_route
    async def api_update_dashboard_settings():
        from config_manager import save_dashboard_settings

        data = request.get_json()
        display_symbols = data.get('display_symbols')
        refresh_interval = data.get('refresh_interval')
        show_24h_change = data.get('show_24h_change')

        if display_symbols:
            display_symbols = [s.strip().upper() for s in display_symbols if s.strip()]

        success = save_dashboard_settings(
            display_symbols=display_symbols,
            refresh_interval=refresh_interval,
            show_24h_change=show_24h_change
        )

        return jsonify({'success': success})

    @app.route('/api/crypto/prices')
    @async_route
    async def api_crypto_prices():
        from config_manager import get_dashboard_settings

        settings = get_dashboard_settings()
        symbols = settings.get('display_symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])

        prices = {}
        for symbol in symbols:
            try:
                current_price = await trading_engine.exchange.get_current_price(symbol)

                change_24h = 0
                klines = await trading_engine.exchange.get_klines(symbol, 'D', limit=2)
                if len(klines) >= 2:
                    yesterday_close = klines[-2]['close']
                    if yesterday_close > 0:
                        change_24h = ((current_price - yesterday_close) / yesterday_close) * 100

                prices[symbol] = {
                    'price': current_price,
                    'change_24h': round(change_24h, 2),
                    'symbol': symbol,
                    'name': symbol.replace('USDT', '')
                }
            except Exception as e:
                logger.error(f"Помилка отримання ціни {symbol}: {e}")
                prices[symbol] = {
                    'price': 0,
                    'change_24h': 0,
                    'symbol': symbol,
                    'name': symbol.replace('USDT', ''),
                    'error': str(e)
                }

        return jsonify(prices)

    # ============= Сигнали API =============

    @app.route('/api/signals/add', methods=['POST'])
    @async_route
    async def api_add_signal():
        data = request.get_json()

        signals_strategy = None
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'signals':
                signals_strategy = strategy
                break

        if not signals_strategy:
            return jsonify({'error': 'Signals strategy not found'}), 404

        if 'text' in data:
            parsed = signals_strategy.parse_signal_text(data['text'])
            if not parsed:
                return jsonify({'error': 'Failed to parse signal text'}), 400
            signal_data = parsed
            signal_data['trade_size_usdt'] = data.get('trade_size_usdt', 20)
        else:
            signal_data = {
                'symbol': data.get('symbol'),
                'signal_type': data.get('signal_type'),
                'entry_price': data.get('entry_price'),
                'entry_limit': data.get('entry_limit'),
                'stop_loss': data.get('stop_loss'),
                'take_profits': data.get('take_profits', []),
                'trade_size_usdt': data.get('trade_size_usdt', 20)
            }

        required_fields = ['symbol', 'signal_type', 'entry_price', 'stop_loss', 'take_profits']
        for field in required_fields:
            if not signal_data.get(field):
                return jsonify({'error': f'Missing field: {field}'}), 400

        signal = await signals_strategy.add_signal(signal_data)
        if signal:
            return jsonify({
                'success': True,
                'signal_id': signal.id,
                'message': f'Signal added: {signal.signal_type} {signal.symbol}'
            })
        else:
            return jsonify({'error': 'Failed to add signal'}), 500

    @app.route('/api/signals/close/<signal_id>', methods=['POST'])
    @async_route
    async def api_close_signal(signal_id):
        try:
            data = {}
            if request.is_json:
                data = request.get_json() or {}

            price = data.get('price')

            signals_strategy = None
            for strategy in trading_engine.strategies.values():
                if strategy.name == 'signals':
                    signals_strategy = strategy
                    break

            if not signals_strategy:
                return jsonify({'error': 'Signals strategy not found'}), 404

            success = await signals_strategy.manual_close(signal_id, price)

            if success:
                return jsonify({'success': True, 'message': 'Position closed'})
            else:
                return jsonify({'error': 'Signal not found or already closed'}), 404

        except Exception as e:
            logger.error(f"Помилка api_close_signal: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/signals/active')
    @async_route
    async def api_get_active_signals():
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'signals':
                status = await strategy.get_status()
                return jsonify(status.get('active_signals', []))
        return jsonify([])

    @app.route('/api/signals/history')
    @async_route
    async def api_get_signals_history():
        limit = request.args.get('limit', 50, type=int)

        from database.db import get_db

        try:
            with get_db() as conn:
                strategy = conn.execute(
                    "SELECT id FROM strategies WHERE name = 'signals'"
                ).fetchone()

                if not strategy:
                    return jsonify([])

                signals = conn.execute("""
                    SELECT * FROM signals
                    WHERE strategy_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (strategy['id'], limit)).fetchall()

                result = []
                for s in signals:
                    row = dict(s)
                    if row.get('take_profits'):
                        if isinstance(row['take_profits'], str):
                            row['take_profits'] = [float(x) for x in row['take_profits'].split(',') if x]
                        elif row['take_profits'] is None:
                            row['take_profits'] = []
                    for key in ['entry_price', 'stop_loss', 'trade_size_usdt']:
                        if key in row and row[key] is not None:
                            row[key] = float(row[key])
                    if 'created_at' in row and row['created_at']:
                        row['created_at'] = str(row['created_at'])
                    row['order_pnl'] = None
                    row['closed_price'] = None
                    result.append(row)

                return jsonify(result)

        except Exception as e:
            logger.error(f"Помилка в api_get_signals_history: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    @app.route('/api/signals/status')
    @async_route
    async def api_signals_status():
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'signals':
                status = await strategy.get_status()
                return jsonify(status)
        return jsonify({'error': 'Strategy not found'}), 404

    # ============= МОНІТОРИНГ ЕЛЕКТРОЕНЕРГІЇ API =============

    @app.route('/api/power/status')
    @async_route
    async def api_power_status():
        try:
            from monitoring.power_monitor import power_monitor

            if not power_monitor.running:
                return jsonify({
                    'current_power_watts': 0,
                    'uptime_seconds': 0,
                    'uptime_hours': 0,
                    'session_id': None,
                    'session_energy_kwh': 0,
                    'session_cost_uah': 0,
                    'total_energy_kwh': 0,
                    'total_cost_uah': 0,
                    'total_hours': 0,
                    'month_energy_kwh': 0,
                    'month_cost_uah': 0,
                    'year_energy_kwh': 0,
                    'year_cost_uah': 0
                })

            stats = await power_monitor.get_current_stats()
            return jsonify(stats)
        except Exception as e:
            logger.error(f"Помилка power/status: {e}")
            return jsonify({
                'current_power_watts': 0,
                'uptime_seconds': 0,
                'uptime_hours': 0,
                'session_id': None,
                'session_energy_kwh': 0,
                'session_cost_uah': 0,
                'total_energy_kwh': 0,
                'total_cost_uah': 0,
                'total_hours': 0,
                'month_energy_kwh': 0,
                'month_cost_uah': 0,
                'year_energy_kwh': 0,
                'year_cost_uah': 0
            })

    @app.route('/api/power/history')
    @async_route
    async def api_power_history():
        days = request.args.get('days', 30, type=int)
        from database.power_monitor_db import get_power_history
        history = get_power_history(days)
        return jsonify(history)

    @app.route('/api/power/settings', methods=['GET'])
    @async_route
    async def api_power_settings_get():
        from database.power_monitor_db import get_power_settings
        settings = get_power_settings()
        return jsonify(settings)

    @app.route('/api/power/settings', methods=['POST'])
    @async_route
    async def api_power_settings_update():
        data = request.get_json()
        from database.power_monitor_db import update_power_settings
        from monitoring.power_monitor import power_monitor

        update_power_settings(data)
        power_monitor._load_settings()

        return jsonify({'success': True})

    @app.route('/api/grid/force_rebuild', methods=['POST'])
    @async_route
    async def api_grid_force_rebuild():
        data = request.get_json()
        symbol = data.get('symbol')

        for strategy in trading_engine.strategies.values():
            if strategy.name == 'grid' and hasattr(strategy, 'grids'):
                if symbol in strategy.grids:
                    grid = strategy.grids[symbol]
                    price = await trading_engine.exchange.get_current_price(symbol)
                    if price > 0:
                        for oid in list(grid.active_buy_orders.keys()):
                            await grid.cancel_order(oid)
                        for oid in list(grid.active_sell_orders.keys()):
                            await grid.cancel_order(oid)
                        grid.is_initialized = False
                        await grid.initialize_grid(price)
                        return jsonify({'success': True})
        return jsonify({'error': 'Grid not found'}), 404

    # ============= База даних API =============

    @app.route('/api/db_tables_list')
    @async_route
    async def api_db_tables_list():
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
        try:
            with get_db() as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = cursor.fetchall()
                tables_info = []
                for table in tables:
                    table_name = table['name']
                    count_cursor = conn.execute(f"SELECT COUNT(*) as count FROM {table_name}")
                    row_count = count_cursor.fetchone()['count']
                    try:
                        last_cursor = conn.execute(
                            f"SELECT MAX(id) as last_id, MAX(opened_at) as last_date FROM {table_name}")
                        last_row = last_cursor.fetchone()
                        last_updated = last_row['last_date'] if last_row and last_row['last_date'] else None
                    except:
                        last_updated = None
                    tables_info.append({
                        'name': table_name,
                        'row_count': row_count,
                        'last_updated': last_updated,
                        'columns': []
                    })
                return jsonify(tables_info)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/db_table_info/<table_name>')
    @async_route
    async def api_db_table_info(table_name):
        allowed_tables = ['orders', 'strategies', 'balances', 'logs', 'system_monitor']
        if table_name not in allowed_tables:
            return jsonify({'error': 'Table not allowed'}), 403
        try:
            with get_db() as conn:
                cursor = conn.execute(f"PRAGMA table_info({table_name})")
                columns = [{'name': col[1], 'type': col[2]} for col in cursor.fetchall()]
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
        allowed_tables = ['orders', 'strategies', 'balances', 'logs', 'system_monitor']
        if table_name not in allowed_tables:
            return jsonify({'error': 'Table not allowed'}), 403
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        search = request.args.get('search', '')
        sort_by = request.args.get('sort_by', 'id')
        sort_order = request.args.get('sort_order', 'desc')
        if per_page > 100:
            per_page = 100
        if page < 1:
            page = 1
        try:
            with get_db() as conn:
                allowed_sort_columns = ['id', 'opened_at', 'closed_at', 'price', 'pnl', 'timestamp']
                if sort_by not in allowed_sort_columns:
                    sort_by = 'id'
                sort_dir = 'DESC' if sort_order == 'desc' else 'ASC'
                query = f"SELECT * FROM {table_name}"
                params = []
                if search:
                    query += f" WHERE CAST(id as TEXT) LIKE ? OR CAST(symbol as TEXT) LIKE ? OR CAST(side as TEXT) LIKE ?"
                    search_pattern = f"%{search}%"
                    params = [search_pattern, search_pattern, search_pattern]
                count_query = f"SELECT COUNT(*) as total FROM {table_name}"
                if search:
                    count_query += f" WHERE CAST(id as TEXT) LIKE ? OR CAST(symbol as TEXT) LIKE ? OR CAST(side as TEXT) LIKE ?"
                count_cursor = conn.execute(count_query, params)
                total_rows = count_cursor.fetchone()['total']
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

    # ============= Свічки API =============

    @app.route('/api/klines/<symbol>')
    @async_route
    async def api_klines(symbol):
        interval = request.args.get('interval', '1')
        limit = request.args.get('limit', 100, type=int)
        klines = await trading_engine.exchange.get_klines(symbol, interval, limit)
        return jsonify(klines)

    # ============= Режим роботи API =============

    @app.route('/api/mode')
    @async_route
    async def api_get_mode():
        return jsonify({'mode': trading_engine.config.DEFAULT_MODE})

    @app.route('/api/mode', methods=['POST'])
    @async_route
    async def api_set_mode():
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
            if not trading_engine.config.BYBIT_API_KEY or not trading_engine.config.BYBIT_API_SECRET:
                return jsonify({'error': 'API ключі Bybit не налаштовані'}), 400
            balance = await trading_engine.exchange.get_real_balance('USDT')
            if balance < 10:
                return jsonify(
                    {'error': f'Недостатньо балансу для реальної торгівлі. Доступно: ${balance:.2f}'}), 400
            logger.warning(f"🟢 ПЕРЕХІД У РЕАЛЬНИЙ РЕЖИМ! Баланс: ${balance:.2f}")
        trading_engine.config.DEFAULT_MODE = new_mode
        trading_engine.exchange.mode = new_mode
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

    @app.route('/api/news_status')
    @async_route
    async def api_news_status():
        try:
            for strategy in trading_engine.strategies.values():
                if strategy.name == 'news':
                    status = await strategy.get_status()
                    articles = []

                    if hasattr(strategy, 'last_news') and strategy.last_news:
                        for article in strategy.last_news[:20]:
                            title = article.get('title', '').lower()
                            description = article.get('description', '').lower()
                            text = title + ' ' + (description or '')

                            positive_keywords = ['surge', 'rally', 'gain', 'positive', 'bullish', 'record', 'high',
                                                 'upgrade', 'approve', 'adoption', 'breakthrough', 'soar', 'pump',
                                                 'moon', 'green']
                            negative_keywords = ['drop', 'crash', 'fall', 'negative', 'bearish', 'low', 'decline',
                                                 'hack', 'ban', 'scandal', 'fraud', 'crackdown', 'dump', 'red',
                                                 'sell', 'panic', 'fud']

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

                    sentiment_history = []
                    try:
                        from database.db import get_sentiment_history
                        sentiment_history = get_sentiment_history(limit=50)
                        sentiment_history.reverse()
                    except Exception as e:
                        logger.warning(f"Не вдалося отримати історію сентименту: {e}")

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
                        'api_key_configured': status.get('api_key_configured', False),
                        'sentiment_history': sentiment_history
                    })
        except Exception as e:
            logger.error(f"Помилка в api_news_status: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e), 'api_key_configured': False, 'sentiment_history': []}), 500

        return jsonify({
            'sentiment': {'overall': 'neutral', 'positive': 0, 'neutral': 0, 'negative': 0},
            'articles_count': 0,
            'last_update': None,
            'articles': [],
            'api_key_configured': False,
            'sentiment_history': []
        })

    @app.route('/api/scalp/force_close/<symbol>', methods=['POST'])
    @async_route
    async def api_scalp_force_close(symbol):
        logger.info(f"Запит на примусове закриття позиції {symbol}")

        for strategy in trading_engine.strategies.values():
            if strategy.name == 'scalp':
                if hasattr(strategy, 'force_close_position'):
                    result = await strategy.force_close_position(symbol)
                    return jsonify(result)
                else:
                    return jsonify({'success': False, 'error': 'Method force_close_position not found'}), 500

        return jsonify({'success': False, 'error': 'Scalp strategy not found'}), 404

    @app.route('/api/news_settings', methods=['POST'])
    @async_route
    async def api_news_settings():
        data = request.get_json()
        logger.info(f"📝 Отримано запит на збереження налаштувань новин: {data}")
        for strategy in trading_engine.strategies.values():
            if strategy.name == 'news':
                if 'symbols' in data:
                    strategy.symbols = data['symbols']
                if 'interval_minutes' in data:
                    strategy.interval_minutes = data['interval_minutes']
                if 'sensitivity' in data:
                    strategy.sensitivity = data['sensitivity']
                from config_manager import save_strategy_settings
                save_strategy_settings('news',
                                       symbols=strategy.symbols,
                                       interval_minutes=strategy.interval_minutes,
                                       sensitivity=strategy.sensitivity)
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

    # ============= БЕКТЕСТИНГ API =============

    @app.route('/api/backtest/grid', methods=['POST'])
    @async_route
    async def api_backtest_grid():
        from trading.backtest import GridBacktest

        data = request.get_json()

        symbol = data.get('symbol', 'BTCUSDT')
        start_date_str = data.get('start_date')
        end_date_str = data.get('end_date')
        initial_balance = data.get('initial_balance', 100.0)
        grid_levels = data.get('grid_levels', 10)
        order_size_usdt = data.get('order_size_usdt', 50)
        lower_percent = data.get('lower_percent', 20)
        upper_percent = data.get('upper_percent', 20)
        interval = data.get('interval', '15')

        logger.info(f"Бектест отримано: start={start_date_str}, end={end_date_str}")

        try:
            if 'T' in start_date_str:
                start_date = datetime.fromisoformat(start_date_str)
            else:
                start_date = datetime.fromisoformat(start_date_str + 'T00:00:00')

            if 'T' in end_date_str:
                end_date = datetime.fromisoformat(end_date_str)
            else:
                end_date = datetime.fromisoformat(end_date_str + 'T23:59:59')

        except ValueError as e:
            logger.error(f"Помилка парсингу дат: {e}")
            return jsonify({'error': f'Invalid date format. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS'}), 400

        if start_date >= end_date:
            return jsonify({'error': 'Start date must be before end date'}), 400

        max_days = 90
        if (end_date - start_date).days > max_days:
            return jsonify({'error': f'Date range cannot exceed {max_days} days'}), 400

        backtest = GridBacktest(trading_engine.exchange)

        try:
            result = await backtest.run_backtest(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                initial_balance=initial_balance,
                grid_levels=grid_levels,
                order_size_usdt=order_size_usdt,
                lower_percent=lower_percent,
                upper_percent=upper_percent,
                interval=interval
            )

            return jsonify(result.get_summary())
        except Exception as e:
            logger.error(f"Помилка бектесту: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    @app.route('/api/backtest/grid/optimize', methods=['POST'])
    @async_route
    async def api_backtest_grid_optimize():
        from trading.backtest import GridBacktest

        data = request.get_json()

        symbol = data.get('symbol', 'BTCUSDT')
        start_date_str = data.get('start_date')
        end_date_str = data.get('end_date')
        initial_balance = data.get('initial_balance', 100.0)

        logger.info(f"Оптимізація отримана: start={start_date_str}, end={end_date_str}")

        try:
            if 'T' in start_date_str:
                start_date = datetime.fromisoformat(start_date_str)
            else:
                start_date = datetime.fromisoformat(start_date_str + 'T00:00:00')

            if 'T' in end_date_str:
                end_date = datetime.fromisoformat(end_date_str)
            else:
                end_date = datetime.fromisoformat(end_date_str + 'T23:59:59')

        except ValueError as e:
            logger.error(f"Помилка парсингу дат: {e}")
            return jsonify({'error': f'Invalid date format. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS'}), 400

        if start_date >= end_date:
            return jsonify({'error': 'Start date must be before end date'}), 400

        max_days = 30
        if (end_date - start_date).days > max_days:
            return jsonify({'error': f'Optimization date range cannot exceed {max_days} days'}), 400

        backtest = GridBacktest(trading_engine.exchange)

        try:
            results = await backtest.optimize_parameters(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                initial_balance=initial_balance
            )

            return jsonify({
                'success': True,
                'results': results[:20],
                'total_tested': len(results)
            })
        except Exception as e:
            logger.error(f"Помилка оптимізації: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    # ============= ДІАГНОСТИКА API =============

    @app.route('/api/health/status')
    @async_route
    async def api_health_status():
        import psutil
        import time
        from pathlib import Path
        from database.db import get_db

        result = {
            'timestamp': time.time(),
            'overall': 'healthy',
            'components': {}
        }

        try:
            with get_db() as conn:
                size = Path('trading_bot.db').stat().st_size if Path('trading_bot.db').exists() else 0
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                result['components']['database'] = {
                    'status': 'healthy',
                    'size_mb': round(size / 1024 / 1024, 2),
                    'tables': len(tables),
                    'message': f"{len(tables)} таблиць, {round(size / 1024 / 1024, 2)} MB"
                }
        except Exception as e:
            result['components']['database'] = {'status': 'error', 'message': str(e)}
            result['overall'] = 'degraded'

        try:
            if trading_engine and trading_engine.exchange:
                prices = trading_engine.exchange.current_prices
                has_prices = len(prices) > 0
                ws_connected = len(trading_engine.exchange.ws_connections) > 0 if hasattr(trading_engine.exchange,
                                                                                          'ws_connections') else False

                result['components']['bybit'] = {
                    'status': 'healthy' if has_prices else 'warning',
                    'prices_count': len(prices),
                    'websocket': 'connected' if ws_connected else 'disconnected',
                    'message': f"Цін: {len(prices)}, WebSocket: {'✅' if ws_connected else '❌'}"
                }
            else:
                result['components']['bybit'] = {'status': 'error', 'message': 'Exchange not initialized'}
                result['overall'] = 'degraded'
        except Exception as e:
            result['components']['bybit'] = {'status': 'error', 'message': str(e)}
            result['overall'] = 'degraded'

        try:
            if trading_engine and hasattr(trading_engine, 'telegram_bot') and trading_engine.telegram_bot:
                is_running = getattr(trading_engine.telegram_bot, '_running', False)
                has_token = bool(trading_engine.config.TELEGRAM_BOT_TOKEN)

                if has_token and is_running:
                    result['components']['telegram'] = {'status': 'healthy', 'message': 'Бот активний'}
                elif has_token and not is_running:
                    result['components']['telegram'] = {'status': 'warning', 'message': 'Бот не запущено'}
                else:
                    result['components']['telegram'] = {'status': 'error', 'message': 'Токен відсутній'}
                    result['overall'] = 'degraded'
            else:
                result['components']['telegram'] = {'status': 'error', 'message': 'Telegram бот не ініціалізовано'}
        except Exception as e:
            result['components']['telegram'] = {'status': 'error', 'message': str(e)}

        try:
            news_strategy = None
            for strategy in trading_engine.strategies.values():
                if strategy.name == 'news':
                    news_strategy = strategy
                    break

            if news_strategy:
                api_key_configured = getattr(news_strategy, 'news_api_key', False)
                last_news_count = getattr(news_strategy, 'articles_count', 0)

                if api_key_configured:
                    result['components']['news_api'] = {
                        'status': 'healthy',
                        'last_news': last_news_count,
                        'message': f"API ключ є, останніх новин: {last_news_count}"
                    }
                else:
                    result['components']['news_api'] = {'status': 'error', 'message': 'API ключ відсутній'}
                    result['overall'] = 'degraded'
            else:
                result['components']['news_api'] = {'status': 'warning', 'message': 'News стратегія не знайдена'}
        except Exception as e:
            result['components']['news_api'] = {'status': 'error', 'message': str(e)}

        try:
            from web.websocket_server import connected_clients
            clients_count = len(connected_clients) if 'connected_clients' in dir() else 0
            result['components']['websocket'] = {
                'status': 'healthy' if clients_count > 0 else 'warning',
                'clients': clients_count,
                'message': f"Клієнтів: {clients_count}"
            }
        except:
            result['components']['websocket'] = {'status': 'unknown', 'message': 'Не вдалося перевірити'}

        try:
            result['components']['web_server'] = {
                'status': 'healthy',
                'uptime': time.time() - getattr(trading_engine, 'start_time', time.time()),
                'message': 'Працює'
            }
        except:
            result['components']['web_server'] = {'status': 'healthy', 'message': 'Працює'}

        try:
            cpu = psutil.cpu_percent(interval=0.5)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent

            cpu_status = 'healthy' if cpu < 80 else ('warning' if cpu < 95 else 'error')
            ram_status = 'healthy' if ram < 80 else ('warning' if ram < 95 else 'error')

            result['components']['system'] = {
                'status': cpu_status if cpu_status == ram_status else 'warning',
                'cpu': round(cpu, 1),
                'cpu_status': cpu_status,
                'ram': round(ram, 1),
                'ram_status': ram_status,
                'disk': round(disk, 1),
                'message': f"CPU: {cpu}%, RAM: {ram}%, Disk: {disk}%"
            }
            if cpu > 95 or ram > 95:
                result['overall'] = 'degraded'
        except Exception as e:
            result['components']['system'] = {'status': 'error', 'message': str(e)}

        strategies_status = []
        for strategy in trading_engine.strategies.values():
            status = await strategy.get_status()
            strategies_status.append({
                'name': status['name'],
                'enabled': status['enabled'],
                'status': 'healthy' if status['enabled'] and not status.get('is_blocked') else (
                    'warning' if not status['enabled'] else 'error'),
                'message': f"{'✅ Активна' if status['enabled'] else '❌ Зупинена'}"
            })
        result['components']['strategies'] = strategies_status

        try:
            from database.db import get_db
            from datetime import datetime, timedelta

            with get_db() as conn:
                yesterday = (datetime.now() - timedelta(days=1)).isoformat()
                errors = conn.execute(
                    "SELECT COUNT(*) as count FROM logs WHERE level = 'ERROR' AND timestamp > ?",
                    (yesterday,)
                ).fetchone()
                warnings = conn.execute(
                    "SELECT COUNT(*) as count FROM logs WHERE level = 'WARNING' AND timestamp > ?",
                    (yesterday,)
                ).fetchone()

                error_count = errors['count'] if errors else 0
                warning_count = warnings['count'] if warnings else 0

                log_status = 'healthy' if error_count == 0 else ('warning' if error_count < 10 else 'error')
                result['components']['logs'] = {
                    'status': log_status,
                    'errors_24h': error_count,
                    'warnings_24h': warning_count,
                    'message': f"Помилок: {error_count}, Попереджень: {warning_count}"
                }
                if error_count > 0:
                    result['overall'] = 'degraded' if error_count < 10 else 'unhealthy'
        except Exception as e:
            result['components']['logs'] = {'status': 'error', 'message': str(e)}

        if result['overall'] == 'healthy':
            result['message'] = '✅ Всі системи працюють нормально'
        elif result['overall'] == 'degraded':
            result['message'] = '⚠️ Деякі системи мають проблеми'
        else:
            result['message'] = '🔴 Критичні проблеми!'

        return jsonify(result)

    @app.route('/api/health/test')
    @async_route
    async def api_health_test():
        component = request.args.get('component', 'all')

        results = {}

        if component == 'all' or component == 'database':
            try:
                from database.db import get_db
                with get_db() as conn:
                    conn.execute("SELECT 1")
                results['database'] = {'status': 'ok', 'message': 'Підключення успішне'}
            except Exception as e:
                results['database'] = {'status': 'error', 'message': str(e)}

        if component == 'all' or component == 'bybit':
            try:
                price = await trading_engine.exchange.get_current_price('BTCUSDT')
                results['bybit'] = {'status': 'ok', 'message': f'BTCUSDT: ${price:.2f}'}
            except Exception as e:
                results['bybit'] = {'status': 'error', 'message': str(e)}

        if component == 'all' or component == 'telegram':
            try:
                bot = trading_engine.telegram_bot
                if bot and bot._running:
                    results['telegram'] = {'status': 'ok', 'message': 'Бот активний'}
                else:
                    results['telegram'] = {'status': 'error', 'message': 'Бот не активний'}
            except Exception as e:
                results['telegram'] = {'status': 'error', 'message': str(e)}

        if component == 'all' or component == 'websocket':
            try:
                from web.websocket_server import connected_clients
                results['websocket'] = {'status': 'ok', 'message': f'Клієнтів: {len(connected_clients)}'}
            except Exception as e:
                results['websocket'] = {'status': 'error', 'message': str(e)}

        return jsonify(results)

    # ============= ТЕХНІЧНИЙ АНАЛІЗ API (BLUEPRINT) =============
    # Реєструємо blueprint для технічного аналізу
    app.register_blueprint(tech_analysis_bp)

    # ============= PROMETHEUS METRICS API =============

    @app.route('/metrics')
    async def metrics_endpoint():
        from monitoring.metrics import get_metrics
        return get_metrics(), 200, {'Content-Type': 'text/plain'}

    @app.route('/api/metrics')
    @async_route
    async def api_metrics():
        from monitoring.metrics import get_metrics_json, metrics_collector
        import psutil

        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            ram_percent = psutil.virtual_memory().percent
            ram_used = psutil.virtual_memory().used / (1024 ** 3)
            disk_percent = psutil.disk_usage('/').percent

            metrics_collector.update_system_metrics(cpu_percent, ram_percent, ram_used, disk_percent)

            for strategy in trading_engine.strategies.values():
                status = await strategy.get_status()
                metrics_collector.update_trading_metrics(strategy.name, status, trading_engine.config.DEFAULT_MODE)

                if strategy.name == 'grid' and hasattr(strategy, 'grids'):
                    for symbol, grid in strategy.grids.items():
                        grid_status = grid.get_status()
                        metrics_collector.update_grid_metrics(symbol, grid_status)

                if strategy.name == 'scalp' and hasattr(strategy, 'open_positions'):
                    for symbol in strategy.symbols:
                        has_position = symbol in strategy.open_positions
                        metrics_collector.update_scalp_metrics(symbol, has_position, 0)

                if strategy.name == 'news':
                    metrics_collector.update_news_metrics(
                        status.get('current_sentiment', 'neutral'),
                        status.get('last_news_count', 0)
                    )

            for symbol in trading_engine.config.SYMBOLS:
                price = await trading_engine.exchange.get_current_price(symbol)
                if price > 0:
                    metrics_collector.update_market_metrics(symbol, price)

        except Exception as e:
            logger.error(f"Помилка оновлення метрик: {e}")

        return jsonify(get_metrics_json())

    @app.route('/api/system_status')
    @async_route
    async def api_system_status():
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

    # ============= Перезапуск =============

    @app.route('/api/restart', methods=['POST'])
    @async_route
    async def api_restart():
        def restart():
            time.sleep(1)
            subprocess.Popen([sys.executable, "main.py"])
            sys.exit(0)

        threading.Thread(target=restart).start()
        return jsonify({'success': True})

    # ВЕБ-ХУКИ
    register_webhook_routes(app, trading_engine)

    return app