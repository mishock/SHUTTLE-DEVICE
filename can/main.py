#!/usr/bin/env python3

import can
import json
import time
import configparser
from nats.aio.client import Client as NATS
import asyncio
from datetime import datetime
import os
import socket
import logging
from logging.handlers import RotatingFileHandler
import subprocess

# Инициализация конфигурации
config = configparser.ConfigParser()
config.read("/app/config/settings.ini")

# Проверка обязательных секций
required_sections = ['CAN', 'LOGGING', 'NATS']
for section in required_sections:
    if section not in config:
        raise ValueError(f"Missing required section [{section}] in config file")

# Параметры CAN
can_interface = config['CAN']['interface']
can_bitrate = int(config['CAN']['bitrate'])
polling_period = float(config['CAN']['polling_period'])
can_retry_interval = float(config['CAN'].get('can_retry_interval', 5))
max_can_retries = int(config['CAN'].get('max_can_retries', 3))
request_id = int(config['CAN']['request_id'], 0) if 'request_id' in config['CAN'] else 0x7DF
response_ids = list(map(lambda x: int(x, 0), config['CAN']['response_ids'].split(','))) if 'response_ids' in config['CAN'] else [0x7E8]

# Параметры логирования
logging_enabled = config.getboolean('LOGGING', 'enabled', fallback=True)
data_log_file = config['LOGGING']['data_log_file']
error_log_file = config['LOGGING']['error_log_file']
log_max_bytes = int(config['LOGGING'].get('max_bytes', 1048576))  # 1MB
log_backup_count = int(config['LOGGING'].get('backup_count', 3))

# Параметры NATS
nats_enabled = config.getboolean('NATS', 'enabled', fallback=True)
nats_server = config['NATS']['server']
nats_topic = config['NATS']['topic']
buffer_size = int(config['NATS']['buffer_size'])
nats_send_interval = float(config['NATS'].get('send_interval', 2))
server_check_timeout = int(config['NATS'].get('server_check_timeout', 2))
connect_timeout = int(config['NATS'].get('connect_timeout', 5))
error_log_interval = int(config['NATS'].get('error_log_interval', 300))

# Глобальные переменные
bus = None
message_buffer = []
last_nats_error_time = None
last_nats_error_message = None

# Настройка логирования
def setup_logger():
    logger = logging.getLogger('can_monitor')
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Обработчик для файла ошибок с ротацией
    error_handler = RotatingFileHandler(
        error_log_file,
        maxBytes=log_max_bytes,
        backupCount=log_backup_count
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    # Обработчик для консоли
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()

def initialize_log_files():
    """Инициализация файлов логов"""
    try:
        os.makedirs(os.path.dirname(data_log_file), exist_ok=True)
        os.makedirs(os.path.dirname(error_log_file), exist_ok=True)
        
        if not os.path.exists(data_log_file) or os.path.getsize(data_log_file) == 0:
            with open(data_log_file, 'w') as f:
                json.dump([], f)
                
    except Exception as e:
        logger.error(f"Error initializing log files: {e}")

def is_can_interface_up(interface):
    """Проверка состояния CAN-интерфейса"""
    try:
        output = subprocess.check_output(["ip", "link", "show", interface])
        return "UP" in output.decode()
    except Exception:
        return False

def bring_can_interface_up(interface):
    """Принудительный подъем CAN-интерфейса"""
    try:
        subprocess.run(["sudo", "ip", "link", "set", interface, "down"], check=True)
        subprocess.run(["sudo", "ip", "link", "set", interface, "up"], check=True)
        return True
    except Exception as e:
        logger.error(f"Failed to reset interface {interface}: {e}")
        return False

def connect_to_can():
    """Подключение к CAN-шине с фильтрацией по response_ids"""
    global bus
    retries = 0
    
    while retries < max_can_retries:
        try:
            # Настройка фильтров CAN
            can_filters = [{"can_id": can_id, "can_mask": 0x7FF} for can_id in response_ids]
            
            bus = can.interface.Bus(
                channel=can_interface,
                interface='socketcan',
                bitrate=can_bitrate,
                can_filters=can_filters
            )
            logger.info(f"Connected to {can_interface}, filtering IDs: {[hex(i) for i in response_ids]}")
            return True
        except Exception as e:
            retries += 1
            logger.error(f"CAN connection error (attempt {retries}/{max_can_retries}): {e}")
            time.sleep(can_retry_interval)
    
    logger.error("Failed to connect to CAN bus after maximum retries")
    return False

def send_obd_request(pid):
    """Отправка OBD-II запроса"""
    try:
        data = [0x02, 0x01, pid] + [0x00] * 5
        msg = can.Message(
            arbitration_id=request_id,
            data=data,
            is_extended_id=False
        )
        bus.send(msg)
        logger.debug(f"Sent OBD request: PID={hex(pid)}")
    except Exception as e:
        logger.error(f"Error sending OBD request: {e}")

def decode_obd_response(message):
    """Декодирование OBD-II ответа"""
    data = message.data
    if len(data) < 3 or data[1] != 0x41:  # 0x41 = ответ на режим 0x01
        return None
    
    return {
        'pid': data[2],
        'data': data[3:],
        'timestamp': message.timestamp
    }

async def check_nats_server_available():
    """Проверка доступности NATS сервера"""
    try:
        server, port = nats_server.split('://')[1].split(':')
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(server_check_timeout)
            s.connect((server, int(port)))
        return True
    except Exception:
        return False

async def send_to_nats(data):
    """Улучшенная функция отправки данных в NATS"""
    global last_nats_error_time, last_nats_error_message
    
    # Всегда логируем данные
    log_can_data(data)
    
    # Проверка доступности сервера
    if not await check_nats_server_available():
        error_msg = f"NATS server {nats_server} is unreachable"
        if error_msg != last_nats_error_message or (
            last_nats_error_time is None or 
            (datetime.now() - last_nats_error_time).total_seconds() > error_log_interval
        ):
            logger.error(error_msg)
            last_nats_error_message = error_msg
            last_nats_error_time = datetime.now()
        
        # Добавляем в буфер
        buffer_data(data)
        return False

    # Отправка данных
    nc = NATS()
    try:
        await nc.connect(
            nats_server,
            connect_timeout=connect_timeout,
            max_reconnect_attempts=1,
            reconnect_time_wait=1
        )
        await nc.publish(nats_topic, json.dumps(data).encode())
        await nc.flush()
        
        # Сбрасываем счетчик ошибок при успешной отправке
        if last_nats_error_message is not None:
            logger.info("NATS connection restored, successfully sent data")
            last_nats_error_message = None
            last_nats_error_time = None
            
        return True
        
    except Exception as e:
        error_msg = f"NATS publish error: {str(e)}"
        if error_msg != last_nats_error_message or (
            last_nats_error_time is None or 
            (datetime.now() - last_nats_error_time).total_seconds() > error_log_interval
        ):
            logger.error(error_msg)
            last_nats_error_message = error_msg
            last_nats_error_time = datetime.now()
        
        buffer_data(data)
        return False
    finally:
        await nc.close()

def buffer_data(data):
    """Буферизация данных при недоступности NATS"""
    if len(message_buffer) >= buffer_size:
        message_buffer.pop(0)
    message_buffer.append(data)

async def flush_buffer():
    """Отправка данных из буфера"""
    global message_buffer
    
    if not message_buffer:
        return True
        
    success = True
    for item in message_buffer[:]:
        if await send_to_nats(item):
            message_buffer.remove(item)
        else:
            success = False
            break
            
    return success

def log_can_data(data):
    """Логирование данных CAN"""
    try:
        # Чтение существующих данных
        try:
            with open(data_log_file, 'r') as f:
                log_entries = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log_entries = []
            
        # Добавление новой записи
        log_entries.append(data)
        
        # Запись обновленных данных
        with open(data_log_file, 'w') as f:
            json.dump(log_entries, f, indent=4)
    except Exception as e:
        logger.error(f"Error writing to data log: {e}")

# Функция для декодирования PID
def decode_pid(pid, data):
    """
    Декодирует данные OBD-II на основе PID.
    :param pid: PID (Parameter ID).
    :param data: Данные (байты, начиная с третьего байта в ответе).
    :return: Словарь с декодированным значением, единицей измерения и названием параметра.
    """
    if pid == 0x05:  # Температура охлаждающей жидкости
        return {'name': 'Engine Coolant Tem perature', 'value': data[0] - 40, 'unit': 'C'}
    elif pid == 0x0C:  # Обороты коленвала (Engine RPM)
        return {'name': 'Engine RPM', 'value': (data[0] * 256 + data[1]) / 4, 'unit': 'RPM'}
    elif pid == 0x0D:  # Скорость автомобиля
        return {'name': 'Vehicle Speed', 'value': data[0], 'unit': 'km/h'}
    elif pid == 0x0A:  # Давление топлива
        return {'name': 'Fuel Pressure', 'value': 3 * data[0], 'unit': 'kPa'}
    elif pid == 0x2F:  # Уровень топлива
        return {'name': 'Fuel Level', 'value': (100 / 255) * data[0], 'unit': '%'}
    elif pid == 0x10:  # Расход воздуха
        return {'name': 'Air Flow Rate', 'value': (data[0] * 256 + data[1]) / 100, 'unit': 'g/s'}
    elif pid == 0x11:  # Положение дроссельной заслонки
        return {'name': 'Throttle Position', 'value': (100 / 255) * data[0], 'unit': '%'}
    elif pid == 0x42:  # Напряжение АКБ
        return {'name': 'Battery Voltage', 'value': (data[0] * 256 + data[1]) / 1000, 'unit': 'V'}
    elif pid == 0x5C:  # Температура трансмиссионной жидкости
        return {'name': 'Transmission Fluid Temperature', 'value': data[0] - 40, 'unit': '°C'}
    elif pid == 0x23:  # Давление в топливной рампе (дизель или прямой впрыск бензина)
        return {'name': 'Fuel Rail Pressure (diesel, or gasoline direct inject)', 'value': data[0], 'unit': 'kPa'}
    elif pid == 0x62:  # Давление масла
        return {'name': 'Oil Pressure', 'value': data[0], 'unit': 'kPa'}
    elif pid == 0x63:  # Крутящий момент на валу
        return {'name': 'Engine Torque', 'value': (data[0] * 256 + data[1]) / 4, 'unit': 'Nm'}
    elif pid == 0x64:  # Режимы переключения передач
        return {'name': 'Gear Shifting Mode', 'value': data[0], 'unit': 'mode'}
    elif pid == 0x65:  # Давление в тормозной магистрали
        return {'name': 'Brake Pressure', 'value': data[0], 'unit': 'kPa'}
    elif pid == 0x66:  # Состояние тормозных колодок
        return {'name': 'Brake Pad Wear', 'value': data[0], 'unit': 'wear %'}
    elif pid == 0x67:  # Датчики положения амортизаторов
        return {'name': 'Suspension Level', 'value': data[0], 'unit': 'mm'}
    elif pid == 0x68:  # Угол поворота руля
        return {'name': 'Steering Angle', 'value': (data[0] * 256 + data[1]) / 10 - 500, 'unit': '°'}
    elif pid == 0x69:  # Датчики ускорения кузова
        return {'name': 'Body Acceleration', 'value': (data[0] * 256 + data[1]) / 100 - 100, 'unit': 'm/s²'}
    elif pid == 0x6A:  # Ошибки датчиков удара
        return {'name': 'Crash Sensor Errors', 'value': data[0], 'unit': 'error code'}
    elif pid == 0x6B:  # Состояние пиропатронов подушек
        return {'name': 'Airbag Initiators', 'value': data[0], 'unit': 'status'}
    elif pid == 0x6C:  # Давление хладагента
        return {'name': 'Refrigerant Pressure', 'value': data[0], 'unit': 'kPa'}
    elif pid == 0x6D:  # Работа вентиляторов
        return {'name': 'Fan Speed', 'value': data[0], 'unit': 'RPM'}
    elif pid == 0x6E:  # Состояние датчиков дверей/окон
        return {'name': 'Door/Window Sensors', 'value': data[0], 'unit': 'status'}
    elif pid == 0x6F:  # Освещение (стоп-сигналы, фары)
        return {'name': 'Lighting Status', 'value': data[0], 'unit': 'status'}
    elif pid == 0x70:  # Ошибки впрыска (U-коды)
        return {'name': 'Injection Errors', 'value': data[0], 'unit': 'error code'}
    elif pid == 0x71:  # Тормозная система (ABS/ESP)
        return {'name': 'ABS/ESP Status', 'value': data[0], 'unit': 'status'}
    elif pid == 0x72:  # Датчики скорости колес
        return {'name': 'Wheel Speed', 'value': data[0], 'unit': 'km/h'}
    elif pid == 0x73:  # Давление в топливной рампе
        return {'name': 'Fuel Rail Pressure (diesel, or gasoline direct inject)', 'value': data[0], 'unit': 'kPa'}
    else:
        return None  # Неизвестный PID

# Словарь с ожидаемыми PID (протокол OBD-II)
# Это список PID, которые могут быть запрашиваемы через OBD-II, с их значениями и описаниями.

expected_pids = {
    # 0x00: "Supported PIDs (01-20)"  # Данный PID обычно указывает на поддерживаемые PIDs с 01 по 20.
    # 0x01: "Monitor Status"  # Статус монитора — используется для получения статуса самодиагностики автомобиля.
    #0x02: "Freeze DTC",  # Данный PID используется для получения ошибок, записанных в память компьютера автомобиля (DTC - Diagnostic Trouble Codes).
    #0x03: "Fuel System Status",  # Статус топливной системы — отображает информацию о работе топливной системы.
    #0x04: "Engine Load",  # Нагрузочное состояние двигателя — показывает текущую нагрузку на двигатель.
    0x05: "Engine Coolant Temperature",  # Температура охлаждающей жидкости двигателя.
    #0x06: "Short Term Fuel Trim (Bank 1)",  # Краткосрочная коррекция топлива для банка 1.
    #0x07: "Long Term Fuel Trim (Bank 1)",  # Долгосрочная коррекция топлива для банка 1.
    #0x08: "Short Term Fuel Trim (Bank 2)",  # Краткосрочная коррекция топлива для банка 2.
    #0x09: "Long Term Fuel Trim (Bank 2)",  # Долгосрочная коррекция топлива для банка 2.
    #0x0A: "Fuel Pressure",  # Давление топлива — информация о давлении в топливной системе.
    #0x0B: "Intake Manifold Pressure",  # Давление в коллекторе впуска — показания давления воздуха в впускном коллекторе.
    0x0C: "Engine RPM",  # Обороты двигателя — показывает текущие обороты двигателя.
    0x0D: "Vehicle Speed",  # Скорость автомобиля — отображает текущую скорость автомобиля.
    #0x0E: "Timing Advance",  # Опережение зажигания — информация о степени опережения зажигания в двигателе.
    #0x0F: "Intake Air Temperature",  # Температура воздуха на входе — температура воздуха, поступающего в двигатель.
    #0x10: "Air Flow Rate",  # Расход воздуха — количество воздуха, поступающего в двигатель.
    #0x11: "Throttle Position",  # Положение дроссельной заслонки — положение дроссельной заслонки двигателя.
    #0x12: "Commanded Throttle Actuator",  # Командованное положение дроссельного актуатора — информация о положении дроссельного актуатора.
    #0x13: "Air Injection System Status",  # Статус системы подачи воздуха — показывает, работает ли система подачи воздуха в выхлопную систему.
    #0x14: "O2 Sensors Present (Bank 1)",  # Наличие датчиков O2 в банке 1.
    #0x15: "O2 Sensor Voltage (Bank 1)",  # Напряжение датчиков O2 в банке 1.
    #0x16: "O2 Sensor Voltage (Bank 2)",  # Напряжение датчиков O2 в банке 2.
    #0x17: "EVAP System Status",  # Статус системы испарений (EVAP) — информация о работе системы по контролю испарений топлива.
    #0x18: "EVAP Pressure",  # Давление в системе испарений.
    #0x19: "Catalyst Temperature (Bank 1)",  # Температура катализатора в банке 1.
    #0x1A: "Catalyst Temperature (Bank 2)",  # Температура катализатора в банке 2.
    #0x1B: "Secondary Air System Status",  # Статус системы вторичного воздуха — показывает, работает ли система вторичного воздуха.
    #0x1C: "OBD Standards Compliance",  # Совместимость с OBD-стандартами.
    #0x1D: "O2 Sensors Present (Bank 1)",  # Наличие датчиков O2 в банке 1 (повторно, возможно ошибка).
    #0x1E: "O2 Sensors Present (Bank 2)",  # Наличие датчиков O2 в банке 2.
    #0x1F: "Engine Run Time",  # Время работы двигателя с момента последнего сброса.
    #0x20: "Supported PIDs (21-40)",  # Поддерживаемые PIDs с 21 по 40.
    #0x21: "Distance Traveled with MIL On",  # Расстояние, пройденное при включенной индикаторной лампе MIL.
    #0x22: "Fuel Rail Pressure (relative)",  # Давление в топливной рампе (относительное).
    #0x23: "Fuel Rail Pressure (diesel, or gasoline direct inject)",  # Давление в топливной рампе (для дизеля или для прямого впрыска).
    #0x24: "O2 Sensor Voltage (Bank 1)",  # Напряжение датчиков O2 в банке 1 (повторное использование).
    #0x25: "O2 Sensor Voltage (Bank 2)",  # Напряжение датчиков O2 в банке 2.
    #0x26: "O2 Sensor Voltage (Bank 3)",  # Напряжение датчиков O2 в банке 3.
    #0x27: "O2 Sensor Voltage (Bank 4)",  # Напряжение датчиков O2 в банке 4.
    #0x28: "EVAP System Status",  # Статус системы испарений (EVAP) (повтор).
    #0x29: "EVAP Pressure",  # Давление в системе испарений (повтор).
    #0x2A: "Catalyst Temperature (Bank 1)",  # Температура катализатора в банке 1 (повтор).
    #0x2B: "Catalyst Temperature (Bank 2)",  # Температура катализатора в банке 2 (повтор).
    #0x2C: "Secondary Air System Status",  # Статус системы вторичного воздуха (повтор).
    #0x2D: "OBD Standards Compliance",  # Совместимость с OBD-стандартами (повтор).
    #0x2E: "O2 Sensors Present (Bank 1)",  # Наличие датчиков O2 в банке 1 (повтор).
    #0x2F: "Fuel Level",  # Уровень топлива.
    #0x30: "Distance Since DTC Cleared",  # Расстояние с момента сброса DTC.
    #0x31: "EVAP Pressure",  # Давление в системе испарений (повтор).
    #0x32: "EVAP Pressure",  # Давление в системе испарений (повтор).
    #0x33: "Absolute Manifold Pressure",  # Абсолютное давление во впускном коллекторе.
    #0x34: "O2 Sensor Voltage (Bank 1)",  # Напряжение датчиков O2 в банке 1.
    #0x35: "O2 Sensor Voltage (Bank 2)",  # Напряжение датчиков O2 в банке 2.
    #0x36: "O2 Sensor Voltage (Bank 3)",  # Напряжение датчиков O2 в банке 3.
    #0x37: "O2 Sensor Voltage (Bank 4)",  # Напряжение датчиков O2 в банке 4.
    #0x38: "EVAP System Status",  # Статус системы испарений (EVAP).
    #0x39: "EVAP Pressure",  # Давление в системе испарений.
    #0x3A: "Catalyst Temperature (Bank 1)",  # Температура катализатора в банке 1.
    #0x3B: "Catalyst Temperature (Bank 2)",  # Температура катализатора в банке 2.
    #0x3C: "Secondary Air System Status",  # Статус системы вторичного воздуха.
    #0x3D: "OBD Standards Compliance",  # Совместимость с OBD-стандартами.
    #0x3E: "O2 Sensors Present (Bank 1)",  # Наличие датчиков O2 в банке 1.
    #0x3F: "Engine Run Time",  # Время работы двигателя.
    #0x40: "Supported PIDs (41-60)"  # Поддерживаемые PIDs с 41 по 60.
}


async def can_sniffer():
    global bus
    last_data_time = time.time()
    last_nats_send_time = time.time()
    last_request_time = time.time()
    error_counter = 0
    max_errors_before_reset = 10
    request_interval = 0.2  # Интервал между запросами разных PID

    while True:
        if bus is None:
            if not connect_to_can():
                await asyncio.sleep(can_retry_interval)
                continue

        try:
            # Проверка состояния интерфейса
            if not is_can_interface_up(can_interface):
                logger.error(f"CAN interface {can_interface} is DOWN! Attempting to bring up...")
                bring_can_interface_up(can_interface)
                await asyncio.sleep(1)
                continue

            # Создаем структуру данных с timestamp в начале
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            collected_data = {
                "timestamp": timestamp,
                "can_error": None
            }

            # Инициализация ожидаемых PID
            for pid in expected_pids:
                param_key = expected_pids[pid].lower().replace(" ", "_")
                collected_data[param_key] = None

            # Отправка запросов для каждого PID по очереди
            current_time = time.time()
            if current_time - last_request_time >= request_interval:
                for pid in expected_pids:
                    send_obd_request(pid)
                    await asyncio.sleep(0.05)  # Небольшая задержка между запросами
                last_request_time = current_time

            # Сбор данных
            start_time = time.time()
            while time.time() - start_time < polling_period:
                message = bus.recv(timeout=0.1)
                if message is not None:
                    last_data_time = time.time()
                    
                    obd_response = decode_obd_response(message)
                    if obd_response:
                        pid = obd_response['pid']
                        if pid in expected_pids:
                            decoded_value = decode_pid(pid, obd_response['data'])
                            if decoded_value:
                                param_key = expected_pids[pid].lower().replace(" ", "_")
                                collected_data[param_key] = {
                                    "value": decoded_value['value'],
                                    "unit": decoded_value['unit'],
                                   # "raw": message.data.hex(),
                                   # "timestamp": obd_response['timestamp']
                                }
                else:
                    if time.time() - last_data_time > polling_period * 2:
                        collected_data["can_error"] = "No data from CAN bus"
                        logger.error("No data from CAN bus")
                        break

            if collected_data:
                log_can_data(collected_data)
                
                current_time = time.time()
                if current_time - last_nats_send_time >= nats_send_interval:
                    if await send_to_nats(collected_data):
                        last_nats_send_time = current_time
                        await flush_buffer()

        except can.CanError as e:
            error_counter += 1
            logger.error(f"CAN Error [{error_counter}/{max_errors_before_reset}]: {str(e)}")
            
            if error_counter >= max_errors_before_reset:
                logger.critical("Max CAN errors reached. Resetting connection...")
                if bus:
                    bus.shutdown()
                bus = None
                error_counter = 0
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            if bus:
                bus.shutdown()
            bus = None
            
        await asyncio.sleep(polling_period)

async def main():
    initialize_log_files()
    await can_sniffer()

if __name__ == "__main__":
    asyncio.run(main())