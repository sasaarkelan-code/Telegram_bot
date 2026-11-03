from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def data_dir() -> Path:
    path = Path(os.environ.get("SMM_DATA_DIR", Path.home() / "SMMManagerData")).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".smm-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


DEFAULT_LIMITS = {
    "daily_tokens": 300000,
    "rolling_tokens": 180000,
    "channel_daily_tokens": 200000,
    "daily_requests": 100,
    "hourly_requests": 40,
    "request_reserve": 12000,
    "request_output_bytes": 6500,
    "request_seconds": 90,
    "web_calls": 2,
    "fashion_web_calls": 4,
    "fashion_output_bytes": 24000,
    "fashion_seconds": 150,
    "fashion_steps": 10,
    "lead_minutes": 40,
    "max_attempts_per_slot": 2,
}

DEFAULT_CHANNELS = [
    dict(name=".WORLD", mode="news", topic="Важные новости со всего мира. Разные страны и темы. Проверять дату события, не выдавать старую новость за новую.",
         times=["09:00", "12:00", "15:00", "18:00", "21:00"], max_age_hours=18,
         sources=["https://feeds.bbci.co.uk/news/world/rss.xml", "https://news.un.org/feed/subscribe/ru/news/all/rss.xml"]),
    dict(name="Капитал по делу", mode="news", topic="Вклады, инвестиции и финансовые новости. Условия продуктов с датой и источником, понятные разборы. Не давать персональных инвестиционных рекомендаций и не выдумывать цитаты.",
         times=["09:00", "15:00", "21:00"], max_age_hours=36,
         sources=["https://www.cbr.ru/rss/RssNews"]),
    dict(name="Нейросети на практике", mode="news", topic="Нейросети: значимые обновления, полезные сервисы, практическое применение и проверяемые инструкции. Не придумывать доступность, цены и функции.",
         times=["09:00", "15:00", "21:00"], max_age_hours=48,
         sources=["https://openai.com/news/rss.xml", "https://blogs.microsoft.com/ai/feed/"]),
    dict(name="Игровая сводка", mode="news", topic="Видеоигры: важные анонсы, релизы, обновления и раздачи. Указывать платформу, регион и срок акции, если они известны.",
         times=["09:00", "15:00", "21:00"], max_age_hours=48,
         sources=["https://blog.playstation.com/feed/", "https://news.xbox.com/en-us/feed/"]),
    dict(name="Собрано | Доступный стиль", mode="fashion", topic="Полные гармоничные зимние образы с Wildberries и Ozon. Куртка/пуховик, свитер/худи, брюки, зимняя обувь, шапка, сумка; аксессуары по необходимости. Мужские и женские образы. Две взрослые модели разного телосложения в одном и том же комплекте, без различимых лиц.",
         times=["15:00", "21:00"], max_age_hours=24, sources=[],
         budgets={"15:00": 10000, "21:00": 20000}, web_search=True),
    dict(name="Кино на вечер", mode="cinema", topic="Два интересных фильма или сериала на выбор. Название, год, коротко кому подойдёт, без спойлеров. Изучить настоящие отзывы; не выдавать рекламный анонс за отзыв, не сочинять рейтинг и цитаты.",
         times=["21:00"], max_age_hours=336,
         sources=["https://thefilmstage.com/feed/"]),
]


def channel_defaults() -> dict:
    return dict(
        topic="", mode="news", times=["09:00", "15:00", "21:00"],
        max_age_hours=24, sources=[], include=[], exclude=[],
        style="Русский язык. Живой, понятный, спокойный стиль. Короткие абзацы, без кликбейта и штампов. Не более 650 знаков основного текста.",
        require_photo=True, budgets={}, web_search=False,
    )


def read_config(root: Path) -> dict:
    path = root / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
