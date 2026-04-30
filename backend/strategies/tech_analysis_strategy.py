import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from decimal import Decimal
import random
from enum import Enum

logger = logging.getLogger(__name__)


class SignalType(Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class ForecastStatus(Enum):
    ACTIVE = "active"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


class TechAnalysisStrategy:
    """
    Стратегія на основі технічного аналізу
    Аналізує 3 таймфрейми (1D, 4H, 1H) для прийняття рішень
    """

    def __init__(self, db_connection):
        self.db = db_connection
        self.name = "tech_analysis"
        self.enabled = False
        self.balance = 100.0
        self.locked_balance = 0.0
        self.total_pnl = 0.0
        self.total_trades = 0
        self.win_rate = 0.0
        self.wins = 0
        self.losses = 0

        # Налаштування за замовчуванням
        self.symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self.timeframes = ["1D", "4H", "1H"]  # Довгий, середній, короткий
        self.trade_size_percent = 50.0  # 50% від балансу на угоду
        self.stop_loss_percent = 2.0
        self.take_profit_percent = 4.0
        self.min_confidence = 65.0  # Мінімальна впевненість для входу (%)

        # Активні позиції та прогнози
        self.active_positions = {}  # symbol -> position
        self.forecasts = []  # Список прогнозів

        # Кеш для цін та індикаторів
        self.price_cache = {}
        self.indicator_cache = {}

    async def initialize(self):
        """Ініціалізація стратегії"""
        await self.load_settings()
        logger.info(f"TechAnalysisStrategy ініціалізовано з символами: {self.symbols}")

    async def load_settings(self):
        """Завантаження налаштувань з БД"""
        try:
            # ВАЖЛИВО: self.db може бути контекстним менеджером, а не з'єднанням
            if self.db is None:
                logger.warning("TechAnalysisStrategy: db is None, пропускаємо завантаження налаштувань")
                return

            # Перевіряємо чи є метод cursor()
            if hasattr(self.db, 'cursor'):
                cursor = self.db.cursor()
            else:
                # Якщо self.db це контекстний менеджер, використовуємо with
                with self.db as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM strategies WHERE name = ?", (self.name,))
                    strategy = cursor.fetchone()

                    if strategy:
                        self._load_from_row(strategy)
                    return

            cursor.execute("SELECT * FROM strategies WHERE name = ?", (self.name,))
            strategy = cursor.fetchone()

            if strategy:
                self._load_from_row(strategy)

        except Exception as e:
            logger.error(f"Помилка завантаження налаштувань: {e}")

    def _load_from_row(self, strategy):
        """Завантаження даних з рядка БД"""
        try:
            self.enabled = bool(strategy.get('enabled', False))
            self.balance = float(strategy.get('balance', 100.0))
            self.total_pnl = float(strategy.get('total_pnl', 0.0))
            self.total_trades = strategy.get('total_trades', 0)
            self.wins = strategy.get('wins', 0)
            self.losses = strategy.get('losses', 0)
            self.win_rate = (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

            # Завантаження налаштувань
            settings = strategy.get('settings', {})
            if settings:
                if isinstance(settings, str):
                    import json
                    settings = json.loads(settings)
                self.symbols = settings.get('symbols', self.symbols)
                self.trade_size_percent = settings.get('trade_size_percent', 50.0)
                self.stop_loss_percent = settings.get('stop_loss_percent', 2.0)
                self.take_profit_percent = settings.get('take_profit_percent', 4.0)
                self.min_confidence = settings.get('min_confidence', 65.0)
        except Exception as e:
            logger.error(f"Помилка завантаження даних стратегії: {e}")

    async def save_settings(self, settings: dict):
        """Збереження налаштувань"""
        try:
            if self.db is None:
                logger.warning("TechAnalysisStrategy: db is None, не можу зберегти налаштування")
                return False

            # Оновлюємо налаштування в пам'яті
            if 'symbols' in settings:
                self.symbols = settings['symbols']
            if 'trade_size_percent' in settings:
                self.trade_size_percent = settings['trade_size_percent']
            if 'stop_loss_percent' in settings:
                self.stop_loss_percent = settings['stop_loss_percent']
            if 'take_profit_percent' in settings:
                self.take_profit_percent = settings['take_profit_percent']
            if 'min_confidence' in settings:
                self.min_confidence = settings['min_confidence']

            import json
            settings_json = json.dumps(self.get_settings_dict())

            # Оновлюємо в БД
            if hasattr(self.db, 'cursor'):
                cursor = self.db.cursor()
                cursor.execute("""
                    UPDATE strategies 
                    SET settings = ? 
                    WHERE name = ?
                """, (settings_json, self.name))
                self.db.commit()
            else:
                with self.db as conn:
                    conn.execute("""
                        UPDATE strategies 
                        SET settings = ? 
                        WHERE name = ?
                    """, (settings_json, self.name))

            logger.info("Налаштування TechAnalysisStrategy збережено")
            return True

        except Exception as e:
            logger.error(f"Помилка збереження налаштувань: {e}")
            return False

    def get_settings_dict(self) -> dict:
        """Отримання налаштувань у вигляді словника"""
        return {
            'symbols': self.symbols,
            'trade_size_percent': self.trade_size_percent,
            'stop_loss_percent': self.stop_loss_percent,
            'take_profit_percent': self.take_profit_percent,
            'min_confidence': self.min_confidence,
            'timeframes': self.timeframes
        }

    async def analyze_symbol(self, symbol: str, klines_data: Dict[str, List]) -> Dict:
        """
        Аналіз символу на трьох таймфреймах
        Повертає: сигнал, впевненість, пояснення, прогнозовану ціну
        """
        result = {
            'signal': SignalType.NEUTRAL.value,
            'confidence': 0,
            'explanation': [],
            'target_price': None,
            'trend': 'neutral',
            'indicators': {}
        }

        try:
            # Аналіз кожного таймфрейму
            analysis = {}
            for tf in self.timeframes:
                if tf in klines_data and klines_data[tf]:
                    analysis[tf] = await self._analyze_timeframe(klines_data[tf], tf)

            if not analysis:
                return result

            # 1. Визначення довгострокового тренду (1D)
            long_term = analysis.get('1D', {})
            medium_term = analysis.get('4H', {})
            short_term = analysis.get('1H', {})

            # Збір індикаторів
            indicators = {
                '1D': long_term.get('indicators', {}),
                '4H': medium_term.get('indicators', {}),
                '1H': short_term.get('indicators', {})
            }
            result['indicators'] = indicators

            # Оцінка тренду
            trend_score = 0
            explanations = []

            # Аналіз EMA
            ema_1d = indicators['1D'].get('ema', {})
            ema_4h = indicators['4H'].get('ema', {})
            ema_1h = indicators['1H'].get('ema', {})

            # EMA 9 > 21 > 50 = висхідний тренд
            if ema_1d.get('ema9', 0) > ema_1d.get('ema21', 0) > ema_1d.get('ema50', 0):
                trend_score += 2
                explanations.append("📈 Довгостроковий тренд ВИСХІДНИЙ (EMA на 1D)")
            elif ema_1d.get('ema9', 0) < ema_1d.get('ema21', 0) < ema_1d.get('ema50', 0):
                trend_score -= 2
                explanations.append("📉 Довгостроковий тренд НИЗХІДНИЙ (EMA на 1D)")

            # Середньостроковий тренд (4H)
            if ema_4h.get('ema9', 0) > ema_4h.get('ema21', 0):
                trend_score += 1
                explanations.append("✅ Середньостроковий тренд ВИСХІДНИЙ (EMA на 4H)")
            else:
                trend_score -= 1
                explanations.append("⚠️ Середньостроковий тренд НИЗХІДНИЙ (EMA на 4H)")

            # RSI аналіз
            rsi_1h = indicators['1H'].get('rsi', 50)

            if rsi_1h < 30:
                trend_score += 1.5
                explanations.append(f"🟢 RSI = {rsi_1h:.1f} - ЗОНА ПЕРЕПРОДАНОСТІ (сигнал до buy)")
            elif rsi_1h > 70:
                trend_score -= 1.5
                explanations.append(f"🔴 RSI = {rsi_1h:.1f} - ЗОНА ПЕРЕКУПЛЕНОСТІ (сигнал до sell)")
            else:
                explanations.append(f"⚪ RSI = {rsi_1h:.1f} - НЕЙТРАЛЬНА ЗОНА")

            # MACD аналіз
            macd_1h = indicators['1H'].get('macd', {})
            macd_line = macd_1h.get('macd', 0)
            signal_line = macd_1h.get('signal', 0)

            if macd_line > signal_line:
                trend_score += 1
                explanations.append("📊 MACD вище сигнальної лінії - БИЧИЙ СИГНАЛ")
            else:
                trend_score -= 1
                explanations.append("📊 MACD нижче сигнальної лінії - ВЕДМЕЖИЙ СИГНАЛ")

            # Bollinger Bands
            bb = indicators['1H'].get('bollinger', {})
            current_price = short_term.get('current_price', 0)
            lower_band = bb.get('lower', 0)
            upper_band = bb.get('upper', 0)

            if lower_band and current_price <= lower_band * 1.01:
                trend_score += 1
                explanations.append(f"📉 Ціна біля нижньої смуги Bollinger - ПОТЕНЦІЙНИЙ ВІДСКОК ВГОРУ")
            elif upper_band and current_price >= upper_band * 0.99:
                trend_score -= 1
                explanations.append(f"📈 Ціна біля верхньої смуги Bollinger - ПОТЕНЦІЙНИЙ ВІДКОТ ВНИЗ")

            # Визначення підтримки/опору
            sr_levels = indicators['1H'].get('support_resistance', {})
            nearest_support = sr_levels.get('nearest_support', 0)
            nearest_resistance = sr_levels.get('nearest_resistance', 0)

            if nearest_support and current_price < nearest_support * 1.02:
                explanations.append(f"🛡️ Близько до рівня підтримки: ${nearest_support:.2f}")
            if nearest_resistance and current_price > nearest_resistance * 0.98:
                explanations.append(f"⚡ Близько до рівня опору: ${nearest_resistance:.2f}")

            # Розрахунок впевненості (0-100)
            max_score = 10
            confidence = min(100, max(0, (trend_score + max_score) / (max_score * 2) * 100))
            confidence = round(confidence, 1)

            # Визначення сигналу
            atr = indicators['1H'].get('atr', current_price * 0.01)
            target_offset = atr * 2.5

            if confidence >= self.min_confidence:
                if trend_score > 2:
                    signal = SignalType.LONG.value
                    target_price = current_price + target_offset
                    explanations.append(
                        f"🎯 ПРОГНОЗ: Ціна має зрости до ${target_price:.2f} (+{target_offset / current_price * 100:.1f}%)")
                elif trend_score < -2:
                    signal = SignalType.SHORT.value
                    target_price = current_price - target_offset
                    explanations.append(
                        f"🎯 ПРОГНОЗ: Ціна має впасти до ${target_price:.2f} ({-target_offset / current_price * 100:.1f}%)")
                else:
                    signal = SignalType.NEUTRAL.value
                    explanations.append("⚡ НЕЙТРАЛЬНИЙ СИГНАЛ - очікуємо чіткішого руху")
            else:
                signal = SignalType.NEUTRAL.value
                explanations.append(
                    f"⏳ Впевненість ({confidence}%) нижче мінімуму ({self.min_confidence}%) - сигналу немає")

            result.update({
                'signal': signal,
                'confidence': confidence,
                'explanation': explanations,
                'target_price': target_price if signal != SignalType.NEUTRAL.value else None,
                'trend': 'bullish' if trend_score > 0 else ('bearish' if trend_score < 0 else 'neutral'),
                'trend_score': trend_score
            })

        except Exception as e:
            logger.error(f"Помилка аналізу {symbol}: {e}")

        return result

    async def _analyze_timeframe(self, klines: List, timeframe: str) -> Dict:
        """Аналіз одного таймфрейму"""
        if not klines:
            return {}

        closes = [float(k['close']) for k in klines]
        highs = [float(k['high']) for k in klines]
        lows = [float(k['low']) for k in klines]
        current_price = closes[-1]

        result = {
            'current_price': current_price,
            'indicators': {}
        }

        # EMA
        result['indicators']['ema'] = self._calculate_ema(closes)

        # RSI
        result['indicators']['rsi'] = self._calculate_rsi(closes)

        # MACD
        result['indicators']['macd'] = self._calculate_macd(closes)

        # ATR (волатильність)
        result['indicators']['atr'] = self._calculate_atr(highs, lows, closes)

        # Bollinger Bands
        result['indicators']['bollinger'] = self._calculate_bollinger(closes)

        # Підтримка/опір
        result['indicators']['support_resistance'] = self._find_support_resistance(highs, lows)

        return result

    def _calculate_ema(self, prices: List[float], periods=[9, 21, 50]) -> Dict:
        """Розрахунок EMA"""
        result = {}
        for period in periods:
            if len(prices) >= period:
                k = 2 / (period + 1)
                ema = prices[:period]
                current_ema = sum(ema) / period
                for price in prices[period:]:
                    current_ema = price * k + current_ema * (1 - k)
                result[f'ema{period}'] = current_ema
            else:
                result[f'ema{period}'] = None
        return result

    def _calculate_rsi(self, prices: List[float], period=14) -> float:
        """Розрахунок RSI"""
        if len(prices) < period + 1:
            return 50.0

        gains = []
        losses = []

        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(-diff)

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def _calculate_macd(self, prices: List[float]) -> Dict:
        """Розрахунок MACD (12, 26, 9)"""
        if len(prices) < 26:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        ema12 = self._calculate_ema(prices, [12])['ema12']
        ema26 = self._calculate_ema(prices, [26])['ema26']

        if ema12 is None or ema26 is None:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        macd_line = ema12 - ema26

        # Сигнальна лінія (EMA 9 від MACD)
        macd_values = [0] * 9
        macd_values[-1] = macd_line
        signal_line = sum(macd_values) / 9

        return {
            'macd': macd_line,
            'signal': signal_line,
            'histogram': macd_line - signal_line
        }

    def _calculate_atr(self, highs: List[float], lows: List[float], closes: List[float], period=14) -> float:
        """Розрахунок ATR (Average True Range)"""
        if len(highs) < 2:
            return 0

        tr_values = []
        for i in range(1, len(highs)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr = max(hl, hc, lc)
            tr_values.append(tr)

        if len(tr_values) < period:
            return sum(tr_values) / len(tr_values) if tr_values else 0

        return sum(tr_values[-period:]) / period

    def _calculate_bollinger(self, prices: List[float], period=20, num_std=2) -> Dict:
        """Розрахунок Bollinger Bands"""
        if len(prices) < period:
            return {'upper': None, 'middle': None, 'lower': None}

        recent = prices[-period:]
        mean = sum(recent) / period
        variance = sum((p - mean) ** 2 for p in recent) / period
        std = variance ** 0.5

        return {
            'upper': mean + (std * num_std),
            'middle': mean,
            'lower': mean - (std * num_std)
        }

    def _find_support_resistance(self, highs: List[float], lows: List[float], window=5) -> Dict:
        """Пошук рівнів підтримки та опору"""
        if len(highs) < window * 2:
            return {'nearest_support': None, 'nearest_resistance': None}

        # Пошук локальних максимумів та мінімумів
        resistances = []
        supports = []

        for i in range(window, len(highs) - window):
            # Опір - локальний максимум
            if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
                    all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
                resistances.append(highs[i])

            # Підтримка - локальний мінімум
            if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
                    all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
                supports.append(lows[i])

        current_price = highs[-1] if highs else 0

        # Найближчі рівні
        nearest_resistance = min([r for r in resistances if r > current_price], default=None) if resistances else None
        nearest_support = max([s for s in supports if s < current_price], default=None) if supports else None

        return {
            'nearest_support': nearest_support,
            'nearest_resistance': nearest_resistance,
            'resistances': resistances[-5:],
            'supports': supports[-5:]
        }

    async def create_forecast(self, symbol: str, signal: str, target_price: float,
                              current_price: float, confidence: float, explanation: List[str]):
        """Створення прогнозу в БД"""
        try:
            if self.db is None:
                logger.warning("TechAnalysisStrategy: db is None, не можу створити прогноз")
                return

            forecast_hours = random.randint(1, 24)
            expires_at = datetime.now() + timedelta(hours=forecast_hours)

            if hasattr(self.db, 'cursor'):
                cursor = self.db.cursor()
                cursor.execute("""
                    INSERT INTO forecasts (
                        strategy, symbol, signal_type, entry_price, target_price,
                        confidence, explanation, status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.name, symbol, signal, current_price, target_price,
                    confidence, '\n'.join(explanation), ForecastStatus.ACTIVE.value,
                    datetime.now(), expires_at
                ))
                self.db.commit()
            else:
                with self.db as conn:
                    conn.execute("""
                        INSERT INTO forecasts (
                            strategy, symbol, signal_type, entry_price, target_price,
                            confidence, explanation, status, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        self.name, symbol, signal, current_price, target_price,
                        confidence, '\n'.join(explanation), ForecastStatus.ACTIVE.value,
                        datetime.now(), expires_at
                    ))

            logger.info(f"Створено прогноз для {symbol}: {signal} до ${target_price:.2f}")

        except Exception as e:
            logger.error(f"Помилка створення прогнозу: {e}")

    async def check_forecasts(self):
        """Перевірка активних прогнозів (чи збулися/прострочені)"""
        try:
            if self.db is None:
                return

            if hasattr(self.db, 'cursor'):
                cursor = self.db.cursor()
                cursor.execute("""
                    SELECT * FROM forecasts 
                    WHERE status = 'active' AND strategy = ?
                """, (self.name,))
                forecasts = cursor.fetchall()
            else:
                with self.db as conn:
                    cursor = conn.execute("""
                        SELECT * FROM forecasts 
                        WHERE status = 'active' AND strategy = ?
                    """, (self.name,))
                    forecasts = cursor.fetchall()

            current_prices = await self._get_current_prices()

            for forecast in forecasts:
                symbol = forecast['symbol']
                signal = forecast['signal_type']
                target = forecast['target_price']
                entry = forecast['entry_price']
                current = current_prices.get(symbol, 0)

                if current == 0:
                    continue

                expires_at = datetime.fromisoformat(forecast['expires_at'])
                if datetime.now() > expires_at:
                    status = ForecastStatus.EXPIRED.value
                    success = False
                else:
                    if signal == SignalType.LONG.value and current >= target:
                        status = ForecastStatus.SUCCESS.value
                        success = True
                    elif signal == SignalType.SHORT.value and current <= target:
                        status = ForecastStatus.SUCCESS.value
                        success = True
                    else:
                        continue

                if hasattr(self.db, 'cursor'):
                    cursor = self.db.cursor()
                    cursor.execute("""
                        UPDATE forecasts 
                        SET status = ?, resolved_at = ?, resolved_price = ?, success = ?
                        WHERE id = ?
                    """, (status, datetime.now(), current, 1 if success else 0, forecast['id']))
                    self.db.commit()
                else:
                    with self.db as conn:
                        conn.execute("""
                            UPDATE forecasts 
                            SET status = ?, resolved_at = ?, resolved_price = ?, success = ?
                            WHERE id = ?
                        """, (status, datetime.now(), current, 1 if success else 0, forecast['id']))

                logger.info(f"Прогноз для {symbol} {'збувся' if success else 'не збувся'}")

        except Exception as e:
            logger.error(f"Помилка перевірки прогнозів: {e}")

    async def _get_current_prices(self) -> Dict[str, float]:
        """Отримання поточних цін для символів"""
        return {symbol: 50000.0 for symbol in self.symbols}

    async def execute_trade(self, symbol: str, signal: str, price: float, size_usdt: float):
        """Виконання угоди (віртуально)"""
        try:
            if self.db is None:
                logger.warning("TechAnalysisStrategy: db is None, не можу виконати угоду")
                return

            quantity = size_usdt / price

            if hasattr(self.db, 'cursor'):
                cursor = self.db.cursor()
                cursor.execute("""
                    INSERT INTO orders (
                        strategy_id, symbol, side, price, quantity, status, opened_at
                    ) VALUES (
                        (SELECT id FROM strategies WHERE name = ?), ?, ?, ?, ?, 'open', ?
                    )
                """, (self.name, symbol, 'buy' if signal == SignalType.LONG.value else 'sell',
                      price, quantity, datetime.now()))
                self.db.commit()
            else:
                with self.db as conn:
                    conn.execute("""
                        INSERT INTO orders (
                            strategy_id, symbol, side, price, quantity, status, opened_at
                        ) VALUES (
                            (SELECT id FROM strategies WHERE name = ?), ?, ?, ?, ?, 'open', ?
                        )
                    """, (self.name, symbol, 'buy' if signal == SignalType.LONG.value else 'sell',
                          price, quantity, datetime.now()))

            self.locked_balance += size_usdt
            self.balance -= size_usdt

            self.active_positions[symbol] = {
                'entry_price': price,
                'quantity': quantity,
                'size_usdt': size_usdt,
                'signal': signal,
                'opened_at': datetime.now()
            }

            logger.info(f"Виконано угоду {signal} для {symbol}: {quantity} @ ${price:.2f}")

        except Exception as e:
            logger.error(f"Помилка виконання угоди: {e}")