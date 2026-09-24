"""Настройки из файла .env в корне проекта."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
MOCK_TENDERS_FILE = DATA_DIR / "mock_tenders.json"

# override=True: .env важнее переменных окружения — иначе, например, чужой
# ANTHROPIC_BASE_URL из системы незаметно перенаправит запросы не туда.
load_dotenv(ROOT_DIR / ".env", override=True)

TENDERPLAN_API_KEY = os.getenv("TENDERPLAN_API_KEY", "").strip()
TENDERPLAN_SEARCH_KEY_ID = os.getenv("TENDERPLAN_SEARCH_KEY_ID", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# Доступ через сторонний шлюз (прокси к API Anthropic): свой адрес и токен.
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "").strip()
ANTHROPIC_AUTH_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
ANTHROPIC_CONFIGURED = bool(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN)
# Резервная модель при отказе (бета-функция API). Шлюзы её часто не поддерживают.
ANTHROPIC_FALLBACKS = os.getenv("ANTHROPIC_FALLBACKS", "off" if ANTHROPIC_BASE_URL else "on").strip().lower() == "on"
# Как получать ответ по JSON-схеме: output_config (прямой API) или tool —
# принудительный вызов инструмента. Шлюзы (в т.ч. baza-ai) молча игнорируют
# output_config, поэтому для шлюза по умолчанию tool.
ANTHROPIC_STRUCTURED = os.getenv("ANTHROPIC_STRUCTURED", "tool" if ANTHROPIC_BASE_URL else "output_config").strip().lower()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip()

# Источник тендеров: auto — Тендерплан, если задан ключ, иначе тестовые данные;
# mock — всегда тестовые данные (удобно для демо без интернета).
DATA_SOURCE = os.getenv("DATA_SOURCE", "auto").strip().lower()
USE_MOCK_TENDERS = DATA_SOURCE == "mock" or not TENDERPLAN_API_KEY

CACHE_DIR = DATA_DIR / "cache"

# Вход по паролю (для сервера). Пусто — вход без пароля, как при запуске у себя.
RADAR_USER = os.getenv("RADAR_USER", "radar").strip()
RADAR_PASSWORD = os.getenv("RADAR_PASSWORD", "").strip()

# Подпись менеджера: подставляется в черновики вместо [Имя менеджера] и т.п.
# Пустое поле остаётся заполнителем в квадратных скобках.
SENDER = {
    "[Имя менеджера]": os.getenv("SENDER_NAME", "").strip(),
    "[Должность]": os.getenv("SENDER_POSITION", "").strip(),
    "[Название компании]": os.getenv("SENDER_COMPANY", "").strip(),
    "[Телефон]": os.getenv("SENDER_PHONE", "").strip(),
    "[Email]": os.getenv("SENDER_EMAIL", "").strip(),
}

# Текст для дашборда о расписании автопроверки (скрипт установки на сервер задаёт его сам).
RADAR_AUTO_CHECK = os.getenv("RADAR_AUTO_CHECK", "").strip()
