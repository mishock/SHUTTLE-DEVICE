import os
import time
import math
import json
import smbus
import lgpio as sbc
import configparser
import logging
from logging.handlers import RotatingFileHandler
import nats
from datetime import datetime
import socket
import asyncio

# Инициализация конфигурации
config = configparser.ConfigParser()
config.read("/app/config/settings.ini")

# Пути к лог-файлам
DATA_LOG_PATH = config['LOGS']['data_log_path']
ERROR_LOG_PATH = config['LOGS']['error_log_path']

# Настройки NATS
NATS_SERVER = config['NATS']['nats_server']
NATS_TOPIC = config['NATS']['nats_topic']
BUFFER_SIZE = int(config['NATS']['buffer_size'])
NATS_SEND_INTERVAL = int(config['NATS']['nats_send_interval'])
SERVER_CHECK_TIMEOUT = int(config['NATS'].get('server_check_timeout', '2'))
CONNECT_TIMEOUT = int(config['NATS'].get('connect_timeout', '5'))
ERROR_LOG_INTERVAL = int(config['NATS'].get('error_log_interval', '300'))

# Настройки датчиков
POLLING_INTERVAL = int(config['SENSORS']['polling_interval'])
ENABLE_IMU = config['SENSORS'].getboolean('enable_imu')
ENABLE_SHTC3 = config['SENSORS'].getboolean('enable_shtc3')
ENABLE_GAS_SENSOR = config['SENSORS'].getboolean('enable_gas_sensor')

# Коэффициенты калибровки из конфига
IMU_CALIBRATION = {
    'accel': {
        'x': float(config['CALIBRATION'].get('accel_x_scale', 1.0)),
        'y': float(config['CALIBRATION'].get('accel_y_scale', 1.0)),
        'z': float(config['CALIBRATION'].get('accel_z_scale', 1.0)),
        'x_offset': float(config['CALIBRATION'].get('accel_x_offset', 0.0)),
        'y_offset': float(config['CALIBRATION'].get('accel_y_offset', 0.0)),
        'z_offset': float(config['CALIBRATION'].get('accel_z_offset', 0.0))
    },
    'gyro': {
        'x': float(config['CALIBRATION'].get('gyro_x_scale', 1.0)),
        'y': float(config['CALIBRATION'].get('gyro_y_scale', 1.0)),
        'z': float(config['CALIBRATION'].get('gyro_z_scale', 1.0)),
        'x_offset': float(config['CALIBRATION'].get('gyro_x_offset', 0.0)),
        'y_offset': float(config['CALIBRATION'].get('gyro_y_offset', 0.0)),
        'z_offset': float(config['CALIBRATION'].get('gyro_z_offset', 0.0))
    },
    'mag': {
        'x': float(config['CALIBRATION'].get('mag_x_scale', 1.0)),
        'y': float(config['CALIBRATION'].get('mag_y_scale', 1.0)),
        'z': float(config['CALIBRATION'].get('mag_z_scale', 1.0)),
        'x_offset': float(config['CALIBRATION'].get('mag_x_offset', 0.0)),
        'y_offset': float(config['CALIBRATION'].get('mag_y_offset', 0.0)),
        'z_offset': float(config['CALIBRATION'].get('mag_z_offset', 0.0))
    }
}

GAS_SENSOR_CALIBRATION = {
    'gain': float(config['CALIBRATION'].get('gas_sensor_gain', 1.0)),
    'offsets': [
        float(config['CALIBRATION'].get('gas_sensor_ain0_offset', 0.0)),
        float(config['CALIBRATION'].get('gas_sensor_ain1_offset', 0.0)),
        float(config['CALIBRATION'].get('gas_sensor_ain2_offset', 0.0)),
        float(config['CALIBRATION'].get('gas_sensor_ain3_offset', 0.0))
    ]
}

SHTC3_CALIBRATION = {
    'temp_offset': float(config['CALIBRATION'].get('shtc3_temp_offset', 0.0)),
    'humidity_offset': float(config['CALIBRATION'].get('shtc3_humidity_offset', 0.0))
}

# Глобальные переменные для логирования ошибок NATS
last_nats_error_time = None
last_nats_error_message = None

# Создание директории для логов
os.makedirs(os.path.dirname(DATA_LOG_PATH), exist_ok=True)
os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)

# Инициализация логгера для ошибок
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(ERROR_LOG_PATH, maxBytes=1024 * 1024, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Буфер для хранения данных при отсутствии связи с NATS
data_buffer = []

# Константы и переменные для IMU
Gyro = [0, 0, 0]
Accel = [0, 0, 0]
Mag = [0, 0, 0]
pitch = 0.0
roll = 0.0
yaw = 0.0
pu8data = [0, 0, 0, 0, 0, 0, 0, 0]
U8tempX = [0, 0, 0, 0, 0, 0, 0, 0, 0]
U8tempY = [0, 0, 0, 0, 0, 0, 0, 0, 0]
U8tempZ = [0, 0, 0, 0, 0, 0, 0, 0, 0]
GyroOffset = [0, 0, 0]
Ki = 1.0
Kp = 4.50
q0 = 1.0
q1 = q2 = q3 = 0.0
angles = [0.0, 0.0, 0.0]
true = 0x01
false = 0x00

# Определение адресов и регистров ICM-20948
I2C_ADD_ICM20948 = 0x68
I2C_ADD_ICM20948_AK09916 = 0x0C
I2C_ADD_ICM20948_AK09916_READ = 0x80
I2C_ADD_ICM20948_AK09916_WRITE = 0x00

# Определение регистров ICM-20948
REG_ADD_WIA = 0x00
REG_VAL_WIA = 0xEA
REG_ADD_USER_CTRL = 0x03
REG_VAL_BIT_DMP_EN = 0x80
REG_VAL_BIT_FIFO_EN = 0x40
REG_VAL_BIT_I2C_MST_EN = 0x20
REG_VAL_BIT_I2C_IF_DIS = 0x10
REG_VAL_BIT_DMP_RST = 0x08
REG_VAL_BIT_DIAMOND_DMP_RST = 0x04
REG_ADD_PWR_MGMT_1 = 0x06
REG_VAL_ALL_RGE_RESET = 0x80
REG_VAL_RUN_MODE = 0x01
REG_ADD_LP_CONFIG = 0x05
REG_ADD_PWR_MGMT_2 = 0x07
REG_ADD_ACCEL_XOUT_H = 0x2D
REG_ADD_ACCEL_XOUT_L = 0x2E
REG_ADD_ACCEL_YOUT_H = 0x2F
REG_ADD_ACCEL_YOUT_L = 0x30
REG_ADD_ACCEL_ZOUT_H = 0x31
REG_ADD_ACCEL_ZOUT_L = 0x32
REG_ADD_GYRO_XOUT_H = 0x33
REG_ADD_GYRO_XOUT_L = 0x34
REG_ADD_GYRO_YOUT_H = 0x35
REG_ADD_GYRO_YOUT_L = 0x36
REG_ADD_GYRO_ZOUT_H = 0x37
REG_ADD_GYRO_ZOUT_L = 0x38
REG_ADD_EXT_SENS_DATA_00 = 0x3B
REG_ADD_REG_BANK_SEL = 0x7F
REG_VAL_REG_BANK_0 = 0x00
REG_VAL_REG_BANK_1 = 0x10
REG_VAL_REG_BANK_2 = 0x20
REG_VAL_REG_BANK_3 = 0x30

# Регистры для гироскопа и акселерометра (Bank 2)
REG_ADD_GYRO_SMPLRT_DIV = 0x00
REG_ADD_GYRO_CONFIG_1 = 0x01
REG_ADD_ACCEL_SMPLRT_DIV_2 = 0x11
REG_ADD_ACCEL_CONFIG = 0x14

# Регистры для работы с магнитометром (Bank 3)
REG_ADD_I2C_SLV0_ADDR = 0x03
REG_ADD_I2C_SLV0_REG = 0x04
REG_ADD_I2C_SLV0_CTRL = 0x05
REG_ADD_I2C_SLV0_DO = 0x06
REG_ADD_I2C_SLV1_ADDR = 0x07
REG_ADD_I2C_SLV1_REG = 0x08
REG_ADD_I2C_SLV1_CTRL = 0x09
REG_ADD_I2C_SLV1_DO = 0x0A

# Константы для управления I2C
REG_VAL_BIT_SLV0_EN = 0x80
REG_VAL_BIT_SLV1_EN = 0x40
REG_VAL_BIT_MASK_LEN = 0x07

# Определение регистров магнитометра AK09916
REG_ADD_MAG_WIA1 = 0x00
REG_VAL_MAG_WIA1 = 0x48
REG_ADD_MAG_WIA2 = 0x01
REG_VAL_MAG_WIA2 = 0x09
REG_ADD_MAG_ST2 = 0x10
REG_ADD_MAG_DATA = 0x11
REG_ADD_MAG_CNTL2 = 0x31
REG_VAL_MAG_MODE_PD = 0x00
REG_VAL_MAG_MODE_SM = 0x01
REG_VAL_MAG_MODE_10HZ = 0x02
REG_VAL_MAG_MODE_20HZ = 0x04
REG_VAL_MAG_MODE_50HZ = 0x05
REG_VAL_MAG_MODE_100HZ = 0x08
REG_VAL_MAG_MODE_ST = 0x10

MAG_DATA_LEN = 6

# Константы для SHTC3
SHTC3_I2C_ADDRESS = 0x70
SHTC3_ID = 0xEFC8
CRC_POLYNOMIAL = 0x0131
SHTC3_WakeUp = 0x3517
SHTC3_Sleep = 0xB098
SHTC3_Software_RES = 0x805D
SHTC3_NM_CD_ReadTH = 0x7866
SHTC3_NM_CD_ReadRH = 0x58E0

# Константы для SGM58031
SGM_I2C_ADDRESS = 0x48
SGM_POINTER_CONVERT = 0x00
SGM_POINTER_CONFIG = 0x01
SGM_CONFIG_OS_SINGLE_CONVERT = 0x8000
SGM_CONFIG_MUX_SINGLE_0 = 0x4000
SGM_CONFIG_MUX_SINGLE_1 = 0x5000
SGM_CONFIG_MUX_SINGLE_2 = 0x6000
SGM_CONFIG_MUX_SINGLE_3 = 0x7000
SGM_CONFIG_PGA_4096 = 0x0200
SGM_CONFIG_MODE_NOCONTINUOUS = 0x0100
SGM_CONFIG_COMP_QUE_NON = 0x0003
SGM_CONFIG_COMP_NONLAT = 0x0000
SGM_CONFIG_COMP_POL_LOW = 0x0000
SGM_CONFIG_COMP_MODE_TRADITIONAL = 0x0000
SGM_CONFIG_DR_RATE_480 = 0x00C0

class SGM58031:
    def __init__(self, address=SGM_I2C_ADDRESS):
        self._address = address
        self._bus = smbus.SMBus(1)

    def SGM58031_SINGLE_READ(self, channel):
        data = 0
        Config_Set = (
            SGM_CONFIG_MODE_NOCONTINUOUS |
            SGM_CONFIG_PGA_4096 |
            SGM_CONFIG_COMP_QUE_NON |
            SGM_CONFIG_COMP_NONLAT |
            SGM_CONFIG_COMP_POL_LOW |
            SGM_CONFIG_COMP_MODE_TRADITIONAL |
            SGM_CONFIG_DR_RATE_480
        )
        if channel == 0:
            Config_Set |= SGM_CONFIG_MUX_SINGLE_0
        elif channel == 1:
            Config_Set |= SGM_CONFIG_MUX_SINGLE_1
        elif channel == 2:
            Config_Set |= SGM_CONFIG_MUX_SINGLE_2
        elif channel == 3:
            Config_Set |= SGM_CONFIG_MUX_SINGLE_3
        Config_Set |= SGM_CONFIG_OS_SINGLE_CONVERT
        self._write_word(SGM_POINTER_CONFIG, Config_Set)
        time.sleep(0.02)
        data = self._read_u16(SGM_POINTER_CONVERT)
        # Применяем калибровку
        return (data * 0.125 + GAS_SENSOR_CALIBRATION['offsets'][channel]) * GAS_SENSOR_CALIBRATION['gain']

    def _read_u16(self, cmd):
        LSB = self._bus.read_byte_data(self._address, cmd)
        MSB = self._bus.read_byte_data(self._address, cmd + 1)
        return (LSB << 8) + MSB

    def _write_word(self, cmd, val):
        Val_H = val & 0xff
        Val_L = val >> 8
        val = (Val_H << 8) | Val_L
        self._bus.write_word_data(self._address, cmd, val)

class SHTC3:
    def __init__(self, sbc, bus, address, flags=0):
        self._sbc = sbc
        self._fd = self._sbc.i2c_open(bus, address, flags)
        self.SHTC_SOFT_RESET()

    def SHTC3_CheckCrc(self, data, length, checksum):
        crc = 0xFF
        for byteCtr in range(length):
            crc ^= data[byteCtr]
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ CRC_POLYNOMIAL
                else:
                    crc <<= 1
        return crc == checksum

    def SHTC3_WriteCommand(self, cmd):
        self._sbc.i2c_write_byte_data(self._fd, cmd >> 8, cmd & 0xFF)

    def SHTC3_WAKEUP(self):
        self.SHTC3_WriteCommand(SHTC3_WakeUp)
        time.sleep(0.01)

    def SHTC3_SLEEP(self):
        self.SHTC3_WriteCommand(SHTC3_Sleep)
        time.sleep(0.01)

    def SHTC_SOFT_RESET(self):
        self.SHTC3_WriteCommand(SHTC3_Software_RES)
        time.sleep(0.01)

    def SHTC3_Read_TH(self):
        self.SHTC3_WAKEUP()
        self.SHTC3_WriteCommand(SHTC3_NM_CD_ReadTH)
        time.sleep(0.02)
        count, buf = self._sbc.i2c_read_device(self._fd, 3)
        if count == 3 and self.SHTC3_CheckCrc(buf, 2, buf[2]):
            return round((buf[0] << 8 | buf[1]) * 175 / 65536 - 45.0 + SHTC3_CALIBRATION['temp_offset'], 1)
        return 0

    def SHTC3_Read_RH(self):
        self.SHTC3_WAKEUP()
        self.SHTC3_WriteCommand(SHTC3_NM_CD_ReadRH)
        time.sleep(0.02)
        count, buf = self._sbc.i2c_read_device(self._fd, 3)
        if count == 3 and self.SHTC3_CheckCrc(buf, 2, buf[2]):
            return round(100 * (buf[0] << 8 | buf[1]) / 65536 + SHTC3_CALIBRATION['humidity_offset'], 1)
        return 0

class ICM20948:
    def __init__(self, address=I2C_ADD_ICM20948):
        self._address = address
        self._bus = smbus.SMBus(1)
        self.icm20948Check()
        time.sleep(0.5)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)
        self._write_byte(REG_ADD_PWR_MGMT_1, REG_VAL_ALL_RGE_RESET)
        time.sleep(0.1)
        self._write_byte(REG_ADD_PWR_MGMT_1, REG_VAL_RUN_MODE)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_2)
        self._write_byte(REG_ADD_GYRO_SMPLRT_DIV, 0x07)
        self._write_byte(REG_ADD_GYRO_CONFIG_1, 0x06)
        self._write_byte(REG_ADD_ACCEL_SMPLRT_DIV_2, 0x07)
        self._write_byte(REG_ADD_ACCEL_CONFIG, 0x06)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)
        time.sleep(0.1)
        self.icm20948GyroOffset()
        self.icm20948MagCheck()
        self.icm20948WriteSecondary(I2C_ADD_ICM20948_AK09916 | I2C_ADD_ICM20948_AK09916_WRITE, REG_ADD_MAG_CNTL2, REG_VAL_MAG_MODE_20HZ)

    def icm20948_Gyro_Accel_Read(self):
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)
        data = self._read_block(REG_ADD_ACCEL_XOUT_H, 12)
        
        # Чтение и калибровка акселерометра
        Accel[0] = ((data[0] << 8) | data[1]) * IMU_CALIBRATION['accel']['x'] + IMU_CALIBRATION['accel']['x_offset']
        Accel[1] = ((data[2] << 8) | data[3]) * IMU_CALIBRATION['accel']['y'] + IMU_CALIBRATION['accel']['y_offset']
        Accel[2] = ((data[4] << 8) | data[5]) * IMU_CALIBRATION['accel']['z'] + IMU_CALIBRATION['accel']['z_offset']
        
        # Чтение и калибровка гироскопа
        Gyro[0] = (((data[6] << 8) | data[7]) - GyroOffset[0]) * IMU_CALIBRATION['gyro']['x'] + IMU_CALIBRATION['gyro']['x_offset']
        Gyro[1] = (((data[8] << 8) | data[9]) - GyroOffset[1]) * IMU_CALIBRATION['gyro']['y'] + IMU_CALIBRATION['gyro']['y_offset']
        Gyro[2] = (((data[10] << 8) | data[11]) - GyroOffset[2]) * IMU_CALIBRATION['gyro']['z'] + IMU_CALIBRATION['gyro']['z_offset']
        
        self._adjust_overflow(Accel, Gyro)

    def icm20948MagRead(self):
        counter = 20
        while counter > 0:
            time.sleep(0.01)
            self.icm20948ReadSecondary(I2C_ADD_ICM20948_AK09916 | I2C_ADD_ICM20948_AK09916_READ, REG_ADD_MAG_ST2, 1)
            if (pu8data[0] & 0x01) != 0:
                break
            counter -= 1
        if counter != 0:
            for i in range(8):
                self.icm20948ReadSecondary(I2C_ADD_ICM20948_AK09916 | I2C_ADD_ICM20948_AK09916_READ, REG_ADD_MAG_DATA, MAG_DATA_LEN)
                U8tempX[i] = (pu8data[1] << 8) | pu8data[0]
                U8tempY[i] = (pu8data[3] << 8) | pu8data[2]
                U8tempZ[i] = (pu8data[5] << 8) | pu8data[4]
            
            # Калибровка магнитометра
            Mag[0] = ((U8tempX[0] + U8tempX[1] + U8tempX[2] + U8tempX[3] + 
                      U8tempX[4] + U8tempX[5] + U8tempX[6] + U8tempX[7]) / 8) * IMU_CALIBRATION['mag']['x'] + IMU_CALIBRATION['mag']['x_offset']
            Mag[1] = -((U8tempY[0] + U8tempY[1] + U8tempY[2] + U8tempY[3] + 
                       U8tempY[4] + U8tempY[5] + U8tempY[6] + U8tempY[7]) / 8) * IMU_CALIBRATION['mag']['y'] + IMU_CALIBRATION['mag']['y_offset']
            Mag[2] = -((U8tempZ[0] + U8tempZ[1] + U8tempZ[2] + U8tempZ[3] + 
                       U8tempZ[4] + U8tempZ[5] + U8tempZ[6] + U8tempZ[7]) / 8) * IMU_CALIBRATION['mag']['z'] + IMU_CALIBRATION['mag']['z_offset']
            
            self._adjust_overflow(Mag)

    def _adjust_overflow(self, *sensors):
        for sensor in sensors:
            for i in range(3):
                if sensor[i] >= 32767:
                    sensor[i] -= 65535
                elif sensor[i] <= -32767:
                    sensor[i] += 65535

    def icm20948ReadSecondary(self, u8I2CAddr, u8RegAddr, u8Len):
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_3)
        self._write_byte(REG_ADD_I2C_SLV0_ADDR, u8I2CAddr)
        self._write_byte(REG_ADD_I2C_SLV0_REG, u8RegAddr)
        self._write_byte(REG_ADD_I2C_SLV0_CTRL, REG_VAL_BIT_SLV0_EN | u8Len)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)
        u8Temp = self._read_byte(REG_ADD_USER_CTRL)
        u8Temp |= REG_VAL_BIT_I2C_MST_EN
        self._write_byte(REG_ADD_USER_CTRL, u8Temp)
        time.sleep(0.01)
        u8Temp &= ~REG_VAL_BIT_I2C_MST_EN
        self._write_byte(REG_ADD_USER_CTRL, u8Temp)
        for i in range(u8Len):
            pu8data[i] = self._read_byte(REG_ADD_EXT_SENS_DATA_00 + i)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_3)
        u8Temp = self._read_byte(REG_ADD_I2C_SLV0_CTRL)
        u8Temp &= ~((REG_VAL_BIT_I2C_MST_EN) & (REG_VAL_BIT_MASK_LEN))
        self._write_byte(REG_ADD_I2C_SLV0_CTRL, u8Temp)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)

    def icm20948WriteSecondary(self, u8I2CAddr, u8RegAddr, u8data):
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_3)
        self._write_byte(REG_ADD_I2C_SLV1_ADDR, u8I2CAddr)
        self._write_byte(REG_ADD_I2C_SLV1_REG, u8RegAddr)
        self._write_byte(REG_ADD_I2C_SLV1_DO, u8data)
        self._write_byte(REG_ADD_I2C_SLV1_CTRL, REG_VAL_BIT_SLV0_EN | 1)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)
        u8Temp = self._read_byte(REG_ADD_USER_CTRL)
        u8Temp |= REG_VAL_BIT_I2C_MST_EN
        self._write_byte(REG_ADD_USER_CTRL, u8Temp)
        time.sleep(0.01)
        u8Temp &= ~REG_VAL_BIT_I2C_MST_EN
        self._write_byte(REG_ADD_USER_CTRL, u8Temp)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_3)
        u8Temp = self._read_byte(REG_ADD_I2C_SLV0_CTRL)
        u8Temp &= ~((REG_VAL_BIT_I2C_MST_EN) & (REG_VAL_BIT_MASK_LEN))
        self._write_byte(REG_ADD_I2C_SLV0_CTRL, u8Temp)
        self._write_byte(REG_ADD_REG_BANK_SEL, REG_VAL_REG_BANK_0)

    def icm20948GyroOffset(self):
        s32TempGx = s32TempGy = s32TempGz = 0
        for _ in range(32):
            self.icm20948_Gyro_Accel_Read()
            s32TempGx += Gyro[0]
            s32TempGy += Gyro[1]
            s32TempGz += Gyro[2]
            time.sleep(0.01)
        GyroOffset[0] = int(s32TempGx / 32)  # Используем целочисленное деление вместо сдвига
        GyroOffset[1] = int(s32TempGy / 32)
        GyroOffset[2] = int(s32TempGz / 32)

    def _read_byte(self, cmd):
        return self._bus.read_byte_data(self._address, cmd)

    def _read_block(self, reg, length=1):
        return self._bus.read_i2c_block_data(self._address, reg, length)

    def _write_byte(self, cmd, val):
        self._bus.write_byte_data(self._address, cmd, val)
        time.sleep(0.0001)

    def icm20948Check(self):
        return self._read_byte(REG_ADD_WIA) == REG_VAL_WIA

    def icm20948MagCheck(self):
        self.icm20948ReadSecondary(I2C_ADD_ICM20948_AK09916 | I2C_ADD_ICM20948_AK09916_READ, REG_ADD_MAG_WIA1, 2)
        return pu8data[0] == REG_VAL_MAG_WIA1 and pu8data[1] == REG_VAL_MAG_WIA2

    def imuAHRSupdate(self, gx, gy, gz, ax, ay, az, mx, my, mz):
        global q0, q1, q2, q3

        norm = 0.0
        hx = hy = hz = bx = bz = 0.0
        vx = vy = vz = wx = wy = wz = 0.0
        exInt = eyInt = ezInt = 0.0
        ex = ey = ez = 0.0
        halfT = 0.024

        norm = float(1 / math.sqrt(ax * ax + ay * ay + az * az))
        ax *= norm
        ay *= norm
        az *= norm

        norm = float(1 / math.sqrt(mx * mx + my * my + mz * mz))
        mx *= norm
        my *= norm
        mz *= norm

        hx = 2 * mx * (0.5 - q2 * q2 - q3 * q3) + 2 * my * (q1 * q2 - q0 * q3) + 2 * mz * (q1 * q3 + q0 * q2)
        hy = 2 * mx * (q1 * q2 + q0 * q3) + 2 * my * (0.5 - q1 * q1 - q3 * q3) + 2 * mz * (q2 * q3 - q0 * q1)
        hz = 2 * mx * (q1 * q3 - q0 * q2) + 2 * my * (q2 * q3 + q0 * q1) + 2 * mz * (0.5 - q1 * q1 - q2 * q2)
        bx = math.sqrt((hx * hx) + (hy * hy))
        bz = hz

        vx = 2 * (q1 * q3 - q0 * q2)
        vy = 2 * (q0 * q1 + q2 * q3)
        vz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3
        wx = 2 * bx * (0.5 - q2 * q2 - q3 * q3) + 2 * bz * (q1 * q3 - q0 * q2)
        wy = 2 * bx * (q1 * q2 - q0 * q3) + 2 * bz * (q0 * q1 + q2 * q3)
        wz = 2 * bx * (q0 * q2 + q1 * q3) + 2 * bz * (0.5 - q1 * q1 - q2 * q2)

        ex = (ay * vz - az * vy) + (my * wz - mz * wy)
        ey = (az * vx - ax * vz) + (mz * wx - mx * wz)
        ez = (ax * vy - ay * vx) + (mx * wy - my * wx)

        if ex != 0.0 and ey != 0.0 and ez != 0.0:
            exInt += ex * Ki * halfT
            eyInt += ey * Ki * halfT
            ezInt += ez * Ki * halfT

            gx = gx + Kp * ex + exInt
            gy = gy + Kp * ey + eyInt
            gz = gz + Kp * ez + ezInt

        q0 = q0 + (-q1 * gx - q2 * gy - q3 * gz) * halfT
        q1 = q1 + (q0 * gx + q2 * gz - q3 * gy) * halfT
        q2 = q2 + (q0 * gy - q1 * gz + q3 * gx) * halfT
        q3 = q3 + (q0 * gz + q1 * gy - q2 * gx) * halfT

        norm = float(1 / math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3))
        q0 = q0 * norm
        q1 = q1 * norm
        q2 = q2 * norm
        q3 = q3 * norm

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

async def process_sensor_data():
    """Обработка данных с датчиков и их сохранение"""
    global data_buffer
    
    try:
        # Получаем данные с датчиков
        if ENABLE_IMU:
            icm20948.icm20948_Gyro_Accel_Read()
            icm20948.icm20948MagRead()
            icm20948.imuAHRSupdate(
                Gyro[0] * 0.0175, Gyro[1] * 0.0175, Gyro[2] * 0.0175,
                Accel[0], Accel[1], Accel[2],
                Mag[0], Mag[1], Mag[2]
            )
            pitch = math.asin(-2 * q1 * q3 + 2 * q0 * q2) * 57.3
            roll = math.atan2(2 * q2 * q3 + 2 * q0 * q1, -2 * q1 * q1 - 2 * q2 * q2 + 1) * 57.3
            yaw = math.atan2(-2 * q1 * q2 - 2 * q0 * q3, 2 * q2 * q2 + 2 * q3 * q3 - 1) * 57.3

        if ENABLE_SHTC3:
            temperature = shtc3.SHTC3_Read_TH()
            humidity = shtc3.SHTC3_Read_RH()

        if ENABLE_GAS_SENSOR:
            gas_data = {
                "AIN0": sgm58031.SGM58031_SINGLE_READ(0),
                "AIN1": sgm58031.SGM58031_SINGLE_READ(1),
                "AIN2": sgm58031.SGM58031_SINGLE_READ(2),
                "AIN3": sgm58031.SGM58031_SINGLE_READ(3)
            }

        # Формируем данные для отправки
        data = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "imu": {
                "roll": roll if ENABLE_IMU else None,
                "pitch": pitch if ENABLE_IMU else None,
                "yaw": yaw if ENABLE_IMU else None,
                "accel": {"x": Accel[0], "y": Accel[1], "z": Accel[2]} if ENABLE_IMU else None,
                "gyro": {"x": Gyro[0], "y": Gyro[1], "z": Gyro[2]} if ENABLE_IMU else None,
                "mag": {"x": Mag[0], "y": Mag[1], "z": Mag[2]} if ENABLE_IMU else None
            },
            "shtc3": {
                "temperature": temperature if ENABLE_SHTC3 else None,
                "humidity": humidity if ENABLE_SHTC3 else None
            },
            "gas_sensor": gas_data if ENABLE_GAS_SENSOR else None
        }

        # Сохраняем данные в лог
        write_to_data_log(data)
        
        # Добавляем в буфер
        if len(data_buffer) < BUFFER_SIZE:
            data_buffer.append(data)
        else:
            logger.warning("NATS buffer full, discarding oldest message")
            data_buffer.pop(0)
            data_buffer.append(data)
            
        return data
        
    except Exception as e:
        logger.error(f"Sensor data processing error: {e}")
        return None

async def main():
    global icm20948, shtc3, sgm58031
    
    # Инициализация датчиков
    icm20948 = ICM20948() if ENABLE_IMU else None
    shtc3 = SHTC3(sbc, 1, SHTC3_I2C_ADDRESS) if ENABLE_SHTC3 else None
    sgm58031 = SGM58031() if ENABLE_GAS_SENSOR else None
    
    last_send_time = time.time()

    while True:
        try:
            # Получаем данные с датчиков
            data = await process_sensor_data()
            
            # Отправляем данные по интервалу
            current_time = time.time()
            if data and (current_time - last_send_time >= NATS_SEND_INTERVAL):
                if data_buffer:
                    success = True
                    for item in data_buffer:
                        if not await send_to_nats(item):
                            success = False
                            break
                    
                    if success:
                        data_buffer.clear()
                        last_send_time = current_time
            
            await asyncio.sleep(POLLING_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Program stopped by user")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            await asyncio.sleep(1)

if __name__ == '__main__':
    asyncio.run(main())