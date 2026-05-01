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
            forecasts = [dict(row) for row in cursor.fetchall()]

            # Конвертуємо дати в рядки
            for f in forecasts:
                if f.get('created_at'):
                    f['created_at'] = str(f['created_at'])
                if f.get('expires_at'):
                    f['expires_at'] = str(f['expires_at'])
                if f.get('resolved_at'):
                    f['resolved_at'] = str(f['resolved_at'])

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