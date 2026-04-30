"""Стратегія для ручних сигналів з Telegram"""
import logging
import re
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import json
from trading.strategies.base import BaseStrategy
from database.db import get_db, add_log
from config_manager import get_strategy_settings, save_strategy_settings

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Модель сигналу"""
    id: str
    symbol: str
    signal_type: str  # LONG or SHORT
    entry_price: float
    entry_limit: Optional[float]
    stop_loss: float
    take_profits: List[float]
    trade_size_usdt: float
    created_at: datetime
    status: str = "pending"  # pending, active, closed, cancelled
    order_id: Optional[str] = None
    closed_at: Optional[datetime] = None
    total_pnl: float = 0.0
    partial_closes: List[dict] = field(default_factory=list)
    error_message: Optional[str] = None  # Додано поле для помилок


class SignalStrategy(BaseStrategy):
    """Стратегія для ручних сигналів з Telegram"""

    def __init__(self, strategy_id: int, name: str, mode: str, exchange):
        super().__init__(strategy_id, name, mode, exchange)

        saved = get_strategy_settings('signals')
        self.default_trade_size = saved.get('trade_size_usdt', 20)
        self.enabled = saved.get('enabled', False)
        self.symbols = saved.get('symbols', [])

        # Стан
        self.balance = 100.0  # Віртуальний баланс
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        # Активні сигнали
        self.active_signals: Dict[str, Signal] = {}
        self.pending_signals: Dict[str, Signal] = {}

        self._load_history()
        logger.info(f"SignalStrategy ініціалізовано, баланс: ${self.balance}")

    async def start(self):
        await super().start()
        save_strategy_settings('signals', enabled=True)
        logger.info("SignalStrategy: запущено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, True)

    async def stop(self):
        await super().stop()
        save_strategy_settings('signals', enabled=False)
        logger.info("SignalStrategy: зупинено")
        if self.telegram_bot:
            await self.telegram_bot.send_strategy_status(self.name, False)

    @property
    def available_balance(self):
        return self.balance - self.locked_balance

    def get_current_balance(self) -> float:
        return self.balance

    def _load_history(self):
        """Завантаження історії з БД"""
        with get_db() as conn:
            # Баланс
            bal = conn.execute(
                "SELECT amount FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                (self.strategy_id,)
            ).fetchone()
            if bal:
                self.balance = bal['amount']
            else:
                self.balance = 100.0
                self._save_balance()

            # Статистика
            stats = conn.execute(
                "SELECT SUM(pnl) as pnl, COUNT(*) as cnt FROM orders WHERE strategy_id=? AND status='closed'",
                (self.strategy_id,)
            ).fetchone()
            if stats and stats['pnl']:
                self.total_pnl = stats['pnl']
                self.total_trades = stats['cnt']

            win = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE strategy_id=? AND status='closed' AND pnl>0",
                (self.strategy_id,)
            ).fetchone()
            self.winning_trades = win[0] if win else 0
            self.losing_trades = self.total_trades - self.winning_trades

            # Активні сигнали (відкриті ордери)
            orders = conn.execute("""
                SELECT o.*, s.name as strategy_name 
                FROM orders o
                LEFT JOIN strategies s ON o.strategy_id = s.id
                WHERE o.strategy_id=? AND o.status='open'
            """, (self.strategy_id,)).fetchall()

            for order in orders:
                o = dict(order)

                # Відновлюємо take_profits з рядка
                take_profits = []
                if o.get('take_profits'):
                    try:
                        take_profits = [float(x) for x in o['take_profits'].split(',')]
                    except:
                        take_profits = []

                # Відновлюємо partial_closes з JSON
                partial_closes = []
                if o.get('partial_closes'):
                    try:
                        partial_closes = json.loads(o['partial_closes'])
                    except:
                        partial_closes = []

                # Визначаємо сигнал типу
                signal_type = o.get('signal_type') or (
                    o.get('order_type') if o.get('order_type') in ['LONG', 'SHORT'] else 'LONG')

                # Відновлюємо сигнал
                signal = Signal(
                    id=o['order_id'],
                    symbol=o['symbol'],
                    signal_type=signal_type,
                    entry_price=o['price'],
                    entry_limit=None,
                    stop_loss=o.get('stop_loss', 0) or 0,
                    take_profits=take_profits,
                    trade_size_usdt=o['quantity'] * o['price'],
                    created_at=datetime.fromisoformat(o['opened_at']),
                    status='active',
                    order_id=o['order_id'],
                    partial_closes=partial_closes
                )
                self.active_signals[signal.id] = signal
                self.locked_balance += o['quantity'] * o['price']

                logger.info(
                    f"Відновлено сигнал {signal.id}: {signal.signal_type} {signal.symbol}, SL={signal.stop_loss}")

    def _save_balance(self):
        with get_db() as conn:
            conn.execute(
                "DELETE FROM balances WHERE strategy_id=? AND asset='USDT' AND symbol IS NULL",
                (self.strategy_id,)
            )
            conn.execute(
                "INSERT INTO balances (strategy_id, asset, amount, mode, updated_at) VALUES (?,?,?,?,?)",
                (self.strategy_id, 'USDT', self.balance, self.mode, datetime.now().isoformat())
            )

    def _save_signal(self, signal: Signal):
        """Збереження сигналу в БД"""
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO signals 
                (id, strategy_id, symbol, signal_type, entry_price, entry_limit, stop_loss, 
                 take_profits, trade_size_usdt, status, created_at, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.id, self.strategy_id, signal.symbol, signal.signal_type, signal.entry_price,
                signal.entry_limit, signal.stop_loss, ','.join(map(str, signal.take_profits)),
                signal.trade_size_usdt, signal.status, signal.created_at.isoformat(),
                signal.error_message
            ))

    def _save_order(self, order_id: str, symbol: str, side: str, price: float,
                    quantity: float, status: str, signal: Signal = None):
        """Збереження ордера з додатковою інформацією про сигнал"""
        with get_db() as conn:
            if signal:
                # Зберігаємо всі дані сигналу
                conn.execute("""
                    INSERT OR REPLACE INTO orders 
                    (order_id, strategy_id, symbol, side, price, quantity, status, 
                     order_type, opened_at, stop_loss, take_profits, signal_type, partial_closes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, self.strategy_id, symbol, side, price, quantity, status,
                    'Market', datetime.now().isoformat(),
                    signal.stop_loss,
                    ','.join(map(str, signal.take_profits)),
                    signal.signal_type,
                    json.dumps(signal.partial_closes)
                ))
            else:
                # Звичайне збереження
                conn.execute("""
                    INSERT OR REPLACE INTO orders 
                    (order_id, strategy_id, symbol, side, price, quantity, status, order_type, opened_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (order_id, self.strategy_id, symbol, side, price, quantity, status, 'Market',
                      datetime.now().isoformat()))

    def _update_order(self, order_id: str, pnl: float = None, commission: float = None,
                      status: str = None, closed_price: float = None):
        with get_db() as conn:
            updates = []
            params = []
            if pnl is not None:
                updates.append("pnl = ?")
                params.append(pnl)
            if commission is not None:
                updates.append("commission = ?")
                params.append(commission)
            if status is not None:
                updates.append("status = ?")
                params.append(status)
                updates.append("closed_at = ?")
                params.append(datetime.now().isoformat())
            if closed_price is not None:
                updates.append("closed_price = ?")
                params.append(closed_price)
            if updates:
                query = f"UPDATE orders SET {', '.join(updates)} WHERE order_id = ?"
                params.append(order_id)
                conn.execute(query, params)

    def parse_signal_text(self, text: str) -> Optional[Dict]:
        """Парсинг тексту сигналу з різних джерел"""
        try:
            logger.info(f"📝 Парсинг тексту: {text[:200]}...")

            # Визначаємо тип сигналу
            if '🟢' in text or 'LONG' in text.upper():
                signal_type = 'LONG'
            elif '🔴' in text or 'SHORT' in text.upper():
                signal_type = 'SHORT'
            else:
                logger.warning("Не вдалося визначити тип сигналу (LONG/SHORT)")
                return None

            # Шукаємо символ - різні варіанти
            symbol = None

            # Варіант 1: $ZETA
            symbol_match = re.search(r'\$([A-Z]{2,10})', text)
            if symbol_match:
                symbol = symbol_match.group(1)

            # Варіант 2: LONG - $ZETA або LONG - ZETA
            if not symbol:
                symbol_match = re.search(r'(?:LONG|SHORT)\s*[-:]\s*\$?([A-Z]{2,10})', text, re.IGNORECASE)
                if symbol_match:
                    symbol = symbol_match.group(1)

            # Варіант 3: просто ZETA в тексті
            if not symbol:
                symbol_match = re.search(r'\b([A-Z]{3,8})\b', text)
                if symbol_match and symbol_match.group(1) not in ['LONG', 'SHORT', 'ENTRY', 'LIMIT', 'RISK']:
                    symbol = symbol_match.group(1)

            if not symbol:
                logger.warning("Не вдалося визначити символ")
                return None

            symbol = symbol.upper() + 'USDT'
            logger.info(f"Визначено символ: {symbol}")

            # Entry price (різні формати)
            entry_price = None

            # Entry market: 0.05737
            entry_match = re.search(r'Entry market[:\s]*([\d.]+)', text, re.IGNORECASE)
            if entry_match:
                entry_price = float(entry_match.group(1))

            # Entry: 0.05737
            if not entry_price:
                entry_match = re.search(r'Entry[:\s]*([\d.]+)', text, re.IGNORECASE)
                if entry_match:
                    entry_price = float(entry_match.group(1))

            # - Entry: 0.05737
            if not entry_price:
                entry_match = re.search(r'[-•*]\s*Entry[:\s]*([\d.]+)', text, re.IGNORECASE)
                if entry_match:
                    entry_price = float(entry_match.group(1))

            if not entry_price:
                logger.warning("Не вдалося визначити Entry price")
                return None

            logger.info(f"Визначено Entry: {entry_price}")

            # Stop Loss
            stop_loss = None

            sl_match = re.search(r'SL[:\s]*([\d.]+)', text, re.IGNORECASE)
            if sl_match:
                stop_loss = float(sl_match.group(1))

            if not stop_loss:
                sl_match = re.search(r'Stop[_-]?Loss[:\s]*([\d.]+)', text, re.IGNORECASE)
                if sl_match:
                    stop_loss = float(sl_match.group(1))

            if not stop_loss:
                sl_match = re.search(r'[-•*]\s*SL[:\s]*([\d.]+)', text, re.IGNORECASE)
                if sl_match:
                    stop_loss = float(sl_match.group(1))

            if not stop_loss:
                logger.warning("Не вдалося визначити Stop Loss")
                return None

            logger.info(f"Визначено SL: {stop_loss}")

            # Take Profits (всі рівні)
            take_profits = []

            # TP1, TP2, TP3, TP4
            tp_matches = re.findall(r'TP\d+[:\s]*([\d.]+)', text, re.IGNORECASE)
            if tp_matches:
                take_profits = [float(tp) for tp in tp_matches]

            # Якщо не знайшло TP з цифрами, шукаємо просто числа після TP
            if not take_profits:
                tp_matches = re.findall(r'📈\s*TP\d+[:\s]*([\d.]+)', text)
                take_profits = [float(tp) for tp in tp_matches]

            # Якщо є TP але в іншому форматі
            if not take_profits:
                # Шукаємо всі числа після слів TP
                lines = text.split('\n')
                for line in lines:
                    if 'TP' in line.upper():
                        nums = re.findall(r'([\d.]+)', line)
                        for num in nums:
                            if float(num) != entry_price and float(num) != stop_loss:
                                take_profits.append(float(num))

            if not take_profits:
                # Якщо немає TP, створюємо за замовчуванням
                if signal_type == 'LONG':
                    take_profits = [
                        round(entry_price * 1.01, 8),
                        round(entry_price * 1.02, 8),
                        round(entry_price * 1.03, 8)
                    ]
                else:
                    take_profits = [
                        round(entry_price * 0.99, 8),
                        round(entry_price * 0.98, 8),
                        round(entry_price * 0.97, 8)
                    ]

            logger.info(f"Визначено TP: {take_profits[:3]}")

            # Entry limit (опціонально)
            entry_limit = None
            limit_match = re.search(r'Entry limit[:\s]*([\d.]+)', text, re.IGNORECASE)
            if limit_match:
                entry_limit = float(limit_match.group(1))

            result = {
                'symbol': symbol,
                'signal_type': signal_type,
                'entry_price': entry_price,
                'entry_limit': entry_limit,
                'stop_loss': stop_loss,
                'take_profits': take_profits
            }

            logger.info(f"✅ Сигнал успішно розпізнано: {result}")
            return result

        except Exception as e:
            logger.error(f"Помилка парсингу сигналу: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def add_signal(self, signal_data: Dict) -> Optional[Signal]:
        """Додавання нового сигналу"""
        if not self.can_trade(signal_data.get('trade_size_usdt', self.default_trade_size)):
            logger.warning(f"[Signals] Торгівля заблокована: {self._block_reason}")
            return None

        signal_id = f"sig_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"

        signal = Signal(
            id=signal_id,
            symbol=signal_data['symbol'],
            signal_type=signal_data['signal_type'],
            entry_price=signal_data['entry_price'],
            entry_limit=signal_data.get('entry_limit'),
            stop_loss=signal_data['stop_loss'],
            take_profits=signal_data['take_profits'],
            trade_size_usdt=signal_data.get('trade_size_usdt', self.default_trade_size),
            created_at=datetime.now(),
            status='pending'
        )

        self.pending_signals[signal_id] = signal
        self._save_signal(signal)

        # Telegram сповіщення
        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📡 *НОВИЙ СИГНАЛ*\n"
                f"└ {signal.signal_type} ${signal.symbol}\n"
                f"└ Entry: ${signal.entry_price:.8f}\n"
                f"└ SL: ${signal.stop_loss:.8f}\n"
                f"└ TP: {', '.join([f'${tp:.8f}' for tp in signal.take_profits[:3]])}\n"
                f"└ Розмір: ${signal.trade_size_usdt}",
                parse_mode='Markdown'
            )

        logger.info(f"[Signals] Додано сигнал {signal_id}: {signal.signal_type} {signal.symbol}")

        # Автоматичне відкриття позиції
        await self._open_position(signal)

        return signal

    async def check_symbol_supported(self, symbol: str) -> bool:
        """Перевірка чи підтримується символ біржею - спрощена версія"""
        try:
            # Просто намагаємося отримати ціну - якщо помилка, то символ не підтримується
            price = await self.exchange.get_current_price(symbol)
            return price > 0
        except Exception as e:
            error_msg = str(e)
            if 'Not supported symbols' in error_msg or '10001' in error_msg:
                return False
            logger.error(f"Помилка перевірки символу {symbol}: {e}")
            return False

    async def _open_position(self, signal: Signal):
        """Відкриття позиції за сигналом"""
        try:
            # ========== ПЕРЕВІРКА: чи підтримується символ біржею ==========
            is_valid = await self.check_symbol_supported(signal.symbol)
            if not is_valid:
                error_msg = f"❌ Пара {signal.symbol} не підтримується Bybit"
                logger.error(error_msg)

                # Оновлюємо статус сигналу
                signal.status = 'failed'
                signal.error_message = error_msg
                self._save_signal(signal)

                if signal.id in self.pending_signals:
                    del self.pending_signals[signal.id]

                if self.telegram_bot:
                    await self.telegram_bot.send_notification(
                        f"⚠️ *НЕВДАЛОСЬ ВІДКРИТИ ПОЗИЦІЮ*\n"
                        f"└ Пара: `{signal.symbol}`\n"
                        f"└ Причина: {error_msg}\n"
                        f"└ Використовуйте тільки пари, які підтримуються Bybit\n"
                        f"└ Наприклад: BTCUSDT, ETHUSDT, SOLUSDT",
                        parse_mode='Markdown'
                    )
                return
            # ========== КІНЕЦЬ ПЕРЕВІРКИ ==========

            current_price = await self.exchange.get_current_price(signal.symbol)
            if current_price <= 0:
                logger.error(f"[Signals] Не вдалося отримати ціну для {signal.symbol}")
                # Позначаємо сигнал як failed якщо не вдалося отримати ціну
                signal.status = 'failed'
                signal.error_message = f"Не вдалося отримати ціну для {signal.symbol}"
                self._save_signal(signal)
                if signal.id in self.pending_signals:
                    del self.pending_signals[signal.id]
                return

            side = 'buy' if signal.signal_type == 'LONG' else 'sell'
            quantity = signal.trade_size_usdt / current_price
            cost = quantity * current_price

            if self.available_balance < cost:
                logger.warning(
                    f"[Signals] Недостатньо балансу: потрібно ${cost:.2f}, доступно ${self.available_balance:.2f}")
                signal.status = 'failed'
                signal.error_message = f"Недостатньо балансу: потрібно ${cost:.2f}, доступно ${self.available_balance:.2f}"
                self._save_signal(signal)
                if signal.id in self.pending_signals:
                    del self.pending_signals[signal.id]
                return

            # Перевіряємо чи можна створити ордер (мінімальна кількість)
            try:
                min_qty = await self.exchange.get_min_order_quantity(signal.symbol)
                if quantity < min_qty:
                    logger.warning(
                        f"[Signals] Кількість {quantity:.8f} менша за мінімальну {min_qty:.8f} для {signal.symbol}")
                    signal.status = 'failed'
                    signal.error_message = f"Кількість {quantity:.8f} менша за мінімальну {min_qty:.8f}"
                    self._save_signal(signal)
                    if signal.id in self.pending_signals:
                        del self.pending_signals[signal.id]
                    return
            except Exception as e:
                logger.warning(f"Не вдалося перевірити min quantity для {signal.symbol}: {e}")

            order_result = await self.exchange.create_order(
                signal.symbol, side, 'Market', quantity, current_price
            )

            if order_result.get('error'):
                error_msg = order_result.get('error', 'Невідома помилка')
                logger.error(f"[Signals] Помилка відкриття позиції: {error_msg}")
                signal.status = 'failed'
                signal.error_message = f"Помилка біржі: {error_msg}"
                self._save_signal(signal)
                if signal.id in self.pending_signals:
                    del self.pending_signals[signal.id]
                return

            order_id = f"sig_order_{signal.id}"

            # ВИПРАВЛЕНО: правильне збереження ордера
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO orders 
                    (order_id, strategy_id, symbol, side, price, quantity, status, 
                     order_type, opened_at, stop_loss, take_profits, signal_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, self.strategy_id, signal.symbol, side, current_price, quantity, 'open',
                    'Market',
                    datetime.now().isoformat(),
                    signal.stop_loss,
                    ','.join(map(str, signal.take_profits)),
                    signal.signal_type
                ))

            signal.status = 'active'
            signal.order_id = order_id
            signal.entry_price = current_price
            signal.error_message = None
            self.active_signals[signal.id] = signal
            if signal.id in self.pending_signals:
                del self.pending_signals[signal.id]

            self.locked_balance += cost
            self._save_signal(signal)

            logger.info(f"[Signals] ✅ Відкрито позицію {signal.symbol} @ ${current_price:.8f}, SL={signal.stop_loss}")

            if self.telegram_bot:
                await self.telegram_bot.send_notification(
                    f"✅ *ВІДКРИТО ПОЗИЦІЮ*\n"
                    f"└ Сигнал: {signal.signal_type} ${signal.symbol}\n"
                    f"└ Ціна: ${current_price:.8f}\n"
                    f"└ SL: ${signal.stop_loss:.8f}\n"
                    f"└ TP: {', '.join([f'${tp:.8f}' for tp in signal.take_profits[:3]])}\n"
                    f"└ Розмір: ${signal.trade_size_usdt}",
                    parse_mode='Markdown'
                )

            asyncio.create_task(self._monitor_position(signal))

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Signals] Помилка відкриття позиції: {error_msg}")
            try:
                signal.status = 'failed'
                signal.error_message = f"Виняток: {error_msg}"
                self._save_signal(signal)
                if signal.id in self.pending_signals:
                    del self.pending_signals[signal.id]
            except:
                pass

    async def _monitor_position(self, signal: Signal):
        """Моніторинг відкритої позиції"""
        while signal.status == 'active':
            try:
                current_price = await self.exchange.get_current_price(signal.symbol)
                if current_price <= 0:
                    await asyncio.sleep(5)
                    continue

                # Розрахунок PnL
                if signal.signal_type == 'LONG':
                    pnl_percent = (current_price - signal.entry_price) / signal.entry_price * 100
                    # Перевірка TP
                    for i, tp in enumerate(signal.take_profits):
                        if current_price >= tp and not any(pc['tp_level'] == i + 1 for pc in signal.partial_closes):
                            await self._close_partial(signal, current_price, i + 1, tp)
                    # Перевірка SL
                    if current_price <= signal.stop_loss:
                        await self._close_position(signal, current_price, "stop_loss")
                        break
                else:  # SHORT
                    pnl_percent = (signal.entry_price - current_price) / signal.entry_price * 100
                    for i, tp in enumerate(signal.take_profits):
                        if current_price <= tp and not any(pc['tp_level'] == i + 1 for pc in signal.partial_closes):
                            await self._close_partial(signal, current_price, i + 1, tp)
                    if current_price >= signal.stop_loss:
                        await self._close_position(signal, current_price, "stop_loss")
                        break

                await asyncio.sleep(2)  # Перевірка кожні 2 секунди

            except Exception as e:
                logger.error(f"[Signals] Помилка моніторингу {signal.id}: {e}")
                await asyncio.sleep(5)

    async def _close_partial(self, signal: Signal, price: float, tp_level: int, tp_price: float):
        """Часткове закриття позиції при досягненні TP"""
        close_percent = 0.25  # Закриваємо 25% на кожному TP
        quantity_to_close = (signal.trade_size_usdt / signal.entry_price) * close_percent

        # Розрахунок PnL для цієї частини
        if signal.signal_type == 'LONG':
            pnl = quantity_to_close * (price - signal.entry_price)
        else:
            pnl = quantity_to_close * (signal.entry_price - price)

        commission = quantity_to_close * price * 0.0018
        pnl -= commission

        # Оновлюємо баланс
        self.locked_balance -= quantity_to_close * signal.entry_price
        self.balance += quantity_to_close * price - commission
        self.total_pnl += pnl
        self.total_trades += 1

        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        signal.partial_closes.append({
            'tp_level': tp_level,
            'price': price,
            'quantity': quantity_to_close,
            'pnl': pnl,
            'timestamp': datetime.now().isoformat()
        })

        logger.info(f"[Signals] 📍 Часткове закриття TP{tp_level}: {signal.symbol} @ ${price:.8f}, PnL: ${pnl:.2f}")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"🎯 *TP{tp_level} ДОСЯГНУТО*\n"
                f"└ {signal.signal_type} ${signal.symbol}\n"
                f"└ Ціна: ${price:.8f}\n"
                f"└ PnL: ${pnl:.2f}",
                parse_mode='Markdown'
            )

        self._save_balance()
        self.update_balance_for_drawdown()

        # Якщо всі TP виконані - закриваємо повністю
        if len(signal.partial_closes) >= len(signal.take_profits):
            await self._close_position(signal, price, "all_tp_hit")

    async def _close_position(self, signal: Signal, price: float, reason: str):
        """Повне закриття позиції"""
        if signal.status != 'active':
            return

        # Розрахунок залишку
        closed_quantity = sum(pc['quantity'] for pc in signal.partial_closes)
        remaining_quantity = (signal.trade_size_usdt / signal.entry_price) - closed_quantity

        if remaining_quantity > 0:
            if signal.signal_type == 'LONG':
                pnl = remaining_quantity * (price - signal.entry_price)
            else:
                pnl = remaining_quantity * (signal.entry_price - price)

            commission = remaining_quantity * price * 0.0018
            pnl -= commission

            self.locked_balance -= remaining_quantity * signal.entry_price
            self.balance += remaining_quantity * price - commission
            self.total_pnl += pnl
            self.total_trades += 1

            if pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1

            logger.info(f"[Signals] Закрито залишок: ${pnl:.2f}")

        signal.status = 'closed'
        signal.closed_at = datetime.now()
        signal.total_pnl = sum(pc['pnl'] for pc in signal.partial_closes) + pnl if remaining_quantity > 0 else sum(
            pc['pnl'] for pc in signal.partial_closes)

        # Оновлюємо ордер в БД
        if signal.order_id:
            self._update_order(signal.order_id, status='closed', closed_price=price, pnl=signal.total_pnl)

        self._save_signal(signal)

        reason_text = {
            'stop_loss': '🛑 Stop Loss',
            'all_tp_hit': '🎯 Всі TP виконані',
            'manual': '✋ Ручне закриття'
        }.get(reason, reason)

        pnl_icon = "✅" if signal.total_pnl >= 0 else "❌"

        logger.info(f"[Signals] 📊 Позицію закрито: {reason_text}, PnL: {pnl_icon} ${signal.total_pnl:.2f}")

        if self.telegram_bot:
            await self.telegram_bot.send_notification(
                f"📊 *ПОЗИЦІЮ ЗАКРИТО*\n"
                f"└ {signal.signal_type} ${signal.symbol}\n"
                f"└ Причина: {reason_text}\n"
                f"└ PnL: {pnl_icon} ${signal.total_pnl:.2f}",
                parse_mode='Markdown'
            )

        del self.active_signals[signal.id]
        self._save_balance()
        self.update_balance_for_drawdown()

    async def manual_close(self, signal_id: str, price: float = None) -> bool:
        """Ручне закриття позиції"""
        signal = self.active_signals.get(signal_id)
        if not signal:
            return False

        if not price:
            price = await self.exchange.get_current_price(signal.symbol)

        await self._close_position(signal, price, "manual")
        return True

    async def get_status(self) -> dict:
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades else 0

        # Отримуємо failed сигнали з БД
        failed_signals = []
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT id, symbol, signal_type, entry_price, stop_loss, take_profits, 
                       created_at, error_message
                FROM signals 
                WHERE strategy_id = ? AND status = 'failed'
                ORDER BY created_at DESC
                LIMIT 20
            """, (self.strategy_id,))
            for row in cursor.fetchall():
                failed_signals.append({
                    'id': row['id'],
                    'symbol': row['symbol'],
                    'signal_type': row['signal_type'],
                    'entry_price': float(row['entry_price']) if row['entry_price'] else 0,
                    'stop_loss': float(row['stop_loss']) if row['stop_loss'] else 0,
                    'take_profits': row['take_profits'].split(',') if row['take_profits'] else [],
                    'created_at': row['created_at'],
                    'error_message': row['error_message'] or ''
                })

        active_list = []
        for s in self.active_signals.values():
            active_list.append({
                'id': s.id,
                'symbol': s.symbol,
                'type': s.signal_type,
                'entry_price': float(s.entry_price),
                'stop_loss': float(s.stop_loss),
                'take_profits': [float(tp) for tp in s.take_profits],
                'partial_closes': len(s.partial_closes),
                'total_tp': len(s.take_profits),
                'created_at': s.created_at.isoformat() if hasattr(s.created_at, 'isoformat') else str(s.created_at)
            })

        return {
            'id': self.strategy_id,
            'name': self.name,
            'enabled': self.enabled,
            'mode': self.mode,
            'balance': round(self.balance, 2),
            'locked_balance': round(self.locked_balance, 2),
            'available_balance': round(self.available_balance, 2),
            'total_pnl': round(self.total_pnl, 2),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': round(win_rate, 1),
            'active_signals': active_list,
            'failed_signals': failed_signals,
            'pending_signals': len(self.pending_signals),
            'daily_trades_count': self.daily_trades_count,
            'max_daily_trades': self.max_daily_trades,
            'is_blocked': self._is_blocked,
            'block_reason': self._block_reason
        }

    async def reset(self):
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.active_signals.clear()
        self.pending_signals.clear()

        with get_db() as conn:
            conn.execute("DELETE FROM orders WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM balances WHERE strategy_id=?", (self.strategy_id,))
            conn.execute("DELETE FROM signals WHERE strategy_id=?", (self.strategy_id,))

        self._save_balance()
        await self.reset_limits()
        add_log("INFO", self.name, "Стратегію скинуто")

    async def emergency_stop(self):
        for signal_id in list(self.active_signals.keys()):
            await self.manual_close(signal_id)
        await self.stop()

    async def analyze(self) -> dict:
        return {'action': 'hold'}

    async def execute(self, signal: dict):
        pass