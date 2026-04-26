import asyncio
import json
import time
import hmac
import hashlib
import aiohttp
import websockets
from typing import Dict, List, Optional
from utils.logger_utils import setup_logger
from config import Config

logger = setup_logger('exchange')


class BybitExchange:
    """Клієнт для роботи з Bybit API"""

    def __init__(self, config: Config, mode: str = 'simulation'):
        self.config = config
        self.mode = mode
        self.current_prices: Dict[str, float] = {}
        self.price_callbacks = []
        self.ws_tasks = []
        self.ws_connections: Dict[str, websockets.WebSocketClientProtocol] = {}
        self._recv_window = 5000
        self._ws_should_stop = False

        # Параметри для backoff
        self._reconnect_delay = 1  # початкова затримка (секунди)
        self._max_reconnect_delay = 60  # максимальна затримка
        self._reconnect_multiplier = 2  # множник

    async def get_current_price(self, symbol: str) -> float:
        """Отримання поточної ціни"""
        if symbol in self.current_prices:
            return self.current_prices[symbol]

        url = f"{self.config.BYBIT_REST_URL}/v5/market/tickers"
        params = {'category': 'spot', 'symbol': symbol}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    data = await response.json()
                    if data.get('retCode') == 0:
                        price = float(data['result']['list'][0]['lastPrice'])
                        self.current_prices[symbol] = price
                        logger.info(f"Отримано ціну {symbol}: ${price}")
                        return price
                    else:
                        logger.error(f"Помилка API {symbol}: {data}")
                        return 0.0
        except Exception as e:
            logger.error(f"Помилка отримання ціни {symbol}: {e}")
            return 0.0

    async def get_klines(self, symbol: str, interval: str = '1', limit: int = 100) -> List[dict]:
        interval_map = {
            '1': '1', '3': '3', '5': '5', '15': '15', '30': '30',
            '60': '60', '120': '120', '240': '240', '360': '360', '720': '720',
            'D': 'D', 'W': 'W', 'M': 'M'
        }
        interval_key = interval_map.get(str(interval), '1')
        url = f"{self.config.BYBIT_REST_URL}/v5/market/kline"
        params = {'category': 'spot', 'symbol': symbol, 'interval': interval_key, 'limit': limit}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    data = await response.json()
                    if data.get('retCode') == 0:
                        klines = []
                        for k in data['result']['list']:
                            klines.append({
                                'timestamp': int(k[0]), 'open': float(k[1]), 'high': float(k[2]),
                                'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])
                            })
                        return klines
                    return []
        except Exception as e:
            logger.error(f"Помилка свічок {symbol}: {e}")
            return []

    async def get_real_balance(self, asset: str = 'USDT') -> float:
        """Отримання реального балансу з Bybit"""
        if not self.config.BYBIT_API_KEY or not self.config.BYBIT_API_SECRET:
            logger.error("API ключі не налаштовані")
            return 0.0

        timestamp = int(time.time() * 1000)
        params = {'accountType': 'UNIFIED', 'coin': asset}
        param_str = f"{timestamp}{self.config.BYBIT_API_KEY}{self._recv_window}{json.dumps(params)}"
        signature = hmac.new(
            self.config.BYBIT_API_SECRET.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        headers = {
            'X-BAPI-API-KEY': self.config.BYBIT_API_KEY,
            'X-BAPI-TIMESTAMP': str(timestamp),
            'X-BAPI-SIGN': signature,
            'X-BAPI-RECV-WINDOW': str(self._recv_window),
            'Content-Type': 'application/json'
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.config.BYBIT_REST_URL}/v5/account/wallet-balance",
                                       headers=headers, params=params) as response:
                    data = await response.json()
                    if data.get('retCode') == 0:
                        for coin in data['result']['list'][0]['coin']:
                            if coin['coin'] == asset:
                                return float(coin['walletBalance'])
                    return 0.0
        except Exception as e:
            logger.error(f"Помилка отримання балансу: {e}")
            return 0.0

    async def create_real_order(self, symbol: str, side: str, order_type: str,
                                quantity: float, price: float = None) -> Dict:
        """Створення реального ордера на Bybit"""
        if not self.config.BYBIT_API_KEY or not self.config.BYBIT_API_SECRET:
            return {'error': 'API keys not configured'}

        timestamp = int(time.time() * 1000)
        params = {
            'category': 'spot',
            'symbol': symbol,
            'side': side.capitalize(),
            'orderType': order_type,
            'qty': str(quantity),
            'timeInForce': 'GTC'
        }
        if price and order_type == 'Limit':
            params['price'] = str(price)

        param_str = f"{timestamp}{self.config.BYBIT_API_KEY}{self._recv_window}{json.dumps(params)}"
        signature = hmac.new(
            self.config.BYBIT_API_SECRET.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        headers = {
            'X-BAPI-API-KEY': self.config.BYBIT_API_KEY,
            'X-BAPI-TIMESTAMP': str(timestamp),
            'X-BAPI-SIGN': signature,
            'X-BAPI-RECV-WINDOW': str(self._recv_window),
            'Content-Type': 'application/json'
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.config.BYBIT_REST_URL}/v5/order/create",
                                        headers=headers, json=params) as response:
                    data = await response.json()
                    if data.get('retCode') == 0:
                        logger.info(f"✅ Реальний ордер створено: {side} {quantity} {symbol} @ {price}")
                        return data['result']
                    else:
                        logger.error(f"Помилка створення ордера: {data}")
                        return {'error': data.get('retMsg', 'Unknown error')}
        except Exception as e:
            logger.error(f"Помилка створення ордера: {e}")
            return {'error': str(e)}

    async def create_order(self, symbol: str, side: str, order_type: str,
                           quantity: float, price: float = None) -> Dict:
        """Створення ордера (в залежності від режиму)"""
        if self.mode == 'simulation':
            return {
                'orderId': f"sim_{int(time.time())}_{symbol}",
                'symbol': symbol,
                'side': side,
                'price': price or 0,
                'quantity': quantity,
                'status': 'Filled' if order_type == 'Market' else 'New'
            }
        elif self.mode == 'monitor':
            logger.info(f"[МОНІТОРИНГ] {side} {quantity} {symbol} по ціні {price}")
            return {'status': 'monitor', 'simulated': True}
        elif self.mode == 'real':
            balance = await self.get_real_balance('USDT')
            cost = quantity * (price or 0)
            if balance < cost * 1.1:
                logger.error(f"❌ Недостатньо балансу! Потрібно ${cost:.2f}, є ${balance:.2f}")
                return {'error': f'Insufficient balance: need ${cost:.2f}, have ${balance:.2f}'}
            return await self.create_real_order(symbol, side, order_type, quantity, price)
        else:
            logger.warning(f"[РЕАЛЬНИЙ РЕЖИМ] {side} {quantity} {symbol}")
            return {'status': 'real', 'simulated': False}

    async def calculate_commission(self, side: str, quantity: float, price: float) -> float:
        return quantity * price * 0.001  # 0.1% замість 0.18%

    async def start_websocket(self, symbols: List[str]):
        """Запуск WebSocket для всіх символів"""
        self._ws_should_stop = False
        for symbol in symbols:
            task = asyncio.create_task(self._websocket_worker_with_backoff(symbol))
            self.ws_tasks.append(task)
        logger.info(f"Запущено WebSocket для {len(symbols)} символів")

    async def _websocket_worker_with_backoff(self, symbol: str):
        """Worker з експоненційним backoff для перепідключення"""
        delay = self._reconnect_delay

        while not self._ws_should_stop:
            try:
                await self._websocket_worker(symbol)
                # Якщо вийшли нормально (без помилки) - скидаємо затримку
                delay = self._reconnect_delay
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"⚠️ WebSocket {symbol} закрито: {e}. Перепідключення через {delay}с...")
                await asyncio.sleep(delay)
                delay = min(delay * self._reconnect_multiplier, self._max_reconnect_delay)
            except Exception as e:
                logger.error(f"❌ WebSocket помилка {symbol}: {e}. Перепідключення через {delay}с...")
                await asyncio.sleep(delay)
                delay = min(delay * self._reconnect_multiplier, self._max_reconnect_delay)

    async def _websocket_worker(self, symbol: str):
        """Основний WebSocket воркер"""
        ws_url = self.config.WS_URL

        async with websockets.connect(
                ws_url,
                ping_interval=20,  # пінг кожні 20 секунд
                ping_timeout=10,  # таймаут пінга 10 секунд
                close_timeout=5  # таймаут закриття 5 секунд
        ) as websocket:
            self.ws_connections[symbol] = websocket

            # Підписка на ticker
            subscribe_msg = {"op": "subscribe", "args": [f"tickers.{symbol}"]}
            await websocket.send(json.dumps(subscribe_msg))
            logger.info(f"✅ Підключено WebSocket для {symbol} (ping_interval=20s)")

            # Відправка pong на ping (автоматично через ping_interval)
            try:
                async for message in websocket:
                    data = json.loads(message)

                    # Обробка pong відповіді
                    if 'op' in data and data['op'] == 'pong':
                        continue

                    # Обробка ціни
                    if 'data' in data and 'lastPrice' in data.get('data', {}):
                        price = float(data['data']['lastPrice'])
                        self.current_prices[symbol] = price

                        # Сповіщаємо всіх підписників
                        for callback in self.price_callbacks:
                            try:
                                await callback(symbol, price)
                            except Exception as e:
                                logger.error(f"Помилка в callback для {symbol}: {e}")

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket {symbol} з'єднання закрито: {e}")
                raise

    def add_price_callback(self, callback):
        """Додавання callback для оновлення ціни"""
        self.price_callbacks.append(callback)

    async def stop_websocket(self):
        """Зупинка всіх WebSocket з'єднань"""
        self._ws_should_stop = True
        for symbol, ws in self.ws_connections.items():
            try:
                await ws.close()
                logger.info(f"WebSocket {symbol} закрито")
            except Exception as e:
                logger.error(f"Помилка закриття WebSocket {symbol}: {e}")

        # Скасовуємо всі задачі
        for task in self.ws_tasks:
            if not task.done():
                task.cancel()

        # Чекаємо завершення
        if self.ws_tasks:
            await asyncio.gather(*self.ws_tasks, return_exceptions=True)

        self.ws_tasks.clear()
        self.ws_connections.clear()
        logger.info("Всі WebSocket з'єднання зупинено")