import logging
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from functools import wraps
import asyncio
from database.db import get_db

logger = logging.getLogger(__name__)

tech_analysis_bp = Blueprint('tech_analysis', __name__, url_prefix='/api/tech_analysis')

# Глобальний екземпляр trading_engine
_trading_engine = None
_tech_strategy = None  # запасний варіант


def init_trading_engine(engine):
    """Ініціалізація trading_engine для доступу до стратегій"""
    global _trading_engine
    _trading_engine = engine
    logger.info("Trading engine initialized for tech_analysis blueprint")


def init_tech_strategy(strategy):
    """Ініціалізація стратегії (запасний варіант)"""
    global _tech_strategy
    _tech_strategy = strategy
    logger.info("Tech strategy initialized directly")


def get_tech_strategy():
    """Отримання стратегії з trading_engine"""
    # Спершу пробуємо отримати з trading_engine
    if _trading_engine and hasattr(_trading_engine, 'strategies'):
        for strategy in _trading_engine.strategies.values():
            if strategy.name == 'tech_analysis':
                return strategy

    # Якщо не знайшли, повертаємо запасний варіант
    return _tech_strategy


def async_route(f):
    """Декоратор для асинхронних роутів"""

    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(f(*args, **kwargs))

    return wrapped


# ============= ОСНОВНІ ENDPOINTS =============

@tech_analysis_bp.route('/status', methods=['GET'])
@async_route
async def get_status():
    """Отримання статусу стратегії"""
    strategy = get_tech_strategy()

    # Якщо стратегії немає, але є trading_engine - спробуємо знайти ще раз
    if not strategy and _trading_engine:
        for s in _trading_engine.strategies.values():
            if s.name == 'tech_analysis':
                strategy = s
                break

    if not strategy:
        # Повертаємо базовий статус без помилки, щоб фронтенд не падав
        return jsonify({
            'enabled': False,
            'name': 'tech_analysis',
            'balance': 100,
            'locked_balance': 0,
            'available_balance': 100,
            'total_pnl': 0,
            'total_trades': 0,
            'win_rate': 0,
            'symbols': ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'],
            'trade_size_percent': 50,
            'take_profit_percent': 4.0,
            'stop_loss_percent': 2.0,
            'min_confidence': 65,
            'timeframe': '60',
            'forecast_accuracy': 0,
            'active_forecasts': 0,
            'settings': {
                'trade_size_percent': 50,
                'min_confidence': 65,
                'stop_loss_percent': 2.0,
                'take_profit_percent': 4.0
            }
        })

    try:
        if hasattr(strategy, 'get_status'):
            status = await strategy.get_status()
            return jsonify(status)
        else:
            return jsonify({
                'id': getattr(strategy, 'strategy_id', None),
                'name': getattr(strategy, 'name', 'tech_analysis'),
                'enabled': getattr(strategy, 'enabled', False),
                'balance': getattr(strategy, 'balance', 100),
                'locked_balance': getattr(strategy, 'locked_balance', 0),
                'available_balance': getattr(strategy, 'available_balance', 100),
                'total_pnl': getattr(strategy, 'total_pnl', 0),
                'total_trades': getattr(strategy, 'total_trades', 0),
                'winning_trades': getattr(strategy, 'winning_trades', 0),
                'losing_trades': getattr(strategy, 'losing_trades', 0),
                'win_rate': getattr(strategy, 'win_rate', 0),
                'symbols': getattr(strategy, 'symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']),
                'trade_size_percent': getattr(strategy, 'trade_size_percent', 50),
                'take_profit_percent': getattr(strategy, 'take_profit_percent', 4.0),
                'stop_loss_percent': getattr(strategy, 'stop_loss_percent', 2.0),
                'min_confidence': getattr(strategy, 'min_confidence', 65),
                'timeframe': getattr(strategy, 'timeframe', '60'),
                'forecast_accuracy': getattr(strategy, 'forecast_accuracy', 0),
                'active_forecasts': len(getattr(strategy, 'forecasts', [])),
                'settings': {
                    'trade_size_percent': getattr(strategy, 'trade_size_percent', 50),
                    'min_confidence': getattr(strategy, 'min_confidence', 65),
                    'stop_loss_percent': getattr(strategy, 'stop_loss_percent', 2.0),
                    'take_profit_percent': getattr(strategy, 'take_profit_percent', 4.0)
                }
            })
    except Exception as e:
        logger.error(f"Помилка отримання статусу: {e}")
        return jsonify({'enabled': False, 'error': str(e), 'name': 'tech_analysis'}), 200


@tech_analysis_bp.route('/toggle', methods=['POST'])
@async_route
async def toggle_strategy():
    """Увімкнення/вимкнення стратегії"""
    strategy = get_tech_strategy()
    if not strategy:
        return jsonify({'success': False, 'error': 'Стратегія не знайдена', 'enabled': False}), 404

    try:
        if strategy.enabled:
            if _trading_engine and hasattr(strategy, 'strategy_id'):
                await _trading_engine.stop_strategy(strategy.strategy_id)
            else:
                await strategy.stop()
            return jsonify({'success': True, 'enabled': False, 'message': 'Стратегію зупинено'})
        else:
            if _trading_engine and hasattr(strategy, 'strategy_id'):
                await _trading_engine.start_strategy(strategy.strategy_id)
            else:
                await strategy.start()
            return jsonify({'success': True, 'enabled': True, 'message': 'Стратегію запущено'})
    except Exception as e:
        logger.error(f"Помилка toggle: {e}")
        return jsonify({'success': False, 'error': str(e), 'enabled': False}), 500


@tech_analysis_bp.route('/reset', methods=['POST'])
@async_route
async def reset_strategy():
    """Скидання стратегії"""
    strategy = get_tech_strategy()
    if not strategy:
        return jsonify({'success': False, 'error': 'Стратегія не знайдена'}), 404

    try:
        await strategy.reset()
        return jsonify({'success': True, 'message': 'Стратегію скинуто'})
    except Exception as e:
        logger.error(f"Помилка reset: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@tech_analysis_bp.route('/analyze', methods=['POST'])
@async_route
async def analyze_symbol():
    """Аналіз конкретного символу"""
    strategy = get_tech_strategy()

    data = request.json
    symbol = data.get('symbol', '').upper()

    if not symbol:
        return jsonify({'error': 'symbol не вказано'}), 400

    # Якщо стратегії немає, повертаємо нейтральний аналіз
    if not strategy:
        return jsonify({
            'signal': 'neutral',
            'confidence': 50,
            'explanation': ['Стратегія технічного аналізу не активована'],
            'target_price': 0,
            'current_price': 0,
            'trend': 'neutral'
        })

    try:
        if hasattr(strategy, '_get_indicators'):
            indicators = await strategy._get_indicators(symbol)
            if indicators:
                signal = 'long' if indicators.get('buy_signal') else 'short' if indicators.get(
                    'sell_signal') else 'neutral'
                current_price = indicators.get('price', 0)
                target_mult = 1.04 if signal == 'long' else 0.96 if signal == 'short' else 1

                explanation = []
                if indicators.get('trend'):
                    explanation.append(f"📊 Тренд: {indicators['trend']}")
                if indicators.get('rsi'):
                    explanation.append(f"📈 RSI: {indicators['rsi']:.1f}")
                if indicators.get('confidence'):
                    explanation.append(f"🎯 Впевненість: {indicators['confidence']:.0f}%")

                if signal == 'long':
                    explanation.append("🟢 Сигнал до КУПІВЛІ")
                elif signal == 'short':
                    explanation.append("🔴 Сигнал до ПРОДАЖУ")
                else:
                    explanation.append("⚪ Нейтральний сигнал")

                return jsonify({
                    'signal': signal,
                    'confidence': indicators.get('confidence', 0),
                    'explanation': explanation,
                    'target_price': round(current_price * target_mult, 2),
                    'current_price': round(current_price, 2),
                    'trend': indicators.get('trend', 'neutral')
                })
    except Exception as e:
        logger.error(f"Помилка аналізу: {e}")

    return jsonify({
        'signal': 'neutral',
        'confidence': 50,
        'explanation': ['Не вдалося виконати технічний аналіз'],
        'target_price': 0,
        'current_price': 0,
        'trend': 'neutral'
    })


@tech_analysis_bp.route('/forecasts', methods=['GET'])
@async_route
async def get_forecasts():
    """Отримання прогнозів з БД"""
    status = request.args.get('status', 'all')
    limit = int(request.args.get('limit', 50))

    try:
        with get_db() as conn:
            query = "SELECT * FROM forecasts WHERE strategy = 'tech_analysis'"
            params = []

            if status != 'all':
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            forecasts = []
            for row in cursor.fetchall():
                f = dict(row)
                # Конвертуємо дати в рядки
                if f.get('created_at'):
                    f['created_at'] = str(f['created_at'])
                if f.get('expires_at'):
                    f['expires_at'] = str(f['expires_at'])
                if f.get('resolved_at'):
                    f['resolved_at'] = str(f['resolved_at'])
                forecasts.append(f)

            return jsonify({'forecasts': forecasts})
    except Exception as e:
        logger.error(f"Помилка отримання прогнозів: {e}")
        return jsonify({'forecasts': []})


@tech_analysis_bp.route('/forecasts/stats', methods=['GET'])
@async_route
async def get_forecast_stats():
    """Статистика прогнозів"""
    try:
        with get_db() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM forecasts WHERE strategy = 'tech_analysis'")
            total = cursor.fetchone()['count'] if cursor else 0

            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM forecasts WHERE strategy = 'tech_analysis' AND status = 'active'")
            active = cursor.fetchone()['count'] if cursor else 0

            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM forecasts WHERE strategy = 'tech_analysis' AND success = 1")
            success = cursor.fetchone()['count'] if cursor else 0

            accuracy = round(success / (total - active) * 100, 1) if (total - active) > 0 else 0

            return jsonify({
                'total': total,
                'active': active,
                'success': success,
                'failed': total - active - success,
                'accuracy': accuracy
            })
    except Exception as e:
        logger.error(f"Помилка отримання статистики: {e}")
        return jsonify({'total': 0, 'active': 0, 'success': 0, 'failed': 0, 'accuracy': 0})


@tech_analysis_bp.route('/analyze_all', methods=['POST'])
@async_route
async def analyze_all_symbols():
    """Аналіз всіх символів та створення прогнозів"""
    strategy = get_tech_strategy()
    if not strategy:
        return jsonify({'success': False, 'error': 'Стратегія не знайдена'}), 404

    try:
        results = []
        # Отримуємо дані про ціни
        for symbol in getattr(strategy, 'symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']):
            # Отримуємо свічки для різних таймфреймів
            klines_data = {}
            for tf in ['1D', '4H', '1H']:
                klines = await strategy.exchange.get_klines(symbol, tf, limit=100)
                if klines:
                    klines_data[tf] = klines

            # Аналізуємо символ
            if hasattr(strategy, 'analyze_symbol'):
                analysis = await strategy.analyze_symbol(symbol, klines_data)
                results.append({
                    'symbol': symbol,
                    'signal': analysis.get('signal'),
                    'confidence': analysis.get('confidence'),
                    'target_price': analysis.get('target_price'),
                    'explanation': analysis.get('explanation', [])
                })

                # Якщо є сигнал - створюємо прогноз
                if analysis.get('signal') in ['long', 'short'] and analysis.get('confidence', 0) >= getattr(strategy,
                                                                                                            'min_confidence',
                                                                                                            65):
                    await strategy.create_forecast(
                        symbol=symbol,
                        signal=analysis.get('signal'),
                        target_price=analysis.get('target_price', 0),
                        current_price=analysis.get('current_price', 0),
                        confidence=analysis.get('confidence', 0),
                        explanation=analysis.get('explanation', [])
                    )

        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logger.error(f"Помилка аналізу всіх символів: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@tech_analysis_bp.route('/settings', methods=['POST'])
@async_route
async def update_settings():
    """Оновлення налаштувань стратегії"""
    strategy = get_tech_strategy()
    if not strategy:
        return jsonify({'success': False, 'error': 'Стратегія не знайдена'}), 404

    data = request.json

    try:
        if 'symbols' in data:
            strategy.symbols = [s.upper().strip() + 'USDT' if not s.upper().endswith('USDT') else s.upper().strip()
                                for s in data['symbols'] if s]
        if 'trade_size_percent' in data:
            strategy.trade_size_percent = float(data['trade_size_percent'])
        if 'min_confidence' in data:
            strategy.min_confidence = float(data['min_confidence'])
        if 'stop_loss_percent' in data:
            strategy.stop_loss_percent = float(data['stop_loss_percent'])
        if 'take_profit_percent' in data:
            strategy.take_profit_percent = float(data['take_profit_percent'])
        if 'timeframe' in data:
            strategy.timeframe = str(data['timeframe'])

        if hasattr(strategy, 'update_settings'):
            await strategy.update_settings(**data)

        logger.info(f"Оновлено налаштування tech_analysis: {data}")
        return jsonify({'success': True, 'message': 'Налаштування збережено'})
    except Exception as e:
        logger.error(f"Помилка збереження налаштувань: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============= ДІАГНОСТИКА ТА ТЕСТОВІ ENDPOINTS =============

@tech_analysis_bp.route('/debug/check_db', methods=['GET'])
@async_route
async def debug_check_db():
    """Перевірка БД - чи є таблиця forecasts і чи є дані"""
    try:
        with get_db() as conn:
            # Перевіряємо чи існує таблиця
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forecasts'")
            table_exists = cursor.fetchone() is not None

            result = {
                'table_exists': table_exists,
                'forecasts_count': 0,
                'sample_forecasts': [],
                'tables': []
            }

            if table_exists:
                cursor = conn.execute("SELECT COUNT(*) as count FROM forecasts")
                result['forecasts_count'] = cursor.fetchone()['count']

                # Отримуємо кілька прогнозів для прикладу
                cursor = conn.execute("SELECT * FROM forecasts LIMIT 5")
                for row in cursor.fetchall():
                    result['sample_forecasts'].append(dict(row))

            # Список всіх таблиць
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            result['tables'] = [row['name'] for row in cursor.fetchall()]

            return jsonify(result)
    except Exception as e:
        logger.error(f"Помилка діагностики: {e}")
        return jsonify({'error': str(e)}), 500


@tech_analysis_bp.route('/test/create_forecast', methods=['POST'])
@async_route
async def test_create_forecast():
    """Тестовий ендпоінт для створення прогнозу"""
    data = request.json

    symbol = data.get('symbol', 'BTCUSDT')
    signal_type = data.get('signal_type', 'long')
    entry_price = data.get('entry_price', 50000)
    target_price = data.get('target_price', 52000)
    confidence = data.get('confidence', 75)
    explanation = data.get('explanation', 'Тестовий прогноз з веб-інтерфейсу')

    try:
        from datetime import datetime, timedelta

        with get_db() as conn:
            conn.execute("""
                INSERT INTO forecasts (
                    strategy, symbol, signal_type, entry_price, target_price,
                    confidence, explanation, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'tech_analysis', symbol, signal_type, entry_price, target_price,
                confidence, explanation, 'active', datetime.now(),
                datetime.now() + timedelta(hours=24)
            ))

        logger.info(f"✅ Тестовий прогноз створено для {symbol}")
        return jsonify({'success': True, 'message': f'Прогноз для {symbol} створено'})
    except Exception as e:
        logger.error(f"Помилка створення тестового прогнозу: {e}")
        return jsonify({'error': str(e)}), 500


@tech_analysis_bp.route('/create_forecast', methods=['POST'])
@async_route
async def create_forecast_manual():
    """Ручне створення прогнозу через API"""
    data = request.json

    required = ['symbol', 'signal_type', 'target_price']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Поле {field} обов\'язкове'}), 400

    try:
        from datetime import datetime, timedelta

        symbol = data['symbol'].upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'

        with get_db() as conn:
            # Отримуємо поточну ціну з ордерів або використовуємо entry_price з запиту
            entry_price = data.get('entry_price', 0)

            conn.execute("""
                INSERT INTO forecasts (
                    strategy, symbol, signal_type, entry_price, target_price,
                    confidence, explanation, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'tech_analysis',
                symbol,
                data['signal_type'],
                entry_price,
                data['target_price'],
                data.get('confidence', 70),
                data.get('explanation', 'Ручний прогноз'),
                'active',
                datetime.now(),
                datetime.now() + timedelta(hours=data.get('hours', 24))
            ))

        return jsonify({'success': True, 'message': f'Прогноз для {symbol} створено'})
    except Exception as e:
        logger.error(f"Помилка створення прогнозу: {e}")
        return jsonify({'error': str(e)}), 500


@tech_analysis_bp.route('/close_position/<symbol>', methods=['POST'])
@async_route
async def close_position(symbol):
    """Примусове закриття позиції для конкретного символу"""
    strategy = get_tech_strategy()
    if not strategy:
        return jsonify({'success': False, 'error': 'Стратегія не знайдена'}), 404

    try:
        if symbol not in strategy.open_positions:
            return jsonify({'success': False, 'error': f'Немає відкритої позиції для {symbol}'}), 404

        position = strategy.open_positions[symbol]
        current_price = strategy.current_prices.get(symbol, position['entry_price'])

        # Розраховуємо PnL перед закриттям
        entry_price = position['entry_price']
        quantity = position['quantity']
        side = position['side']

        if side == 'buy':
            gross_pnl = (current_price - entry_price) * quantity
            gross_pnl_percent = (current_price - entry_price) / entry_price * 100
        else:
            gross_pnl = (entry_price - current_price) * quantity
            gross_pnl_percent = (entry_price - current_price) / entry_price * 100

        commission_rate = 0.001
        commission = (quantity * entry_price + quantity * current_price) * commission_rate
        estimated_real_pnl = gross_pnl - commission

        await strategy._close_position(symbol, current_price, "manual_force_close")

        return jsonify({
            'success': True,
            'symbol': symbol,
            'entry_price': entry_price,
            'close_price': current_price,
            'gross_pnl': round(gross_pnl, 4),
            'gross_pnl_percent': round(gross_pnl_percent, 2),
            'commission': round(commission, 4),
            'real_pnl': round(estimated_real_pnl, 4),
            'message': f'Позицію {symbol} закрито з PnL: ${estimated_real_pnl:.4f}'
        })
    except Exception as e:
        logger.error(f"Помилка закриття позиції {symbol}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@tech_analysis_bp.route('/close_all_positions', methods=['POST'])
@async_route
async def close_all_positions():
    """Закриття всіх відкритих позицій"""
    strategy = get_tech_strategy()
    if not strategy:
        return jsonify({'success': False, 'error': 'Стратегія не знайдена'}), 404

    try:
        closed_count = 0
        total_pnl = 0.0

        for symbol in list(strategy.open_positions.keys()):
            position = strategy.open_positions[symbol]
            current_price = strategy.current_prices.get(symbol, position['entry_price'])

            # Розраховуємо PnL
            entry_price = position['entry_price']
            quantity = position['quantity']
            side = position['side']

            if side == 'buy':
                pnl = (current_price - entry_price) * quantity
            else:
                pnl = (entry_price - current_price) * quantity

            await strategy._close_position(symbol, current_price, "batch_close")
            closed_count += 1
            total_pnl += pnl

        return jsonify({
            'success': True,
            'closed_count': closed_count,
            'total_pnl': round(total_pnl, 4)
        })
    except Exception as e:
        logger.error(f"Помилка закриття всіх позицій: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500