#!/bin/bash

# Проверяем, доступен ли can1
if ! ip link show can1 > /dev/null 2>&1; then
    echo "Ошибка: can0 не найден!"
    exit 1
fi

# Настраиваем can0
ip link set can1 down
ip link set can1 up type can bitrate 500000

# Запускаем основной скрипт
python /app/main.py
