"""
Сервіс моніторингу електроенергії
Збирає дані та зберігає в окремій БД
"""
import asyncio
import time
import psutil
import os
from datetime import datetime
from typing import Dict, Optional
import uuid

from database.power_monitor_db import (
    get_power_db, init_power_db, start_new_session, end_current_session,
    add_power_record, update_daily_aggregates, get_power_settings,
    get_current_session, get_power_summary
)
from utils.logger_utils import setup_logger

logger = setup_logger('power_monitor')


class PowerMonitor:
    """Монітор споживання електроенергії"""

    def __init__(self):
        self.session_id = None
        self.running = False
        self.task = None
        self.start_time = None
        self.last_energy_kwh = 0
        self.last_cost_uah = 0

        # Параметри розрахунку
        self.base_power = 3.5  # W
        self.max_power = 6.5  # W
        self.psu_efficiency = 0.85
        self.cable_loss = 0.03
        self.price_per_kwh = 4.32  # грн

        self._load_settings()

    def _load_settings(self):
        """Завантаження налаштувань з БД"""
        try:
            settings = get_power_settings()
            self.base_power = float(settings.get('base_power_watts', 3.5))
            self.max_power = float(settings.get('max_power_watts', 6.5))
            self.psu_efficiency = float(settings.get('psu_efficiency', 0.85))
            self.cable_loss = float(settings.get('cable_loss', 0.03))
            self.price_per_kwh = float(settings.get('electricity_price', 4.32))
        except Exception as e:
            logger.error(f"Помилка завантаження налаштувань: {e}")

    async def start(self):
        """Запуск моніторингу"""
        if self.running:
            logger.warning("Моніторинг вже запущено")
            return

        # Ініціалізація БД
        init_power_db()

        # Створюємо нову сесію
        self.session_id = f"session_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        start_new_session(self.session_id)

        self.running = True
        self.start_time = time.time()
        self.task = asyncio.create_task(self._monitor_loop())
        logger.info(f"🔌 Моніторинг електроенергії запущено (сесія: {self.session_id})")
        logger.info(f"⏱️ Початковий час: {datetime.now().isoformat()}")

    async def stop(self):
        """Зупинка моніторингу"""
        if not self.running:
            return

        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        # Завершуємо сесію
        end_current_session()
        update_daily_aggregates()

        logger.info(f"🔌 Моніторинг електроенергії зупинено")

    async def _monitor_loop(self):
        """Основний цикл моніторингу"""
        logger.info("🟢 _monitor_loop запущено")

        last_record_time = time.time()
        last_aggregate_time = time.time()

        # Початкові значення для розрахунку енергії
        last_power = self._calculate_current_power()
        last_time = time.time()
        total_energy = 0
        total_cost = 0

        logger.info(f"Початкова потужність: {last_power:.2f} W")

        while self.running:
            try:
                current_time = time.time()

                # Отримуємо поточну потужність
                power_watts = self._calculate_current_power()

                # Розраховуємо спожиту енергію за інтервал
                delta_hours = (current_time - last_time) / 3600
                if delta_hours > 0:
                    avg_power = (last_power + power_watts) / 2
                    energy_kwh = avg_power * delta_hours / 1000
                    cost_uah = energy_kwh * self.price_per_kwh

                    total_energy += energy_kwh
                    total_cost += cost_uah

                    logger.debug(f"delta={delta_hours:.4f}год, енергія={energy_kwh:.8f}кВт⋅год")

                last_power = power_watts
                last_time = current_time

                # Зберігаємо запис КОЖНУ ХВИЛИНУ для тесту (потім повернемо на 1 годину)
                if current_time - last_record_time >= 60:  # Тимчасово 1 хвилина
                    timestamp = datetime.now()
                    cpu_percent = psutil.cpu_percent()
                    ram_percent = psutil.virtual_memory().percent

                    add_power_record(
                        timestamp=timestamp,
                        power_watts=power_watts,
                        energy_kwh=total_energy,
                        cost_uah=total_cost,
                        cpu_percent=cpu_percent,
                        ram_percent=ram_percent,
                        session_id=self.session_id
                    )

                    logger.info(f"📊 Запис споживання: потужність={power_watts:.2f}W, "
                                f"енергія={total_energy:.4f}кВт⋅год, витрати={total_cost:.2f}грн")
                    last_record_time = current_time

                # Оновлюємо денні агрегати щогодини
                if current_time - last_aggregate_time >= 3600:
                    update_daily_aggregates()
                    last_aggregate_time = current_time
                    logger.info("🔄 Оновлено денні агрегати")

                await asyncio.sleep(10)  # Перевірка кожні 10 секунд (для тесту)

            except asyncio.CancelledError:
                logger.info("⏹️ _monitor_loop скасовано")
                break
            except Exception as e:
                logger.error(f"❌ Помилка в _monitor_loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(30)

    def _calculate_current_power(self) -> float:
        """Розрахунок поточної споживаної потужності на основі навантаження CPU/RAM"""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            ram_percent = psutil.virtual_memory().percent

            # Коефіцієнт навантаження
            cpu_factor = 0.7 + (cpu_percent / 100) * 0.6
            ram_factor = 0.9 + (ram_percent / 100) * 0.2
            load_factor = (cpu_factor + ram_factor) / 2

            # Потужність Raspberry Pi в залежності від навантаження
            rpi_power = self.base_power + (self.max_power - self.base_power) * (load_factor - 0.7) / 0.6
            rpi_power = max(self.base_power, min(self.max_power, rpi_power))

            # Втрати на БЖ та кабелях
            total_power = rpi_power / self.psu_efficiency * (1 + self.cable_loss)

            return total_power

        except Exception as e:
            logger.error(f"Помилка розрахунку потужності: {e}")
            return self.base_power

    async def get_current_stats(self) -> Dict:
        """Отримання поточної статистики"""
        try:
            uptime_seconds = int(time.time() - self.start_time) if self.start_time else 0
            current_power = self._calculate_current_power()

            # Якщо монітор не запущений, повертаємо дані на основі uptime
            if not self.running or not self.start_time:
                return {
                    'current_power_watts': round(current_power, 2),
                    'uptime_seconds': 0,
                    'uptime_hours': 0,
                    'session_id': None,
                    'session_energy_kwh': 0,
                    'session_cost_uah': 0,
                    'total_energy_kwh': 0,
                    'total_cost_uah': 0,
                    'total_hours': 0,
                    'month_energy_kwh': 0,
                    'month_cost_uah': 0,
                    'year_energy_kwh': 0,
                    'year_cost_uah': 0
                }

            # Розраховуємо енергію за поточну сесію
            session_energy_kwh = (current_power * uptime_seconds / 3600) / 1000
            session_cost_uah = session_energy_kwh * self.price_per_kwh

            # Отримуємо дані з БД
            try:
                summary = get_power_summary()
            except Exception as e:
                logger.error(f"Помилка отримання summary: {e}")
                summary = {
                    'total_energy_kwh': session_energy_kwh,
                    'total_cost_uah': session_cost_uah,
                    'total_hours': uptime_seconds / 3600,
                    'month_energy_kwh': session_energy_kwh * 30,
                    'month_cost_uah': session_cost_uah * 30,
                    'year_energy_kwh': session_energy_kwh * 365,
                    'year_cost_uah': session_cost_uah * 365
                }

            return {
                'current_power_watts': round(current_power, 2),
                'uptime_seconds': uptime_seconds,
                'uptime_hours': round(uptime_seconds / 3600, 2),
                'session_id': self.session_id,
                'session_energy_kwh': round(session_energy_kwh, 4),
                'session_cost_uah': round(session_cost_uah, 2),
                'total_energy_kwh': round(summary.get('total_energy_kwh', session_energy_kwh), 2),
                'total_cost_uah': round(summary.get('total_cost_uah', session_cost_uah), 2),
                'total_hours': round(summary.get('total_hours', uptime_seconds / 3600), 2),
                'month_energy_kwh': round(summary.get('month_energy_kwh', session_energy_kwh * 30), 2),
                'month_cost_uah': round(summary.get('month_cost_uah', session_cost_uah * 30), 2),
                'year_energy_kwh': round(summary.get('year_energy_kwh', session_energy_kwh * 365), 2),
                'year_cost_uah': round(summary.get('year_cost_uah', session_cost_uah * 365), 2)
            }
        except Exception as e:
            logger.error(f"Помилка get_current_stats: {e}")
            return {
                'current_power_watts': 0,
                'uptime_seconds': 0,
                'uptime_hours': 0,
                'session_id': None,
                'session_energy_kwh': 0,
                'session_cost_uah': 0,
                'total_energy_kwh': 0,
                'total_cost_uah': 0,
                'total_hours': 0,
                'month_energy_kwh': 0,
                'month_cost_uah': 0,
                'year_energy_kwh': 0,
                'year_cost_uah': 0
            }


# Глобальний екземпляр
power_monitor = PowerMonitor()