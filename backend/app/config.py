"""Настройки из файла .env в корне проекта."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
MOCK_TENDERS_FILE = DATA_DIR / "mock_tenders.json"

load_dotenv(ROOT_DIR / ".env")

TENDERPLAN_API_KEY = os.getenv("TENDERPLAN_API_KEY", "").strip()
TENDERPLAN_SEARCH_KEY_ID = os.getenv("TENDERPLAN_SEARCH_KEY_ID", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip()

# Источник тендеров: auto — Тендерплан, если задан ключ, иначе тестовые данные;
# mock — всегда тестовые данные (удобно для демо без интернета).
DATA_SOURCE = os.getenv("DATA_SOURCE", "auto").strip().lower()
USE_MOCK_TENDERS = DATA_SOURCE == "mock" or not TENDERPLAN_API_KEY

CACHE_DIR = DATA_DIR / "cache"
