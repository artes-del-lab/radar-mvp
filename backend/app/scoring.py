"""Оценка релевантности тендера.

Два этапа:
1. Быстрый предфильтр в коде (бесплатно). Если у лота чужой код ОКПД2 и в
   тексте нет ни типов техники, ни брендов — это точно не наш тендер,
   Claude не вызываем.
2. Остальное оценивает Claude: балл 0–100, короткое обоснование для менеджера
   и разбор по четырём критериям.

Результаты кэшируются в data/cache/evaluations.json: повторный показ дашборда
не стоит денег. Кэш сбрасывается сам, если изменился тендер, модель или промпт.

Запуск из консоли (оценить весь текущий список):
    cd backend && python -m app.scoring
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import config, criteria
from .models import Tender

PROMPT_VERSION = "1"
CACHE_FILE = config.CACHE_DIR / "evaluations.json"

Status = Literal["ok", "warn", "bad"]
Level = Literal["high", "medium", "low"]


class Check(BaseModel):
    status: Status
    note: str


class Checks(BaseModel):
    okpd2: Check = Field(description="Код ОКПД2 и тип техники")
    brand: Check = Field(description="Соответствие брендам")
    price: Check = Field(description="Разумность НМЦК")
    deadline: Check = Field(description="Актуальность срока подачи")


class Evaluation(BaseModel):
    tender_id: str
    score: int = Field(description="0–100")
    level: Level
    summary: str = Field(description="Обоснование для менеджера, 1–2 предложения")
    checks: Checks
    method: Literal["llm", "prefilter"]
    model: str | None = None
    evaluated_at: datetime


class ScoringError(Exception):
    pass


# --- то, что возвращает Claude ---


class _LlmAnswer(BaseModel):
    score: int
    summary: str
    checks: Checks


def _strict_schema(schema: dict) -> dict:
    """JSON-схема для структурированного ответа: без $ref и лишних полей."""
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


ANSWER_SCHEMA = _strict_schema(_LlmAnswer.model_json_schema())

SYSTEM_PROMPT = f"""Ты — аналитик отдела продаж компании, эксклюзивного представителя \
немецкого поставщика спецтехники на российском рынке. Компания поставляет новую \
технику брендов {", ".join(criteria.BRANDS)}: экскаваторы, бульдозеры, погрузчики, \
грейдеры, краны, тракторы и сельхозтехнику.

Твоя задача — оценить, стоит ли менеджеру тратить время на тендер: может ли \
компания выиграть его своей техникой.

Критерии:
1. okpd2 — предмет закупки: это поставка техники, которую продаёт компания? \
Целевые группы ОКПД2: {", ".join(criteria.OKPD2_INCLUDE)} (кроме легковых и автобусов). \
Услуги, аренда, запчасти, шины, мебель и прочее — не основной профиль (запчасти к \
технике наших брендов — частичное попадание).
2. brand — упомянуты ли наши бренды, допускается ли эквивалент, нет ли требований, \
исключающих нашу технику (конкретный чужой бренд, отечественное шасси, локализация).
3. price — разумна ли НМЦК для такой техники. Ориентир нижнего порога — \
{criteria.MIN_NMCK_RUB:,} руб.; слишком низкая цена или допуск б/у — плохой знак.
4. deadline — сколько дней осталось до окончания подачи. Меньше 7 дней — тесно, \
срок прошёл — участвовать нельзя.

Статус каждого критерия: ok — подходит, warn — есть оговорки, bad — не подходит. \
В note — 3–10 слов по сути.

score (0–100): 70+ — стоит заняться, 40–69 — посмотреть внимательнее, ниже 40 — мимо. \
Истёкший срок подачи или предмет закупки не техника — не выше 20.

summary — 1–2 предложения для менеджера на русском: почему стоит или не стоит \
обратить внимание. Конкретно, с фактами из лота (модель, сумма, срок), без общих фраз."""


def _level(score: int) -> Level:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _days_left(tender: Tender, now: datetime) -> int:
    return (tender.deadline - now).days


def _text(tender: Tender) -> str:
    return f"{tender.title} {tender.description}".lower()


def _has_keywords(tender: Tender) -> bool:
    text = _text(tender)
    patterns = criteria.EQUIPMENT_PATTERNS + criteria.BRAND_PATTERNS
    return any(re.search(p, text) for p in patterns)


def _prefilter(tender: Tender, now: datetime) -> Evaluation | None:
    """Отсев очевидно нерелевантного без вызова Claude."""
    if criteria.okpd2_matches(tender.okpd2.code) or _has_keywords(tender):
        return None
    days = _days_left(tender, now)
    return Evaluation(
        tender_id=tender.id,
        score=0,
        level="low",
        summary=f"Не наш профиль: «{tender.okpd2.name or tender.okpd2.code}», "
        "в описании нет спецтехники и наших брендов.",
        checks=Checks(
            okpd2=Check(status="bad", note=f"{tender.okpd2.code} — вне целевых групп"),
            brand=Check(status="bad", note="бренды не упоминаются"),
            price=Check(status="warn", note="не оценивалась"),
            deadline=Check(
                status="bad" if days < 0 else "ok",
                note="срок прошёл" if days < 0 else f"осталось {days} дн.",
            ),
        ),
        method="prefilter",
        evaluated_at=now,
    )


def _tender_prompt(tender: Tender, now: datetime) -> str:
    days = _days_left(tender, now)
    deadline_fact = f"срок подачи прошёл {-days} дн. назад" if days < 0 else f"осталось {days} дн."
    return f"""Сегодня: {now:%d.%m.%Y}.

Тендер {tender.number} ({tender.law}, площадка {tender.platform})
Название лота: {tender.title}
Описание: {tender.description}
ОКПД2: {tender.okpd2.code} {tender.okpd2.name} — \
{"в целевых группах" if criteria.okpd2_matches(tender.okpd2.code) else "вне целевых групп"}
Количество: {tender.quantity or "не указано"}
НМЦК: {tender.nmck:,.0f} {tender.currency}
Регион: {tender.region}{f", {tender.delivery_place}" if tender.delivery_place else ""}
Опубликован: {tender.published_at:%d.%m.%Y}
Окончание подачи заявок: {tender.deadline:%d.%m.%Y %H:%M} ({deadline_fact})
Заказчик: {tender.customer.name}{f" ({tender.customer.industry})" if tender.customer.industry else ""}"""


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if not config.ANTHROPIC_API_KEY:
        raise ScoringError("Не задан ANTHROPIC_API_KEY в файле .env")
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _ask_claude(tender: Tender, now: datetime) -> Evaluation:
    try:
        response = _get_client().beta.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _tender_prompt(tender, now)}],
            output_config={"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            # Если модель откажется отвечать, API сам повторит запрос на резервной модели.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise ScoringError("Anthropic: неверный ANTHROPIC_API_KEY") from e
    except anthropic.RateLimitError as e:
        raise ScoringError("Anthropic: превышен лимит запросов, попробуйте через минуту") from e
    except anthropic.APIStatusError as e:
        raise ScoringError(f"Anthropic: ошибка {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ScoringError("Anthropic: нет соединения с API") from e

    if response.stop_reason == "refusal":
        raise ScoringError("Claude отказался оценивать этот тендер")
    if response.stop_reason == "max_tokens":
        raise ScoringError("Ответ Claude обрезан по лимиту длины")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ScoringError("Claude вернул пустой ответ")
    answer = _LlmAnswer.model_validate_json(text)
    score = max(0, min(100, answer.score))
    return Evaluation(
        tender_id=tender.id,
        score=score,
        level=_level(score),
        summary=answer.summary.strip(),
        checks=answer.checks,
        method="llm",
        model=response.model,
        evaluated_at=now,
    )


# --- кэш ---


def _fingerprint(tender: Tender) -> str:
    raw = tender.model_dump_json() + config.ANTHROPIC_MODEL + PROMPT_VERSION
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def get_cached(tender: Tender) -> Evaluation | None:
    entry = _load_cache().get(tender.id)
    if entry and entry.get("fingerprint") == _fingerprint(tender):
        return Evaluation.model_validate(entry["evaluation"])
    return None


def evaluate(tender: Tender, force: bool = False) -> Evaluation:
    """Оценка одного тендера (из кэша, если он актуален)."""
    if not force and (cached := get_cached(tender)):
        return cached

    now = datetime.now(timezone.utc)
    result = _prefilter(tender, now) or _ask_claude(tender, now)

    cache = _load_cache()
    cache[tender.id] = {"fingerprint": _fingerprint(tender), "evaluation": result.model_dump(mode="json")}
    _save_cache(cache)
    return result


if __name__ == "__main__":
    import sys

    from .tenderplan_client import get_tenders

    force = "--force" in sys.argv
    marks = {"high": "🟢", "medium": "🟡", "low": "⚪"}
    for t in get_tenders():
        try:
            e = evaluate(t, force=force)
        except ScoringError as err:
            print(f"{t.id}  ошибка: {err}")
            continue
        print(f"{marks[e.level]} {t.id}  {e.score:>3}  [{e.method}]  {t.title[:60]}")
        print(f"      {e.summary}")
