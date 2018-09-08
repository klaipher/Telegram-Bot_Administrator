def calculate_time(time_ban: str) -> tuple:
    """
    Рассчитать время бана.
    """
    times = {'w': 10080,
             'd': 1440,
             'h': 60,
             'm': 1}
    time_period = {'w': 'нед.',
                   'd': 'дн.',
                   'h': 'час.',
                   'm': 'мин.'}
    if not time_ban.isdigit():
        if time_ban[-1] in times.keys():
            period = time_ban[-1]
            time_ban = int(time_ban[:-1]) * times[period]
            return time_ban, time_period[period]  # Переводим в минуты
    else:
        raise ValueError  # Показать ошибку, чтобы ее обработать


def rate_limit(limit: int, key=None):
    """
    Декоратор для настройки ограничения скорости и ключа в различных функциях.
    """

    def decorator(func):
        setattr(func, 'throttling_rate_limit', limit)
        if key:
            setattr(func, 'throttling_key', key)
        return func

    return decorator
