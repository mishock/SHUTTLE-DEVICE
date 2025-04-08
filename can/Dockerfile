# Используем легковесный образ Python
FROM python:3.9-slim

# Устанавливаем зависимости для работы с CAN
RUN apt-get update && apt-get install -y can-utils iproute2 && rm -rf /var/lib/apt/lists/*

# Устанавливаем необходимые Python-библиотеки
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Создаём рабочую директорию в контейнере
WORKDIR /app

# Копируем файлы проекта в контейнер
COPY main.py .
COPY config ./config

# Создаем директорию для логов, если она не существует
RUN mkdir -p /app/logs

# Даем права на запись в логи
RUN chmod -R 777 /app/logs

# Копируем логи (если они уже есть на хосте)
COPY logs ./logs

# Копируем и настраиваем скрипт запуска
COPY setup.sh /app/setup.sh
RUN chmod +x /app/setup.sh

# Запускаем скрипт при старте контейнера
CMD ["/bin/bash", "/app/setup.sh"]
