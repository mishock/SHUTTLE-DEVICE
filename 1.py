import RPi.GPIO as GPIO
import serial
import time

ser = serial.Serial("/dev/ttyUSB0", 115200)
ser.flushInput()

command_input = ''
rec_buff = ''

try:
    while True:
        command_input = input('Please input the AT command: ')  # raw_input() заменено на input() для Python 3
        ser.write((command_input + '\r\n').encode())
        time.sleep(0.1)
        if ser.inWaiting():
            time.sleep(0.01)
            rec_buff = ser.read(ser.inWaiting())
        if rec_buff != '':
            print(rec_buff.decode())
            rec_buff = ''
except:
    ser.close()
    GPIO.cleanup()
