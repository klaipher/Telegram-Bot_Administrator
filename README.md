# Telegram-Bot_Administrator
Бот для администрирования суперчатов

# Установка
Создаем виртуальное окружение с Python 3 и ставим зависимости
```
virtualenv venv
source venv/bin/activate
pip install -r requirements.txt
```

# Конфигурация
1. Вам нужно поговорить с BotFather, как описано [здесь](https://core.telegram.org/bots#botfather) и получите API токен.
Также вам нужно отключить конфиденциальность, отправив BotFather команду `/setprivacy`
2. Редактируем файл bot/config.py. И выставляем свою конфигурацию.
3. Для создание таблицы в БД отредактируйте строку в main.py:
```
conn = loop.run_until_complete(create_conn(**DB, create_table=True))
```
