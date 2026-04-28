# web/webhook_routes.py
"""
Веб-хуки для отримання зовнішніх сигналів
"""
import hashlib
import hmac
import os
from datetime import datetime
from flask import request, jsonify

from utils.logger_utils import setup_logger

logger = setup_logger('webhooks')

WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'your-secret-key-change-me')


def register_webhook_routes(app, trading_engine):
    """Реєстрація всіх веб-хук роутів"""

    @app.route('/webhook/tradingview', methods=['POST'])
    def webhook_tradingview():
        """Веб-хук від TradingView"""
        try:
            data = request.get_json()
            logger.info(f"📡 Отримано сигнал від TradingView: {data}")

            # Отримуємо стратегію signals
            signals_strategy = None
            for strategy in trading_engine.strategies.values():
                if strategy.name == 'signals':
                    signals_strategy = strategy
                    break

            if not signals_strategy:
                return jsonify({'error': 'Signals strategy not found'}), 404

            # Парсимо дані
            symbol = data.get('symbol', '').upper().strip()
            action = data.get('action', '').lower()
            price = float(data.get('price', 0))
            stop_loss = float(data.get('stop_loss', 0)) if data.get('stop_loss') else None
            take_profit = data.get('take_profit', [])

            if not symbol or not action or price <= 0:
                return jsonify({'error': 'Missing required fields'}), 400

            if action in ['buy', 'long']:
                signal_type = 'LONG'
            elif action in ['sell', 'short']:
                signal_type = 'SHORT'
            else:
                return jsonify({'error': f'Unknown action: {action}'}), 400

            if not take_profit:
                if signal_type == 'LONG':
                    take_profit = [round(price * 1.01, 2), round(price * 1.02, 2)]
                else:
                    take_profit = [round(price * 0.99, 2), round(price * 0.98, 2)]

            if not stop_loss:
                if signal_type == 'LONG':
                    stop_loss = round(price * 0.98, 2)
                else:
                    stop_loss = round(price * 1.02, 2)

            # Створюємо сигнал через asyncio
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            signal_data = {
                'symbol': symbol,
                'signal_type': signal_type,
                'entry_price': price,
                'stop_loss': stop_loss,
                'take_profits': take_profit,
                'trade_size_usdt': data.get('trade_size', 20)
            }

            signal = loop.run_until_complete(signals_strategy.add_signal(signal_data))
            loop.close()

            return jsonify({
                'status': 'success',
                'signal_id': signal.id if signal else None,
                'message': f'Signal added: {signal_type} {symbol} @ ${price}'
            })

        except Exception as e:
            logger.error(f"Помилка TradingView веб-хука: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/webhook/simple', methods=['POST'])
    def webhook_simple():
        """Простий веб-хук для зовнішніх сервісів"""
        try:
            data = request.get_json()
            logger.info(f"📡 Отримано простий веб-хук: {data}")

            signals_strategy = None
            for strategy in trading_engine.strategies.values():
                if strategy.name == 'signals':
                    signals_strategy = strategy
                    break

            if not signals_strategy:
                return jsonify({'error': 'Signals strategy not found'}), 404

            symbol = data.get('symbol', '').upper().strip()
            if not symbol.endswith('USDT'):
                symbol = symbol + 'USDT'

            side = data.get('side') or data.get('action')
            if side in ['buy', 'long', 'LONG', 'BUY']:
                signal_type = 'LONG'
            elif side in ['sell', 'short', 'SHORT', 'SELL']:
                signal_type = 'SHORT'
            else:
                return jsonify({'error': f'Unknown side: {side}'}), 400

            price = float(data.get('entry') or data.get('price') or 0)
            if price <= 0:
                return jsonify({'error': 'Invalid price'}), 400

            stop_loss = data.get('sl') or data.get('stop_loss')
            if stop_loss:
                stop_loss = float(stop_loss)
            else:
                stop_loss = price * 0.98 if signal_type == 'LONG' else price * 1.02

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

            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            signal_data = {
                'symbol': symbol,
                'signal_type': signal_type,
                'entry_price': price,
                'stop_loss': stop_loss,
                'take_profits': take_profit,
                'trade_size_usdt': data.get('trade_size', 20)
            }

            signal = loop.run_until_complete(signals_strategy.add_signal(signal_data))
            loop.close()

            return jsonify({
                'status': 'success',
                'signal_id': signal.id if signal else None,
                'message': f'Signal added: {signal_type} {symbol} @ ${price}'
            })

        except Exception as e:
            logger.error(f"Помилка простого веб-хука: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/webhook/test', methods=['GET', 'POST'])
    def webhook_test():
        """Тестовий ендпоінт"""
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
                    '/webhook/simple': 'POST - Simple custom webhook',
                    '/webhook/test': 'GET/POST - Test endpoint'
                },
                'example': {
                    'symbol': 'BTCUSDT',
                    'side': 'buy',
                    'entry': 50000,
                    'sl': 49000,
                    'tp': [51000, 52000]
                }
            })

    logger.info("✅ Веб-хуки зареєстровано")