# Используем легковесный образ Python
FROM python:3.9-slim

# Устанавливаем зависимости для работы с последовательным портом
RUN apt-get update && apt-get install -y \
    libgpiod2 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

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
COPY logs /app/logs

# Запускаем скрипт при старте контейнера
CMD ["python", "/app/main.py"]