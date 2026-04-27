"""Стратегія для ручних сигналів з Telegram"""
import logging
import re
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

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
            orders = conn.execute(
                "SELECT * FROM orders WHERE strategy_id=? AND status='open'",
                (self.strategy_id,)
            ).fetchall()
            for order in orders:
                o = dict(order)
                # Відновлюємо сигнал з метаданих
                signal = Signal(
                    id=o['order_id'],
                    symbol=o['symbol'],
                    signal_type=o.get('order_type', 'LONG'),
                    entry_price=o['price'],
                    entry_limit=None,
                    stop_loss=0,
                    take_profits=[],
                    trade_size_usdt=o['quantity'] * o['price'],
                    created_at=datetime.fromisoformat(o['opened_at']),
                    status='active',
                    order_id=o['order_id']
                )
                self.active_signals[signal.id] = signal
                self.locked_balance += o['quantity'] * o['price']

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
                (id, symbol, signal_type, entry_price, entry_limit, stop_loss, 
                 take_profits, trade_size_usdt, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.id, signal.symbol, signal.signal_type, signal.entry_price,
                signal.entry_limit, signal.stop_loss, ','.join(map(str, signal.take_profits)),
                signal.trade_size_usdt, signal.status, signal.created_at.isoformat()
            ))

    def _save_order(self, order_id: str, symbol: str, side: str, price: float, quantity: float, status: str):
        with get_db() as conn:
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
        """Парсинг тексту сигналу з Telegram"""
        try:
            # Визначаємо тип сигналу
            if '🟢' in text or 'LONG' in text.upper():
                signal_type = 'LONG'
            elif '🔴' in text or 'SHORT' in text.upper():
                signal_type = 'SHORT'
            else:
                return None

            # Шукаємо символ ($SYMBOL або просто SYMBOL)
            symbol_match = re.search(r'\$([A-Z]+)', text)
            if not symbol_match:
                symbol_match = re.search(r'([A-Z]{3,10})(?:\s|$)', text)
            if not symbol_match:
                return None
            symbol = symbol_match.group(1) + 'USDT'

            # Шукаємо Entry price
            entry_match = re.search(r'Entry[:\s]*([\d.]+)', text, re.IGNORECASE)
            if not entry_match:
                entry_match = re.search(r'market\(now\)[:\s]*([\d.]+)', text, re.IGNORECASE)
            entry_price = float(entry_match.group(1)) if entry_match else None

            # Entry limit (опціонально)
            entry_limit_match = re.search(r'Entry limit[:\s]*([\d.]+)', text, re.IGNORECASE)
            entry_limit = float(entry_limit_match.group(1)) if entry_limit_match else None

            # Stop Loss
            sl_match = re.search(r'SL[:\s]*([\d.]+)', text, re.IGNORECASE)
            stop_loss = float(sl_match.group(1)) if sl_match else None

            # Take Profits (всі рівні)
            tp_matches = re.findall(r'TP\d+[:\s]*([\d.]+)', text, re.IGNORECASE)
            take_profits = [float(tp) for tp in tp_matches]

            if not entry_price or not stop_loss or not take_profits:
                return None

            return {
                'symbol': symbol,
                'signal_type': signal_type,
                'entry_price': entry_price,
                'entry_limit': entry_limit,
                'stop_loss': stop_loss,
                'take_profits': take_profits
            }

        except Exception as e:
            logger.error(f"Помилка парсингу сигналу: {e}")
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

    async def _open_position(self, signal: Signal):
        """Відкриття позиції за сигналом"""
        try:
            # Отримуємо поточну ціну
            current_price = await self.exchange.get_current_price(signal.symbol)
            if current_price <= 0:
                logger.error(f"[Signals] Не вдалося отримати ціну для {signal.symbol}")
                return

            # Визначаємо сторону
            side = 'buy' if signal.signal_type == 'LONG' else 'sell'

            # Розраховуємо кількість
            quantity = signal.trade_size_usdt / current_price
            cost = quantity * current_price

            if self.available_balance < cost:
                logger.warning(f"[Signals] Недостатньо балансу: потрібно ${cost:.2f}")
                return

            # Створюємо ордер
            order_result = await self.exchange.create_order(
                signal.symbol, side, 'Market', quantity, current_price
            )

            if order_result.get('error'):
                logger.error(f"[Signals] Помилка відкриття позиції: {order_result}")
                return

            order_id = f"sig_order_{signal.id}"
            self._save_order(order_id, signal.symbol, side, current_price, quantity, 'open')

            # Оновлюємо сигнал
            signal.status = 'active'
            signal.order_id = order_id
            signal.entry_price = current_price
            self.active_signals[signal.id] = signal
            if signal.id in self.pending_signals:
                del self.pending_signals[signal.id]

            self.locked_balance += cost
            self._save_signal(signal)

            logger.info(f"[Signals] ✅ Відкрито позицію {signal.symbol} @ ${current_price:.8f}")

            # Telegram сповіщення
            if self.telegram_bot:
                await self.telegram_bot.send_notification(
                    f"✅ *ВІДКРИТО ПОЗИЦІЮ*\n"
                    f"└ Сигнал: {signal.signal_type} ${signal.symbol}\n"
                    f"└ Ціна: ${current_price:.8f}\n"
                    f"└ Розмір: ${signal.trade_size_usdt}",
                    parse_mode='Markdown'
                )

            # Запускаємо моніторинг
            asyncio.create_task(self._monitor_position(signal))

        except Exception as e:
            logger.error(f"[Signals] Помилка відкриття позиції: {e}")

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
            'active_signals': [
                {
                    'id': s.id,
                    'symbol': s.symbol,
                    'type': s.signal_type,
                    'entry_price': s.entry_price,
                    'stop_loss': s.stop_loss,
                    'take_profits': s.take_profits,
                    'partial_closes': len(s.partial_closes),
                    'total_tp': len(s.take_profits)
                }
                for s in self.active_signals.values()
            ],
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