# Telegram-Bot_Administrator
Superadmin Chat Bot 
Supports Python 3.6+

# Установка
We create a virtual environment with Python 3 and installing dependencies
```
virtualenv venv
source venv/bin/activate
pip install -r requirements.txt
```

# Конфигурация
1. You need to talk to BotFather as described [here](https://core.telegram.org/bots#botfather) and get an API token.
Also you need to disable privacy by sending BotFather command `/setprivacy`
2. Edit the bot/config.py file. And we setup your configuration.
3. To create a table in the database, edit the line in main.py:
```
conn = loop.run_until_complete(create_conn(**DB, create_table=True))
```
