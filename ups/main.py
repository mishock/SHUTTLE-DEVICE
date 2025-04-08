#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Монитор батареи UPS HAT (E) для Raspberry Pi
Основные функции:
- Мониторинг параметров батареи
- Защита от глубокого разряда
- Отправка данных на NATS сервер
- Логирование в файлы error_log.txt и data_log.json
"""

import os
import json
import time
import smbus
import fcntl
import configparser
import logging
from logging.handlers import RotatingFileHandler
import nats
import asyncio
from datetime import datetime
import signal
import sys

# Адрес устройства на шине I2C
UPS_ADDR = 0x2d

class UPSMonitor:
    """Класс для мониторинга UPS HAT (E) через I2C"""
    
    def __init__(self, i2c_bus=1):
        """Инициализация монитора UPS"""
        self.bus = smbus.SMBus(i2c_bus)
        self.low_voltage_counter = 0
        
        # Загрузка параметров из конфига
        self.nominal_capacity = int(config.get('battery', 'nominal_capacity'))
        self.cell_count = int(config.get('battery', 'cell_count'))
        self.low_voltage_cutoff = int(config.get('battery', 'low_voltage_cutoff'))
        self.max_voltage = int(config.get('battery', 'max_voltage'))
        self.min_voltage = int(config.get('battery', 'min_voltage'))
        self.battery_type = config.get('battery', 'battery_type')
        
        self.emergency_shutdown_voltage = int(config.get('protection', 'emergency_shutdown_voltage'))
        self.shutdown_threshold = int(config.get('protection', 'shutdown_threshold'))
        self.current_threshold = int(config.get('protection', 'shutdown_current_threshold'))
        
        self.current_calibration = float(config.get('calibration', 'current_calibration', fallback=1.0))
        self.current_offset = int(config.get('calibration', 'current_offset', fallback=0))

    def get_charging_state(self):
        """Получение текущего состояния зарядки"""
        try:
            data = self.bus.read_i2c_block_data(UPS_ADDR, 0x02, 0x01)
            if data[0] & 0x40:
                return "fast_charging"
            elif data[0] & 0x80:
                return "charging"
            elif data[0] & 0x20:
                return "discharging"
            return "idle"
        except Exception as e:
            error_logger.error(f"Ошибка чтения состояния зарядки: {e}")
            return "error"

    def get_vbus_data(self):
        """Чтение данных о входном питании"""
        try:
            data = self.bus.read_i2c_block_data(UPS_ADDR, 0x10, 0x06)
            return {
                'voltage_mv': data[0] | data[1] << 8,
                'current_ma': data[2] | data[3] << 8,
                'power_mw': data[4] | data[5] << 8
            }
        except Exception as e:
            error_logger.error(f"Ошибка чтения VBUS: {e}")
            return {'voltage_mv': 0, 'current_ma': 0, 'power_mw': 0}

    def get_battery_data(self):
        """Получение данных о батарее"""
        try:
            data = self.bus.read_i2c_block_data(UPS_ADDR, 0x20, 0x0C)
            raw_current = (data[2] | data[3] << 8)
            if raw_current > 0x7FFF:
                raw_current -= 0xFFFF
            
            calibrated_current = int(raw_current * self.current_calibration + self.current_offset)
            
            return {
                'voltage_mv': data[0] | data[1] << 8,
                'current_ma': calibrated_current,
                'percent': int(data[4] | data[5] << 8),
                'remaining_capacity_mah': data[6] | data[7] << 8,
                'time_to_empty_min': data[8] | data[9] << 8 if calibrated_current < 0 else None,
                'time_to_full_min': data[10] | data[11] << 8 if calibrated_current >= 0 else None,
                'raw_current_ma': raw_current
            }
        except Exception as e:
            error_logger.error(f"Ошибка чтения данных батареи: {e}")
            return {
                'voltage_mv': 0,
                'current_ma': 0,
                'percent': 0,
                'remaining_capacity_mah': 0,
                'time_to_empty_min': None,
                'time_to_full_min': None,
                'raw_current_ma': 0
            }

    def get_cell_voltages(self):
        """Получение напряжений на ячейках"""
        try:
            data = self.bus.read_i2c_block_data(UPS_ADDR, 0x30, 0x08)
            return [
                data[0] | data[1] << 8,
                data[2] | data[3] << 8,
                data[4] | data[5] << 8,
                data[6] | data[7] << 8
            ]
        except Exception as e:
            error_logger.error(f"Ошибка чтения напряжений ячеек: {e}")
            return [0, 0, 0, 0]

    def check_shutdown_condition(self, cell_voltages, battery_current):
        """Проверка условий для отключения"""
        try:
            if any(v < self.emergency_shutdown_voltage for v in cell_voltages):
                error_logger.critical(f"АВАРИЯ: Напряжение ниже {self.emergency_shutdown_voltage}мВ!")
                self.safe_shutdown()
                return True

            low_cells = sum(1 for v in cell_voltages if v < self.low_voltage_cutoff)
            
            if low_cells > 0 and abs(battery_current) < self.current_threshold:
                self.low_voltage_counter += 1
                if self.low_voltage_counter >= self.shutdown_threshold:
                    error_logger.warning(f"Достигнут порог отключения ({self.low_voltage_counter} циклов)")
                    self.safe_shutdown()
                return True
            
            self.low_voltage_counter = 0
            return False
        except Exception as e:
            error_logger.error(f"Ошибка проверки отключения: {e}")
            return False

    def safe_shutdown(self):
        """Безопасное отключение системы"""
        try:
            os.popen("i2cset -y 1 0x2d 0x01 0x55")
            os.system("sudo poweroff")
        except Exception as e:
            error_logger.error(f"Ошибка при отключении: {e}")

def setup_logger():
    """Настройка системы логирования"""
    log_path = config.get('settings', 'log_path').split(';')[0].strip()
    os.makedirs(log_path, exist_ok=True)

    # Настройка логгера ошибок (error_log.txt)
    error_log_path = os.path.join(log_path, 'error_log.txt')
    error_logger = logging.getLogger('error_logger')
    error_logger.setLevel(logging.ERROR)
    error_handler = RotatingFileHandler(
        error_log_path,
        maxBytes=1024*1024,
        backupCount=5
    )
    error_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    error_logger.addHandler(error_handler)

    # Инициализация файла data_log.json
    data_log_path = os.path.join(log_path, 'data_log.json')
    if not os.path.exists(data_log_path):
        with open(data_log_path, 'w') as f:
            json.dump([], f)

    data_logger = logging.getLogger('data_logger')
    data_logger.setLevel(logging.INFO)
    
    return error_logger, data_logger

def update_json_log(data):
    """Обновление JSON лога"""
    log_path = config.get('settings', 'log_path').split(';')[0].strip()
    data_log_path = os.path.join(log_path, 'data_log.json')
    
    try:
        if os.path.exists(data_log_path) and os.path.getsize(data_log_path) > 0:
            with open(data_log_path, 'r') as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    logs = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    logs = []
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        else:
            logs = []

        logs.append(data)

        with open(data_log_path, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(logs, f, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        error_logger.error(f'Ошибка обновления лога: {e}')

async def send_to_nats(data):
    """Отправка данных на NATS сервер"""
    try:
        nc = await nats.connect(
            config.get('settings', 'nats_server').split(';')[0].strip()
        )
        await nc.publish(
            config.get('settings', 'nats_topic').split(';')[0].strip(),
            json.dumps(data).encode()
        )
        await nc.close()
        return True
    except Exception as e:
        error_logger.error(f'Ошибка NATS: {e}')
        return False

async def main():
    """Основной цикл работы"""
    ups = UPSMonitor()
    buffer = []
    last_send_time = time.time()
    poll_interval = int(config.get('settings', 'poll_interval').split(';')[0].strip())
    send_interval = int(config.get('settings', 'send_interval').split(';')[0].strip())

    signal.signal(signal.SIGINT, signal_handler)

    while True:
        try:
            current_time = time.time()
            
            # Получение данных
            charging_state = ups.get_charging_state()
            vbus_data = ups.get_vbus_data()
            battery_data = ups.get_battery_data()
            cell_voltages = ups.get_cell_voltages()

            # Расчет времени
            time_to_empty = None
            if battery_data['current_ma'] < 0:
                if battery_data['time_to_empty_min']:
                    time_to_empty = battery_data['time_to_empty_min']
                else:
                    discharge_current = abs(battery_data['current_ma'])
                    if discharge_current > 0:
                        time_to_empty = int((battery_data['remaining_capacity_mah'] / discharge_current) * 60)

            time_to_full = None
            if battery_data['current_ma'] > 0:
                if battery_data['time_to_full_min']:
                    time_to_full = battery_data['time_to_full_min']
                else:
                    remaining_to_charge = ups.nominal_capacity - battery_data['remaining_capacity_mah']
                    if remaining_to_charge > 0 and battery_data['current_ma'] > 0:
                        time_to_full = int((remaining_to_charge / battery_data['current_ma']) * 60)

            # Проверка защиты
            shutdown_warning = ups.check_shutdown_condition(
                cell_voltages, 
                battery_data['current_ma']
            )

            # Формирование данных с правильным форматом времени
            data = {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'battery': {
                    'type': ups.battery_type,
                    'voltage_mv': battery_data['voltage_mv'],
                    'current_ma': battery_data['current_ma'],
                    'percent': battery_data['percent'],
                    'remaining_capacity_mah': battery_data['remaining_capacity_mah'],
                    'nominal_capacity_mah': ups.nominal_capacity,
                    'health_percent': min(100, int((
                        battery_data['remaining_capacity_mah'] / 
                        ups.nominal_capacity
                    ) * 100)),
                    'time_estimates': {
                        'time_to_empty_min': time_to_empty,
                        'time_to_full_min': time_to_full
                    },
                    'cells': [
                        {
                            'number': i+1,
                            'voltage_mv': v,
                            'status': 'ok' if v >= ups.low_voltage_cutoff else 'low',
                            'status_detail': (
                                'critical' if v < ups.emergency_shutdown_voltage else 
                                'warning' if v < ups.low_voltage_cutoff else 
                                'normal'
                            )
                        } for i, v in enumerate(cell_voltages)
                    ]
                },
                'charging': {
                    'state': charging_state,
                    'vbus_voltage_mv': vbus_data['voltage_mv'],
                    'vbus_current_ma': vbus_data['current_ma'],
                    'vbus_power_mw': vbus_data['power_mw']
                },
                'protection': {
                    'low_voltage_warning': shutdown_warning,
                    'low_voltage_counter': ups.low_voltage_counter,
                    'thresholds': {
                        'emergency_shutdown_mv': ups.emergency_shutdown_voltage,
                        'low_voltage_cutoff_mv': ups.low_voltage_cutoff,
                        'shutdown_current_threshold_ma': ups.current_threshold
                    }
                },
                'calibration': {
                    'current_calibration_factor': ups.current_calibration,
                    'current_offset_ma': ups.current_offset,
                    'raw_current_ma': battery_data['raw_current_ma']
                }
            }

            update_json_log(data)
            buffer.append(data)

            # Отправка данных
            if current_time - last_send_time >= send_interval and buffer:
                success = True
                for item in buffer:
                    if not await send_to_nats(item):
                        success = False
                        break
                
                if success:
                    buffer = []
                    last_send_time = current_time

            await asyncio.sleep(poll_interval)

        except Exception as e:
            error_logger.error(f'Ошибка в основном цикле: {e}')
            await asyncio.sleep(5)

def signal_handler(sig, frame):
    """Обработчик сигналов прерывания"""
    print("\nЗавершение работы...")
    sys.exit(0)

if __name__ == '__main__':
    # Загрузка конфигурации
    config = configparser.ConfigParser()
    config.read('/app/config/settings.ini')

    # Инициализация логгеров
    error_logger, data_logger = setup_logger()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Сервис остановлен пользователем")
    except Exception as e:
        error_logger.critical(f'Критическая ошибка: {e}')
    finally:
        logging.shutdown()