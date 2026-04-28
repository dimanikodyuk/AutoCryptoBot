"""
Веб-хуки для отримання зовнішніх сигналів
Підтримує: TradingView, 3Commas, Telegram, Custom webhooks
"""
import asyncio
import hashlib
import hmac
import os
from datetime import datetime
from functools import wraps
from flask import request, jsonify

from utils.logger_utils import setup_logger

logger = setup_logger('webhooks')

# Секретний ключ для верифікації (з .env)
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'your-secret-key-change-me')


def verify_signature(f):
    """Декоратор для перевірки підпису веб-хука"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Для тестування можна пропустити перевірку
        if os.getenv('ENV', 'production') == 'development':
            return f(*args, **kwargs)

        signature = request.headers.get('X-Signature')
        if not signature:
            logger.warning("Веб-хук без підпису")
            return jsonify({'error': 'Missing signature'}), 401

        # Обчислюємо підпис тіла запиту
        body = request.get_data()
        expected = hmac.new(
            WEBHOOK_SECRET.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            logger.warning(f"Невірний підпис веб-хука")
            return jsonify({'error': 'Invalid signature'}), 401

        return f(*args, **kwargs)
    return decorated_function


def register_webhook_routes(app, trading_engine):
    """Реєстрація всіх веб-хук роутів"""

    @app.route('/webhook/tradingview', methods=['POST'])
    @verify_signature
    async def webhook_tradingview():
        """
        Веб-хук від TradingView
        Формат: {"symbol": "BTCUSDT", "action": "buy", "price": 50000, "stop_loss": 49000, "take_profit": [51000, 52000]}
        """
        try:
            data = request.get_json()
            logger.info(f"📡 Отримано сигнал від TradingView: {data}")

            # Отримуємо стратегію signals
            signals_strategy = _get_signals_strategy(trading_engine)
            if not signals_strategy:
                return jsonify({'error': 'Signals strategy not found'}), 404

            # Парсимо дані
            symbol = data.get('symbol', '').upper().strip()
            action = data.get('action', '').lower()
            price = float(data.get('price', 0))
            stop_loss = float(data.get('stop_loss', 0)) if data.get('stop_loss') else None
            take_profit = data.get('take_profit', [])

            # Валідація
            if not symbol or not action or price <= 0:
                return jsonify({'error': 'Missing required fields: symbol, action, price'}), 400

            # Визначаємо тип сигналу
            if action in ['buy', 'long']:
                signal_type = 'LONG'
            elif action in ['sell', 'short']:
                signal_type = 'SHORT'
            else:
                return jsonify({'error': f'Unknown action: {action}'}), 400

            # Якщо немає TP, додаємо стандартні
            if not take_profit:
                if signal_type == 'LONG':
                    take_profit = [round(price * 1.01, 2), round(price * 1.02, 2), round(price * 1.03, 2)]
                else:
                    take_profit = [round(price * 0.99, 2), round(price * 0.98, 2), round(price * 0.97, 2)]

            # Якщо немає SL, додаємо стандартний
            if not stop_loss:
                if signal_type == 'LONG':
                    stop_loss = round(price * 0.98, 2)
                else:
                    stop_loss = round(price * 1.02, 2)

            # Створюємо сигнал
            signal_data = {
                'symbol': symbol,
                'signal_type': signal_type,
                'entry_price': price,
                'stop_loss': stop_loss,
                'take_profits': take_profit,
                'trade_size_usdt': data.get('trade_size', 20)
            }

            signal = await signals_strategy.add_signal(signal_data)

            if signal:
                logger.info(f"✅ Сигнал від TradingView додано: {signal_type} {symbol} @ ${price}")
                return jsonify({
                    'status': 'success',
                    'signal_id': signal.id,
                    'message': f'Signal added: {signal_type} {symbol} @ ${price}'
                })
            else:
                return jsonify({'error': 'Failed to add signal'}), 500

        except Exception as e:
            logger.error(f"Помилка обробки TradingView веб-хука: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/webhook/3commas', methods=['POST'])
    @verify_signature
    async def webhook_3commas():
        """
        Веб-хук від 3Commas
        Формат: {"bot_id": 123, "action": "deal_started", "data": {"pair": "BTC_USDT", "type": "buy", "price": 50000}}
        """
        try:
            data = request.get_json()
            logger.info(f"📡 Отримано сигнал від 3Commas: {data}")

            action = data.get('action')
            deal_data = data.get('data', {})

            if action == 'deal_started':
                symbol = deal_data.get('pair', '').replace('_', '')
                signal_type = 'LONG' if deal_data.get('type') == 'buy' else 'SHORT'
                price = float(deal_data.get('price', 0))

                signals_strategy = _get_signals_strategy(trading_engine)
                if not signals_strategy:
                    return jsonify({'error': 'Signals strategy not found'}), 404

                signal_data = {
                    'symbol': symbol,
                    'signal_type': signal_type,
                    'entry_price': price,
                    'stop_loss': price * 0.98 if signal_type == 'LONG' else price * 1.02,
                    'take_profits': [price * 1.02, price * 1.04] if signal_type == 'LONG' else [price * 0.98, price * 0.96],
                    'trade_size_usdt': 20
                }

                signal = await signals_strategy.add_signal(signal_data)

                return jsonify({'status': 'success', 'signal_id': signal.id if signal else None})

            return jsonify({'status': 'ignored', 'message': f'Unhandled action: {action}'})

        except Exception as e:
            logger.error(f"Помилка обробки 3Commas веб-хука: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/webhook/simple', methods=['POST'])
    @verify_signature
    async def webhook_simple():
        """
        Простий веб-хук для будь-яких зовнішніх сервісів
        Підтримує формат:
        - {"symbol": "BTC", "side": "buy", "entry": 50000}
        - {"symbol": "ETH", "action": "short", "price": 3000, "sl": 3100, "tp": [2900, 2800]}
        """
        try:
            data = request.get_json()
            logger.info(f"📡 Отримано простий веб-хук сигнал: {data}")

            signals_strategy = _get_signals_strategy(trading_engine)
            if not signals_strategy:
                return jsonify({'error': 'Signals strategy not found'}), 404

            # Підтримка різних форматів
            symbol = data.get('symbol', '').upper().strip()
            if not symbol.endswith('USDT'):
                symbol = symbol + 'USDT'

            # Визначаємо сторону
            side = data.get('side') or data.get('action')
            if side in ['buy', 'long', 'LONG', 'BUY']:
                signal_type = 'LONG'
            elif side in ['sell', 'short', 'SHORT', 'SELL']:
                signal_type = 'SHORT'
            else:
                return jsonify({'error': f'Unknown side: {side}'}), 400

            # Ціна
            price = float(data.get('entry') or data.get('price') or data.get('entry_price') or 0)
            if price <= 0:
                return jsonify({'error': 'Invalid price'}), 400

            # Stop Loss
            stop_loss = data.get('sl') or data.get('stop_loss')
            if stop_loss:
                stop_loss = float(stop_loss)
            else:
                stop_loss = price * 0.98 if signal_type == 'LONG' else price * 1.02

            # Take Profits
            take_profit = data.get('tp') or data.get('take_profit')
            if take_profit:
                if isinstance(take_profit, list):
                    take_profit = [float(x) for x in take_profit]
                else:
                    take_profit = [float(take_profit)]
            else:
                if signal_type == 'LONG':
                    take_profit = [round(price * 1.01, 2), round(price * 1.02, 2)]
                else:
                    take_profit = [round(price * 0.99, 2), round(price * 0.98, 2)]

            signal_data = {
                'symbol': symbol,
                'signal_type': signal_type,
                'entry_price': price,
                'stop_loss': stop_loss,
                'take_profits': take_profit,
                'trade_size_usdt': data.get('trade_size', 20)
            }

            signal = await signals_strategy.add_signal(signal_data)

            return jsonify({
                'status': 'success',
                'signal_id': signal.id if signal else None,
                'message': f'Signal added: {signal_type} {symbol} @ ${price}'
            })

        except Exception as e:
            logger.error(f"Помилка обробки простого веб-хука: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/webhook/test', methods=['GET', 'POST'])
    async def webhook_test():
        """Тестовий ендпоінт для перевірки роботи веб-хуків"""
        if request.method == 'POST':
            data = request.get_json()
            logger.info(f"🧪 Тестовий веб-хук отримав: {data}")
            return jsonify({
                'status': 'ok',
                'received': data,
                'timestamp': datetime.now().isoformat()
            })
        else:
            return jsonify({
                'status': 'ok',
                'message': 'Webhook endpoint is working',
                'endpoints': {
                    '/webhook/tradingview': 'POST - TradingView alerts',
                    '/webhook/3commas': 'POST - 3Commas deals',
                    '/webhook/simple': 'POST - Simple custom webhook',
                    '/webhook/test': 'GET/POST - Test endpoint'
                },
                'example_body': {
                    'symbol': 'BTCUSDT',
                    'side': 'buy',
                    'entry': 50000,
                    'sl': 49000,
                    'tp': [51000, 52000]
                }
            })

    logger.info("✅ Веб-хуки зареєстровано")


def _get_signals_strategy(trading_engine):
    """Отримання стратегії signals"""
    for strategy in trading_engine.strategies.values():
        if strategy.name == 'signals':
            return strategy
    return None