import asyncio
import serial
import json
import time
import logging
from datetime import datetime
from configparser import ConfigParser
import os
import smbus2
import subprocess
import pytz
import nats
import signal
import sys
import socket
from datetime import datetime

# Инициализация конфигурации
config = ConfigParser()
config.read('/app/config/settings.ini')

# Настройки подключения
connection_type = config.get('gnss', 'connection_type', fallback='serial')

# Serial настройки
serial_port = config.get('gnss', 'serial_port', fallback='/dev/ttyAMA0')
baudrate = config.getint('gnss', 'baudrate', fallback=115200)
timeout = config.getfloat('gnss', 'timeout', fallback=1.0)

# I2C настройки (только decimal)
i2c_bus = config.getint('gnss', 'i2c_bus', fallback=1)
i2c_address = config.getint('gnss', 'i2c_address', fallback=67)  # 0x43 в decimal

# NATS настройки
nats_server = config.get('nats', 'server')
nats_topic = config.get('nats', 'topic')
buffer_size = config.getint('nats', 'buffer_size')
send_interval = config.getint('nats', 'send_interval')
poll_interval = 1  # Интервал опроса GNSS в секундах

# Проверка допустимости адреса
if not (8 <= i2c_address <= 119):  # Диапазон 0x08-0x77
    i2c_address = 67
    logging.warning(f"Invalid I2C address, using default: {i2c_address}")

# Логирование
data_log_path = config.get('logs', 'data_log')
error_log_path = config.get('logs', 'error_log')

# Настройки синхронизации времени
sync_time_enabled = config.getboolean('time_sync', 'enabled', fallback=True)
time_sync_interval = config.getint('time_sync', 'interval_minutes', fallback=1)
last_time_sync = 0  # Время последней синхронизации

# Буфер для хранения данных перед отправкой
data_buffer = []
last_send_time = time.time()

# Настройка логирования
logging.basicConfig(
    filename=error_log_path,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class I2C_GNSS_Reader:
    """Класс для чтения NMEA предложений по I2C"""
    def __init__(self, bus=1, address=67):
        self.bus_num = bus
        self.address = address
        self.buffer = bytearray()
        self.timeout = timeout
        self.max_retries = 3
        logger.info(f"Initializing I2C on bus {bus}, address {address} (0x{address:02x})")
        self._init_bus()
        
    def _init_bus(self):
        """Инициализация I2C шины с повторными попытками"""
        for attempt in range(self.max_retries):
            try:
                self.bus = smbus2.SMBus(self.bus_num)
                # Проверка доступности устройства
                self.bus.read_byte(self.address)
                logger.info("I2C initialized successfully")
                return
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"I2C init failed: {e}")
                    raise
                logger.warning(f"Retry {attempt + 1}/{self.max_retries}...")
                time.sleep(1)

    def read_sentence(self):
        """Чтение NMEA предложения по I2C"""
        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                self.buffer = bytearray()
                
                while time.time() - start_time < self.timeout:
                    try:
                        byte = self.bus.read_byte(self.address)
                    except OSError as e:
                        if e.errno == 121:  # Remote I/O error
                            raise
                        continue
                    
                    if byte == 0x0A:  # LF
                        if len(self.buffer) > 0:
                            try:
                                sentence = self.buffer.decode('ascii', errors='ignore').strip()
                                if sentence.startswith('$'):
                                    return sentence
                            except UnicodeDecodeError:
                                logger.warning("I2C decode error")
                        self.buffer = bytearray()
                    elif byte != 0x0D:  # Ignore CR
                        self.buffer.append(byte)
            except Exception as e:
                logger.warning(f"I2C read error (attempt {attempt + 1}): {e}")
                if attempt == self.max_retries - 1:
                    logger.error("Max retries reached, reinitializing I2C...")
                    self._init_bus()
                    return None
                time.sleep(0.5)
        return None

# Инициализация подключения
if connection_type == 'i2c':
    try:
        gnss_reader = I2C_GNSS_Reader(i2c_bus, i2c_address)
    except Exception as e:
        logger.critical(f"I2C initialization failed: {e}")
        raise
else:
    try:
        ser = serial.Serial(
            port=serial_port,
            baudrate=baudrate,
            timeout=timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=False,
            dsrdtr=False
        )
        # Очистка буферов
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        logger.info(f"Serial port {serial_port} initialized successfully")
    except Exception as e:
        logger.critical(f"Serial port error: {e}")
        raise

def is_valid_nmea_message(message):
    """Проверка, является ли сообщение валидным NMEA предложением"""
    return message and message.startswith('$')

def convert_nmea_time_to_hms(nmea_time):
    """Конвертация времени из формата NMEA (HHMMSS) в HH:MM:SS"""
    if not nmea_time or len(nmea_time) < 6:
        return None
    return f"{nmea_time[:2]}:{nmea_time[2:4]}:{nmea_time[4:6]}"

def convert_nmea_date_to_yyyy_mm_dd(nmea_date):
    """Конвертация даты из формата NMEA (DDMMYY) в YYYY-MM-DD"""
    if not nmea_date or len(nmea_date) < 6:
        return None
    return f"20{nmea_date[4:6]}-{nmea_date[2:4]}-{nmea_date[:2]}"

def safe_float(s):
    """Безопасное преобразование строки в float"""
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None

def safe_int(s):
    """Безопасное преобразование строки в int"""
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None

def sync_system_time(gnss_date, gnss_time):
    """
    Синхронизация системного времени по данным GNSS (UTC)
    и установка Московского времени (UTC+3)
    """
    try:
        if not gnss_date or not gnss_time:
            return False
            
        date_str = f"20{gnss_date[4:6]}-{gnss_date[2:4]}-{gnss_date[:2]}"
        time_str = f"{gnss_time[:2]}:{gnss_time[2:4]}:{gnss_time[4:6]}"
        
        cmd = f"date -u -s '{date_str} {time_str}'"
        subprocess.call(cmd, shell=True)
        
        logger.info(f"System time synced to UTC: {date_str} {time_str}")
        
        moscow_tz = pytz.timezone('Europe/Moscow')
        moscow_time = datetime.now(pytz.utc).astimezone(moscow_tz)
        moscow_time_str = moscow_time.strftime("%Y-%m-%d %H:%M:%S")
        
        logger.info(f"Moscow time (UTC+3): {moscow_time_str}")
        return True
        
    except Exception as e:
        logger.error(f"Time sync failed: {e}")
        return False

def parse_nmea_message(nmea_message):
    """Разбор NMEA сообщения и извлечение полезных данных"""
    decoded = {}
    parts = nmea_message.split(',')

    if nmea_message.startswith('$GNRMC'):
        decoded['type'] = 'RMC'
        decoded['time'] = convert_nmea_time_to_hms(parts[1])
        decoded['status'] = parts[2]
        decoded['latitude'] = safe_float(parts[3][:2]) + safe_float(parts[3][2:])/60 if parts[3] else 0.0
        decoded['longitude'] = safe_float(parts[5][:3]) + safe_float(parts[5][3:])/60 if parts[5] else 0.0
        decoded['speed'] = safe_float(parts[7])
        decoded['course'] = safe_float(parts[8])
        decoded['date'] = convert_nmea_date_to_yyyy_mm_dd(parts[9])
        decoded['magnetic_variation'] = safe_float(parts[10])
        
        global last_time_sync
        if (sync_time_enabled and parts[1] and parts[9] and 
            time.time() - last_time_sync > time_sync_interval * 60):
            if sync_system_time(parts[9], parts[1]):
                last_time_sync = time.time()
                
    elif nmea_message.startswith('$GNGGA'):
        decoded['type'] = 'GGA'
        decoded['time'] = convert_nmea_time_to_hms(parts[1])
        decoded['latitude'] = safe_float(parts[2][:2]) + safe_float(parts[2][2:])/60 if parts[2] else 0.0
        decoded['longitude'] = safe_float(parts[4][:3]) + safe_float(parts[4][3:])/60 if parts[4] else 0.0
        decoded['fix_quality'] = safe_int(parts[6])
        decoded['satellites'] = safe_int(parts[7])
        decoded['hdop'] = safe_float(parts[8])
        decoded['altitude'] = safe_float(parts[9])
        decoded['geoid_height'] = safe_float(parts[11])
    elif nmea_message.startswith('$GNVTG'):
        decoded['type'] = 'VTG'
        decoded['true_course'] = safe_float(parts[1])
        decoded['magnetic_course'] = safe_float(parts[3])
        decoded['speed_knots'] = safe_float(parts[5])
        decoded['speed_kmh'] = safe_float(parts[7])
    elif nmea_message.startswith('$GNZDA'):
        decoded['type'] = 'ZDA'
        decoded['time'] = convert_nmea_time_to_hms(parts[1])
        decoded['day'] = safe_int(parts[2])
        decoded['month'] = safe_int(parts[3])
        decoded['year'] = safe_int(parts[4])
        decoded['utc_offset_hour'] = safe_int(parts[5])
        decoded['utc_offset_minute'] = safe_int(parts[6])
    elif nmea_message.startswith('$GNGSA'):
        decoded['type'] = 'GSA'
        decoded['mode'] = parts[1]
        decoded['fix_type'] = safe_int(parts[2])
        decoded['satellites_used'] = [safe_int(s) for s in parts[3:15]]
        decoded['pdop'] = safe_float(parts[15])
        decoded['hdop'] = safe_float(parts[16])
        decoded['vdop'] = safe_float(parts[17])
    elif nmea_message.startswith('$GPGSV'):
        decoded['type'] = 'GSV'
        decoded['total_messages'] = safe_int(parts[1])
        decoded['message_number'] = safe_int(parts[2])
        decoded['satellites_in_view'] = safe_int(parts[3])
        decoded['satellites'] = []
        for i in range(4, len(parts)-1, 4):
            satellite_info = parts[i:i+4]
            decoded['satellites'].append({
                'satellite_id': safe_int(satellite_info[0]),
                'elevation': safe_int(satellite_info[1]),
                'azimuth': safe_int(satellite_info[2]),
                'snr': safe_int(satellite_info[3])
            })

    return decoded

last_nats_error_time = None
last_nats_error_message = None

async def send_to_nats(data):
    """Отправка данных на NATS сервер"""
    global last_nats_error_time, last_nats_error_message
    
    try:
        # Проверка доступности сервера перед подключением
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)  # Таймаут 2 секунды
                server, port = nats_server.split('://')[1].split(':')
                s.connect((server, int(port)))
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            error_msg = f"NATS server {nats_server} is unreachable: {str(e)}"
            # Логируем только если ошибка изменилась или прошло более 5 минут
            if error_msg != last_nats_error_message or (
                last_nats_error_time is None or 
                (datetime.now() - last_nats_error_time).total_seconds() > 300
            ):
                logger.error(error_msg)
                last_nats_error_message = error_msg
                last_nats_error_time = datetime.now()
            return False

        # Если сервер доступен, пытаемся отправить данные
        try:
            nc = await nats.connect(
                nats_server,
                connect_timeout=5,  # Таймаут подключения 5 секунд
                max_reconnect_attempts=1,  # Не пытаться переподключаться
                reconnect_time_wait=1
            )
            await nc.publish(nats_topic, json.dumps(data).encode())
            await nc.close()
            
            # Сбрасываем счетчик ошибок при успешной отправке
            if last_nats_error_message is not None:
                logger.info(f"NATS connection restored, successfully sent data")
                last_nats_error_message = None
                last_nats_error_time = None
                
            return True
            
        except Exception as e:
            error_msg = f"NATS publish error: {str(e)}"
            if error_msg != last_nats_error_message or (
                last_nats_error_time is None or 
                (datetime.now() - last_nats_error_time).total_seconds() > 300
            ):
                logger.error(error_msg)
                last_nats_error_message = error_msg
                last_nats_error_time = datetime.now()
            return False
            
    except Exception as e:
        error_msg = f"Unexpected NATS error: {str(e)}"
        if error_msg != last_nats_error_message or (
            last_nats_error_time is None or 
            (datetime.now() - last_nats_error_time).total_seconds() > 300
        ):
            logger.error(error_msg)
            last_nats_error_message = error_msg
            last_nats_error_time = datetime.now()
        return False

def write_to_log(data):
    """Запись данных в лог-файл в формате JSON"""
    try:
        os.makedirs(os.path.dirname(data_log_path), exist_ok=True)
        
        try:
            with open(data_log_path, 'r') as f:
                existing_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = []
        
        log_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "decoded": data['data']
        }
        existing_data.append(log_entry)
        
        with open(data_log_path, 'w') as f:
            json.dump(existing_data, f, indent=4)
            
    except Exception as e:
        logger.error(f"Error writing to log: {e}")

async def process_gnss_data():
    """Обработка данных GNSS и отправка в NATS"""
    global data_buffer, last_send_time
    
    try:
        if connection_type == 'i2c':
            line = gnss_reader.read_sentence()
        else:
            line = ser.readline().decode('ascii', errors='ignore').strip()
        
        if line and is_valid_nmea_message(line):
            decoded = parse_nmea_message(line)
            if decoded:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                data = {
                    'timestamp': timestamp,
                    'connection_type': connection_type,
                    'data': decoded
                }
                
                write_to_log(data)
                
                # Добавляем данные в буфер
                if len(data_buffer) < buffer_size:
                    data_buffer.append(data)
                else:
                    logger.warning("NATS buffer full, discarding oldest message")
                    data_buffer.pop(0)
                    data_buffer.append(data)
                
                # Отправка данных по интервалу
                current_time = time.time()
                if current_time - last_send_time >= send_interval and data_buffer:
                    success = True
                    for item in data_buffer:
                        if not await send_to_nats(item):
                            success = False
                            break
                    
                    if success:
                        data_buffer = []
                        last_send_time = current_time
                        
    except Exception as e:
        logger.error(f"GNSS processing error: {e}")

def signal_handler(sig, frame):
    """Обработчик сигналов прерывания"""
    print("\nЗавершение работы...")
    sys.exit(0)

async def main():
    """Основная функция приложения"""
    # Создание директорий для логов
    os.makedirs(os.path.dirname(data_log_path), exist_ok=True)
    os.makedirs(os.path.dirname(error_log_path), exist_ok=True)

    signal.signal(signal.SIGINT, signal_handler)

    while True:
        try:
            # Обработка данных GNSS
            await process_gnss_data()
            
            await asyncio.sleep(poll_interval)
            
        except serial.SerialException as e:
            logger.error(f"Serial port error: {e}")
            if connection_type == 'serial':
                try:
                    ser.close()
                    ser.open()
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception as e:
                    logger.error(f"Serial port reopen failed: {e}")
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            await asyncio.sleep(1)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
    finally:
        if connection_type == 'serial' and 'ser' in locals() and ser.is_open:
            ser.close()
        logger.info("Application shutdown")