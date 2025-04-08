#!/usr/bin/python
# -*- coding:utf-8 -*-
import serial
import time
import logging
import configparser
import json
import os
import asyncio
import nats
import socket
from datetime import datetime

# Чтение настроек из конфигурационного файла
config = configparser.ConfigParser()
config.read("/app/config/settings.ini")

# Настройки модема
MODEM_PORT = config.get('modem', 'port')
BAUD_RATE = config.getint('modem', 'baud_rate')

# Настройки NATS
NATS_SERVER = config.get('nats', 'server')
NATS_TOPIC = config.get('nats', 'topic')
BUFFER_SIZE = config.getint('nats', 'buffer_size', fallback=100)
NATS_SEND_INTERVAL = config.getint('nats', 'send_interval', fallback=5)
SERVER_CHECK_TIMEOUT = config.getint('nats', 'server_check_timeout', fallback=2)
CONNECT_TIMEOUT = config.getint('nats', 'connect_timeout', fallback=5)
ERROR_LOG_INTERVAL = config.getint('nats', 'error_log_interval', fallback=300)

# Настройки логирования
DATA_LOG_PATH = config.get('logs', 'data_log')
ERROR_LOG_PATH = config.get('logs', 'error_log')

# Интервалы
GPS_INTERVAL = config.getint('intervals', 'gps_interval', fallback=2)
CELL_INTERVAL = config.getint('intervals', 'cell_interval', fallback=3)

# Глобальные переменные для логирования ошибок NATS
last_nats_error_time = None
last_nats_error_message = None

# Создание директории для логов
os.makedirs(os.path.dirname(DATA_LOG_PATH), exist_ok=True)
os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)

# Инициализация логгера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(ERROR_LOG_PATH),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Буфер данных
data_buffer = []

# Функция для преобразования HEX в DEC
def hex_to_dec(hex_str):
    try:
        return int(hex_str, 16)
    except ValueError:
        logger.error(f"Ошибка при преобразовании HEX в DEC: {hex_str}")
        return None

# Функция для очистки CID от лишних символов
def clean_cid(cid):
    if cid:
        return cid.split('\r\n\r\nOK')[0].strip()
    return None

# Функция для преобразования RSSI в dBm
def rssi_to_dbm(rssi):
    try:
        rssi = int(rssi)
        if rssi == 0:
            return None
        elif rssi == 99:
            return None
        else:
            return -113 + (2 * rssi)
    except Exception as e:
        logger.error(f"Ошибка при преобразовании RSSI в dBm: {str(e)}")
        return None

# Функция для конвертации данных из формата +CGPSINFO
def parse_gps_info(response):
    try:
        if isinstance(response, str) and '+CGPSINFO:' in response:
            gps_data = response.split('+CGPSINFO: ')[1].split('\r\n')[0]
            parts = [part.strip() for part in gps_data.split(',')]

            lat = float(parts[0]) if parts[0] else None
            lat_dir = parts[1]
            lon = float(parts[2]) if parts[2] else None
            lon_dir = parts[3]
            date_utc = parts[4] if len(parts) > 4 and parts[4] else None
            time_utc = parts[5] if len(parts) > 5 and parts[5] else None
            altitude = float(parts[6]) if len(parts) > 6 and parts[6] else None
            speed_knots = float(parts[7]) if len(parts) > 7 and parts[7] else None
            course = float(parts[8]) if len(parts) > 8 and parts[8] else None

            # Преобразование координат
            if lat and lon:
                lat = lat // 100 + (lat % 100) / 60
                lon = lon // 100 + (lon % 100) / 60
                if lat_dir == 'S':
                    lat = -lat
                if lon_dir == 'W':
                    lon = -lon

            # Преобразование скорости из узлов в км/ч
            speed_kmh = speed_knots * 1.852 if speed_knots else None

            # Форматирование даты и времени
            if date_utc and time_utc and len(date_utc) == 6:
                # Дата в формате DDMMYY -> YYYY-MM-DD
                day = date_utc[:2]
                month = date_utc[2:4]
                year = "20" + date_utc[4:6]
                
                # Время в формате HHMMSS.S -> HH:MM:SS
                time_str = f"{time_utc[:2]}:{time_utc[2:4]}:{time_utc[4:6]}"
                date_time = f"{year}-{month}-{day} {time_str}"
            else:
                date_time = None

            return {
                "latitude": lat,
                "longitude": lon,
                "date_time": date_time,
                "altitude": altitude,
                "speed_kmh": speed_kmh,
                "course": course
            }
        else:
            logger.error("Ошибка при извлечении данных GPS: Ответ не содержит данных GPS")
            return None
    except Exception as e:
        logger.error(f"Ошибка при извлечении данных GPS: {str(e)}")
        return None

def get_cell_info(ser):
    try:
        cpsi_response = send_at('AT+CPSI?', '+CPSI:', 1, ser)
        if cpsi_response:
            cpsi_data = cpsi_response.split(':')[1].strip().split(',')
            return {
                "system_mode": cpsi_data[0],
                "operation_mode": cpsi_data[1],
                "operator_info": cpsi_data[2],
                "lac": cpsi_data[3],
                "cid": cpsi_data[4],
                "bsic": cpsi_data[5],
                "band": cpsi_data[6],
                "arfcn": cpsi_data[7],
                "dl_freq": cpsi_data[8],
                "ul_freq": cpsi_data[9],
                "rssi": cpsi_data[10],
                "rsrp": cpsi_data[11],
                "rsrq": cpsi_data[12],
                "sinr": cpsi_data[13]
            }
        else:
            logger.error("Не удалось получить данные от команды AT+CPSI?")
            return None
    except Exception as e:
        logger.error(f"Ошибка при получении данных о сотовых вышках: {str(e)}")
        return None

def send_at(command, back, timeout, ser):
    rec_buff = ''
    ser.write((command + '\r\n').encode())
    time.sleep(timeout)
    if ser.inWaiting():
        time.sleep(0.01)
        rec_buff = ser.read(ser.inWaiting())
    if rec_buff != '':
        decoded_response = rec_buff.decode().strip()
        if back not in decoded_response:
            logger.error(command + ' ERROR')
            logger.error(command + ' back:\t' + decoded_response)
            return 0
        else:
            cleaned_response = decoded_response.split('\r\n\r\nOK')[0].strip()
            logger.debug("Ответ от модема: " + cleaned_response)
            return cleaned_response
    else:
        logger.error('GPS не готов')
        return 0

def enable_gps(ser):
    logger.info("Отключаем GPS перед включением...")
    send_at('AT+CGPS=0', 'OK', 1, ser)
    logger.info("GPS отключен. Включаем GPS...")
    time.sleep(2)
    send_at('AT+CGPS=1', 'OK', 1, ser)
    logger.info("GPS включен.")

async def check_nats_server_available():
    """Проверка доступности NATS сервера"""
    try:
        server, port = NATS_SERVER.split('://')[1].split(':')
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(SERVER_CHECK_TIMEOUT)
            s.connect((server, int(port)))
        return True
    except Exception as e:
        return False

async def send_to_nats(data):
    """Улучшенная функция отправки данных на NATS сервер"""
    global last_nats_error_time, last_nats_error_message
    
    # Проверка доступности сервера
    if not await check_nats_server_available():
        error_msg = f"NATS server {NATS_SERVER} is unreachable"
        if error_msg != last_nats_error_message or (
            last_nats_error_time is None or 
            (datetime.now() - last_nats_error_time).total_seconds() > ERROR_LOG_INTERVAL
        ):
            logger.error(error_msg)
            last_nats_error_message = error_msg
            last_nats_error_time = datetime.now()
        return False

    # Если сервер доступен, пытаемся отправить данные
    try:
        nc = await nats.connect(
            NATS_SERVER,
            connect_timeout=CONNECT_TIMEOUT,
            max_reconnect_attempts=1,
            reconnect_time_wait=1
        )
        await nc.publish(NATS_TOPIC, json.dumps(data).encode())
        await nc.close()
        
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
            (datetime.now() - last_nats_error_time).total_seconds() > ERROR_LOG_INTERVAL
        ):
            logger.error(error_msg)
            last_nats_error_message = error_msg
            last_nats_error_time = datetime.now()
        return False

def write_to_data_log(data):
    """Запись данных в лог-файл"""
    try:
        # Проверяем существование файла и загружаем данные
        try:
            with open(DATA_LOG_PATH, 'r') as f:
                existing_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = []
        
        # Добавляем новые данные
        existing_data.append(data)
        
        # Записываем обратно
        with open(DATA_LOG_PATH, 'w') as f:
            json.dump(existing_data, f, indent=4)
            
    except Exception as e:
        logger.error(f"Error writing to data log: {e}")

def is_data_valid(data):
    if data.get("cell_info") is not None:
        return True
    if data.get("gps_data") is not None and any(data["gps_data"].values()):
        return True
    return False

async def process_data(data):
    """Обработка и отправка данных"""
    global data_buffer
    
    if not is_data_valid(data):
        logger.warning("Данные не содержат полезной информации и не будут сохранены.")
        return
    
    # Сохраняем данные в лог
    write_to_data_log(data)
    
    # Добавляем в буфер
    if len(data_buffer) < BUFFER_SIZE:
        data_buffer.append(data)
    else:
        logger.warning("NATS buffer full, discarding oldest message")
        data_buffer.pop(0)
        data_buffer.append(data)

async def send_buffered_data():
    """Отправка данных из буфера"""
    global data_buffer
    
    if data_buffer:
        success = True
        for item in data_buffer:
            if not await send_to_nats(item):
                success = False
                break
        
        if success:
            data_buffer.clear()
            return True
    return False

async def main_loop(ser):
    """Основной цикл обработки данных"""
    last_cell_time = time.time() - CELL_INTERVAL
    last_nats_time = time.time() - NATS_SEND_INTERVAL

    while True:
        try:
            # Запрос данных GPS
            response = send_at('AT+CGPSINFO', '+CGPSINFO:', 1, ser)

            if response:
                gps_data = parse_gps_info(response)
                cell_info = None

                # Запрос данных о сотовых вышках, если прошел интервал
                if time.time() - last_cell_time >= CELL_INTERVAL:
                    cell_info = get_cell_info(ser)
                    last_cell_time = time.time()

                data = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cell_info": cell_info,
                    "gps_data": gps_data
                }

                await process_data(data)

                # Отправка данных по интервалу
                if time.time() - last_nats_time >= NATS_SEND_INTERVAL:
                    if await send_buffered_data():
                        last_nats_time = time.time()

            await asyncio.sleep(GPS_INTERVAL)

        except Exception as e:
            logger.error(f"Ошибка в процессе получения данных GPS: {str(e)}")
            await asyncio.sleep(1)

def main():
    ser = None
    while True:
        try:
            ser = serial.Serial(MODEM_PORT, BAUD_RATE, timeout=1)
            logger.info("Модем подключен")
            enable_gps(ser)
            asyncio.run(main_loop(ser))
            break
        except serial.SerialException as e:
            logger.error(f"Ошибка последовательного порта: {str(e)}")
            time.sleep(10)
        except Exception as e:
            logger.error(f"Неожиданная ошибка: {str(e)}")
            time.sleep(1)
        finally:
            if ser:
                ser.close()

if __name__ == "__main__":
    main()