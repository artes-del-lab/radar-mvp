"""Общая обвязка вокруг Claude: запрос со структурированным ответом и кэш.

Используется оценкой тендеров (scoring.py) и черновиками касаний (outreach.py).
"""

import hashlib
import json
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from . import config

T = TypeVar("T", bound=BaseModel)

_ANSWER_TOOL = "answer"


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
    if not config.ANTHROPIC_CONFIGURED:
        raise LlmError("Не задан ANTHROPIC_API_KEY (или ANTHROPIC_AUTH_TOKEN) в файле .env")
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY or None,
            auth_token=None if config.ANTHROPIC_API_KEY else config.ANTHROPIC_AUTH_TOKEN,
            base_url=config.ANTHROPIC_BASE_URL or None,
        )
    return _client


def ask_json(system: str, prompt: str, answer_model: type[T], max_tokens: int) -> tuple[T, str]:
    """Спрашивает Claude и возвращает (проверенный ответ, модель, которая ответила).

    max_tokens — с небольшим запасом к реальной длине ответа: шлюз резервирует
    его под квоту, и при большом значении отказывает, хотя квота ещё есть.
    """
    params = dict(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    schema = strict_schema(answer_model)
    if config.ANTHROPIC_STRUCTURED == "tool":
        # Шлюзы молча игнорируют output_config и отвечают свободным текстом.
        # Принудительный вызов инструмента они пропускают: ответ приходит
        # аргументами инструмента в виде JSON по схеме.
        params.update(
            tools=[{"name": _ANSWER_TOOL, "description": "Вернуть ответ", "strict": True, "input_schema": schema}],
            tool_choice={"type": "tool", "name": _ANSWER_TOOL},
        )
    else:
        params.update(output_config={"format": {"type": "json_schema", "schema": schema}})
    if config.ANTHROPIC_FALLBACKS:
        # Если модель откажется отвечать, API сам повторит запрос на резервной модели.
        params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    # Шлюз не гарантирует соответствие схеме, поэтому при кривом ответе
    # спрашиваем ещё раз.
    for attempt in range(_ATTEMPTS):
        response = _send(params)
        try:
            return _parse(response, answer_model), response.model
        except (ValidationError, _NoAnswer) as e:
            if attempt == _ATTEMPTS - 1:
                raise LlmError(f"Claude вернул ответ не по схеме ({_ATTEMPTS} попытки подряд)") from e


_ATTEMPTS = 2


class _NoAnswer(Exception):
    pass


def _send(params: dict):
    try:
        response = _get_client().beta.messages.create(**params)
    except anthropic.AuthenticationError as e:
        raise LlmError("Anthropic: ключ или токен не приняты (401)") from e
    except anthropic.RateLimitError as e:
        if "quota" in str(e.message).lower():
            # Так отвечает шлюз, когда исчерпана квота токенов на ключе.
            raise LlmError("Исчерпана квота токенов у шлюза: подождите или пополните баланс") from e
        raise LlmError("Anthropic: превышен лимит запросов, попробуйте через минуту") from e
    except anthropic.APIStatusError as e:
        raise LlmError(f"Anthropic: ошибка {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LlmError("Anthropic: нет соединения с API") from e

    if response.stop_reason == "refusal":
        raise LlmError("Claude отказался отвечать на этот запрос")
    if response.stop_reason == "max_tokens":
        raise LlmError("Ответ Claude обрезан по лимиту длины")
    return response


def _parse(response, answer_model: type[T]) -> T:
    if config.ANTHROPIC_STRUCTURED == "tool":
        data = next((b.input for b in response.content if b.type == "tool_use"), None)
        if data is None:
            raise _NoAnswer
        return answer_model.model_validate(data)
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise _NoAnswer
    return answer_model.model_validate_json(text)


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
