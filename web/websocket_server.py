"""Окремий WebSocket сервер для real-time оновлень"""
import asyncio
import json
import time
import threading
from typing import Dict, Set
from websockets.server import serve
from websockets.exceptions import ConnectionClosed

from utils.logger_utils import setup_logger

logger = setup_logger('websocket')

# Глобальні змінні
connected_clients: Set = set()
trading_engine_global = None


async def register(websocket):
    """Реєстрація нового клієнта"""
    connected_clients.add(websocket)
    logger.info(f"✅ WebSocket клієнт підключено. Всього: {len(connected_clients)}")
    try:
        await websocket.send(json.dumps({
            'type': 'connected',
            'timestamp': time.time(),
            'clients': len(connected_clients)
        }))
    except:
        pass


async def unregister(websocket):
    """Видалення клієнта"""
    connected_clients.discard(websocket)
    logger.info(f"❌ WebSocket клієнт відключено. Всього: {len(connected_clients)}")


async def broadcast(message: dict):
    """Розсилка повідомлення всім клієнтам"""
    if not connected_clients:
        return

    data = json.dumps(message)
    to_remove = set()

    for client in connected_clients:
        try:
            await client.send(data)
        except:
            to_remove.add(client)

    for client in to_remove:
        connected_clients.discard(client)


async def broadcast_loop():
    """Фоновий цикл розсилки оновлень"""
    last_update = {}

    while True:
        try:
            if trading_engine_global and connected_clients:
                # Збираємо дані
                strategies_data = []
                for strategy in trading_engine_global.strategies.values():
                    try:
                        status = await strategy.get_status()
                        strategies_data.append(status)
                    except:
                        pass

                total_pnl = sum(s.get('total_pnl', 0) for s in strategies_data)
                total_balance = sum(s.get('balance', 0) for s in strategies_data)
                active_count = sum(1 for s in strategies_data if s.get('enabled'))

                now = time.time()

                # Оновлення кожні 3 секунди
                if now - last_update.get('time', 0) >= 3:
                    await broadcast({
                        'type': 'update',
                        'timestamp': now,
                        'data': {
                            'total_pnl': total_pnl,
                            'total_balance': total_balance,
                            'active_strategies': active_count,
                            'strategies': strategies_data
                        }
                    })
                    last_update['time'] = now

                # Ціни кожну секунду
                price_updates = {}
                for symbol in trading_engine_global.config.SYMBOLS:
                    price = trading_engine_global.exchange.current_prices.get(symbol, 0)
                    if price > 0:
                        price_updates[symbol] = price

                if price_updates:
                    await broadcast({
                        'type': 'prices',
                        'prices': price_updates
                    })

            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"broadcast_loop помилка: {e}")
            await asyncio.sleep(5)


async def handler(websocket):
    """Обробник WebSocket з'єднання"""
    await register(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get('ping'):
                    await websocket.send(json.dumps({'type': 'pong', 'timestamp': time.time()}))
            except:
                pass
    except ConnectionClosed:
        pass
    finally:
        await unregister(websocket)


async def start_websocket_server(host='0.0.0.0', port=8765):
    """Запуск WebSocket сервера"""
    logger.info(f"🔌 Запуск WebSocket сервера на ws://{host}:{port}")

    async with serve(handler, host, port):
        # Запускаємо фоновий цикл розсилки
        asyncio.create_task(broadcast_loop())
        await asyncio.Future()  # run forever


def run_websocket_server(engine, host='0.0.0.0', port=8765):
    """Запуск WebSocket сервера в окремому потоці"""
    global trading_engine_global
    trading_engine_global = engine

    def _run():
        asyncio.run(start_websocket_server(host, port))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"WebSocket сервер запущено на порту {port}")
    return thread