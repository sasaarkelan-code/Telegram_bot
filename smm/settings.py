"""Shared settings validation, independent of Telegram UI."""
import re

LIMIT_FIELDS = {
    "daily_tokens": ("Токенов за день", 12000, 2000000),
    "rolling_tokens": ("Токенов за 5 часов", 12000, 1000000),
    "channel_daily_tokens": ("На канал за день", 12000, 1000000),
    "daily_requests": ("Запросов за день", 1, 500),
    "hourly_requests": ("Запросов за час", 1, 100),
    "fashion_steps": ("Запросов на подборку одежды", 1, 2),
    "fashion_slot_tokens": ("Токенов на одну подборку одежды", 12000, 60000),
    "fashion_web_calls": ("Поисков за один запрос одежды", 1, 8),
    "fashion_seconds": ("Секунд на поисковый запрос", 30, 240),
}


def parse_times(text):
    values = re.split(r"[\s,;]+", text.strip())
    if not 1 <= len(values) <= 8 or len(set(values)) != len(values):
        raise ValueError("Укажи от 1 до 8 разных времён через пробел.")
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", x) for x in values):
        raise ValueError("Формат времени: 09:00 15:00 21:00.")
    return sorted(values)

