import logging
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

tech_analysis_bp = Blueprint('tech_analysis', __name__, url_prefix='/api/tech_analysis')

# Глобальний екземпляр стратегії
tech_strategy = None


def init_tech_strategy(strategy):
    """Ініціалізація стратегії зовнішнім модулем"""
    global tech_strategy
    tech_strategy = strategy


@tech_analysis_bp.route('/status', methods=['GET'])
def get_status():
    """Отримання статусу стратегії"""
    if not tech_strategy:
        return jsonify({'error': 'Стратегія не ініціалізована'}), 500

    return jsonify({
        'name': tech_strategy.name,
        'enabled': tech_strategy.enabled,
        'balance': tech_strategy.balance,
        'locked_balance': tech_strategy.locked_balance,
        'total_pnl': tech_strategy.total_pnl,
        'total_trades': tech_strategy.total_trades,
        'win_rate': tech_strategy.win_rate,
        'wins': tech_strategy.wins,
        'losses': tech_strategy.losses,
        'symbols': tech_strategy.symbols,
        'timeframes': tech_strategy.timeframes,
        'settings': {
            'trade_size_percent': tech_strategy.trade_size_percent,
            'stop_loss_percent': tech_strategy.stop_loss_percent,
            'take_profit_percent': tech_strategy.take_profit_percent,
            'min_confidence': tech_strategy.min_confidence
        }
    })


@tech_analysis_bp.route('/settings', methods=['POST'])
def update_settings():
    """Оновлення налаштувань стратегії"""
    if not tech_strategy:
        return jsonify({'error': 'Стратегія не ініціалізована'}), 500

    data = request.json

    try:
        if 'symbols' in data:
            tech_strategy.symbols = [s.upper() for s in data['symbols'] if s]
        if 'trade_size_percent' in data:
            tech_strategy.trade_size_percent = float(data['trade_size_percent'])
        if 'min_confidence' in data:
            tech_strategy.min_confidence = float(data['min_confidence'])
        if 'stop_loss_percent' in data:
            tech_strategy.stop_loss_percent = float(data['stop_loss_percent'])
        if 'take_profit_percent' in data:
            tech_strategy.take_profit_percent = float(data['take_profit_percent'])

        return jsonify({'success': True, 'message': 'Налаштування збережено'})
    except Exception as e:
        logger.error(f"Помилка збереження налаштувань: {e}")
        return jsonify({'error': str(e)}), 500


@tech_analysis_bp.route('/analyze', methods=['POST'])
def analyze_symbol():
    """Аналіз конкретного символу"""
    if not tech_strategy:
        return jsonify({'error': 'Стратегія не ініціалізована'}), 500

    data = request.json
    symbol = data.get('symbol', '').upper()

    if not symbol:
        return jsonify({'error': 'symbol не вказано'}), 400

    # Тестові дані для аналізу
    result = {
        'signal': 'neutral',
        'confidence': 75,
        'explanation': [
            '📈 Довгостроковий тренд ВИСХІДНИЙ',
            '🟢 RSI = 32.5 - ЗОНА ПЕРЕПРОДАНОСТІ',
            '📊 MACD вище сигнальної лінії - БИЧИЙ СИГНАЛ',
            '🎯 ПРОГНОЗ: Ціна має зрости до $52340.00 (+2.5%)'
        ],
        'target_price': 52340.00,
        'current_price': 51000.00,
        'trend': 'bullish'
    }

    return jsonify(result)


@tech_analysis_bp.route('/forecasts', methods=['GET'])
def get_forecasts():
    """Отримання списку прогнозів (тестові дані)"""
    status = request.args.get('status', 'all')
    limit = int(request.args.get('limit', 20))

    # Тестові прогнози
    forecasts = []

    # Додаємо тестовий активний прогноз
    forecasts.append({
        'id': 1,
        'symbol': 'BTCUSDT',
        'signal_type': 'long',
        'entry_price': 51000.0,
        'target_price': 53000.0,
        'confidence': 78.5,
        'explanation': 'RSI в зоні перепроданості, MACD дає бичий сигнал, EMA показують висхідний тренд',
        'status': 'active',
        'success': False,
        'created_at': datetime.now().isoformat(),
        'expires_at': (datetime.now() + timedelta(hours=12)).isoformat(),
        'resolved_at': None,
        'resolved_price': None
    })

    # Додаємо тестові історичні прогнози
    historical = [
        {'symbol': 'BTCUSDT', 'signal_type': 'long', 'entry_price': 50000, 'target_price': 52000, 'success': True,
         'status': 'success', 'confidence': 72},
        {'symbol': 'ETHUSDT', 'signal_type': 'short', 'entry_price': 3000, 'target_price': 2850, 'success': True,
         'status': 'success', 'confidence': 68},
        {'symbol': 'SOLUSDT', 'signal_type': 'long', 'entry_price': 150, 'target_price': 165, 'success': False,
         'status': 'failed', 'confidence': 65},
        {'symbol': 'BTCUSDT', 'signal_type': 'short', 'entry_price': 52000, 'target_price': 50500, 'success': False,
         'status': 'failed', 'confidence': 70},
        {'symbol': 'ETHUSDT', 'signal_type': 'long', 'entry_price': 2800, 'target_price': 3100, 'success': True,
         'status': 'expired', 'confidence': 66},
    ]

    for i, h in enumerate(historical, start=2):
        forecasts.append({
            'id': i,
            'symbol': h['symbol'],
            'signal_type': h['signal_type'],
            'entry_price': h['entry_price'],
            'target_price': h['target_price'],
            'confidence': h['confidence'],
            'explanation': 'Технічний аналіз показав потенційний рух',
            'status': h['status'],
            'success': h['success'],
            'created_at': (datetime.now() - timedelta(days=i * 2)).isoformat(),
            'expires_at': (datetime.now() - timedelta(days=i * 2 - 1)).isoformat(),
            'resolved_at': datetime.now().isoformat(),
            'resolved_price': h['target_price'] if h['success'] else None
        })

    # Фільтруємо за статусом
    if status != 'all':
        forecasts = [f for f in forecasts if f['status'] == status]

    # Обмежуємо кількість
    forecasts = forecasts[:limit]

    return jsonify({'forecasts': forecasts, 'total': len(forecasts)})


@tech_analysis_bp.route('/forecasts/stats', methods=['GET'])
def get_forecast_stats():
    """Отримання статистики прогнозів (тестові дані)"""

    # Тестові дані
    daily_stats = []
    for i in range(10, 0, -1):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        total = 3
        success = 2 if i % 2 == 0 else 1
        daily_stats.append({
            'date': date,
            'total': total,
            'success': success,
            'accuracy': round(success / total * 100, 1)
        })

    symbol_stats = [
        {'symbol': 'BTCUSDT', 'total': 15, 'success': 11, 'accuracy': 73.3, 'avg_confidence': 71.5},
        {'symbol': 'ETHUSDT', 'total': 12, 'success': 8, 'accuracy': 66.7, 'avg_confidence': 68.2},
        {'symbol': 'SOLUSDT', 'total': 8, 'success': 5, 'accuracy': 62.5, 'avg_confidence': 65.8},
    ]

    return jsonify({
        'total': 35,
        'active': 1,
        'success': 24,
        'failed': 8,
        'expired': 3,
        'avg_confidence': 69.5,
        'accuracy': 75.0,
        'daily_stats': daily_stats,
        'symbol_stats': symbol_stats
    })


@tech_analysis_bp.route('/toggle', methods=['POST'])
def toggle_strategy():
    """Увімкнення/вимкнення стратегії"""
    if not tech_strategy:
        return jsonify({'error': 'Стратегія не ініціалізована'}), 500

    tech_strategy.enabled = not tech_strategy.enabled
    status = "активовано" if tech_strategy.enabled else "деактивовано"
    return jsonify({'success': True, 'message': f'Стратегію {status}', 'enabled': tech_strategy.enabled})


@tech_analysis_bp.route('/reset', methods=['POST'])
def reset_strategy():
    """Скидання стратегії"""
    if not tech_strategy:
        return jsonify({'error': 'Стратегія не ініціалізована'}), 500

    tech_strategy.balance = 100.0
    tech_strategy.locked_balance = 0.0
    tech_strategy.total_pnl = 0.0
    tech_strategy.total_trades = 0
    tech_strategy.wins = 0
    tech_strategy.losses = 0
    tech_strategy.win_rate = 0.0
    tech_strategy.active_positions = {}
    tech_strategy.forecasts = []

    return jsonify({'success': True, 'message': 'Стратегію скинуто'})