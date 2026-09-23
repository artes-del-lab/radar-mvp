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
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5").strip()

# Нет ключа Тендерплана — работаем на тестовых данных.
USE_MOCK_TENDERS = not TENDERPLAN_API_KEY
