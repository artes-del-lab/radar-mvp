"""Общая обвязка вокруг Claude: запрос со структурированным ответом и кэш.

Используется оценкой тендеров (scoring.py) и черновиками касаний (outreach.py).
"""

import hashlib
import json
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from . import config

T = TypeVar("T", bound=BaseModel)


class LlmError(Exception):
    pass


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON-схема Pydantic-модели в виде, который принимает API: без $ref и лишних полей."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            node = {k: resolve(v) for k, v in node.items() if k not in ("title", "description")}
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            return node
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if not config.ANTHROPIC_API_KEY:
        raise LlmError("Не задан ANTHROPIC_API_KEY в файле .env")
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def ask_json(system: str, prompt: str, answer_model: type[T]) -> tuple[T, str]:
    """Спрашивает Claude и возвращает (проверенный ответ, модель, которая ответила)."""
    try:
        response = _get_client().beta.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": strict_schema(answer_model)}},
            # Если модель откажется отвечать, API сам повторит запрос на резервной модели.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise LlmError("Anthropic: неверный ANTHROPIC_API_KEY") from e
    except anthropic.RateLimitError as e:
        raise LlmError("Anthropic: превышен лимит запросов, попробуйте через минуту") from e
    except anthropic.APIStatusError as e:
        raise LlmError(f"Anthropic: ошибка {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LlmError("Anthropic: нет соединения с API") from e

    if response.stop_reason == "refusal":
        raise LlmError("Claude отказался отвечать на этот запрос")
    if response.stop_reason == "max_tokens":
        raise LlmError("Ответ Claude обрезан по лимиту длины")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LlmError("Claude вернул пустой ответ")
    return answer_model.model_validate_json(text), response.model


class JsonCache:
    """Кэш результатов в JSON-файле. Запись актуальна, пока совпадает отпечаток."""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def fingerprint(*parts: str) -> str:
        raw = "|".join(parts + (config.ANTHROPIC_MODEL,))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, key: str, fingerprint: str) -> dict | None:
        entry = self._load().get(key)
        if entry and entry.get("fingerprint") == fingerprint:
            return entry["value"]
        return None

    def put(self, key: str, fingerprint: str, value: dict) -> None:
        data = self._load()
        data[key] = {"fingerprint": fingerprint, "value": value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
