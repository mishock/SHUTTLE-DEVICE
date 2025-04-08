from gpiozero import LED
import time
import requests

# Функция проверки наличия интернета
def check_internet():
    try:
        requests.get("https://www.google.com", timeout=5)
        return True
    except requests.RequestException:
        return False

def main():
    LED_PIN = 21  # Пин, к которому подключен светодиод
    led = LED(LED_PIN)  # Инициализация светодиода

    try:
        while True:
            if check_internet():
                led.on()  # Включаем светодиод, если есть интернет
            else:
                led.off()  # Выключаем светодиод, если интернета нет
            time.sleep(10)  # Проверяем интернет каждые 10 секунд
    except KeyboardInterrupt:
        led.close()  # Очищаем ресурсы
        print("Выход из программы")

if __name__ == "__main__":
    main()