"""Оценка релевантности тендера.

Два этапа:
1. Быстрый предфильтр в коде (бесплатно). Если у лота чужой код ОКПД2 и в
   тексте нет ни типов техники, ни брендов — это точно не наш тендер,
   Claude не вызываем.
2. Остальное оценивает Claude: балл 0–100, короткое обоснование для менеджера
   и разбор по четырём критериям.

Результаты кэшируются в data/cache/evaluations.json: повторный показ дашборда
не стоит денег. Кэш сбрасывается сам, если изменился тендер, модель, промпт
или наступил новый день.

Запуск из консоли (оценить весь текущий список):
    cd backend && python -m app.scoring
"""

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from . import config, criteria
from .llm import JsonCache, LlmError, ask_json
from .models import Tender

PROMPT_VERSION = "2"
cache = JsonCache(config.CACHE_DIR / "evaluations.json")

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


ScoringError = LlmError


# --- то, что возвращает Claude ---


class _LlmAnswer(BaseModel):
    score: int
    summary: str
    checks: Checks


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
исключающих нашу технику (конкретный чужой бренд, отечественное шасси, локализация). \
Вся наша техника — импортная, поэтому важен национальный режим, если он указан: \
«Запрет» — иностранная продукция к закупке не допускается, участвовать нельзя \
(brand — bad, score не выше 20); «Ограничение» — заявки с иностранной продукцией \
отклоняются, если есть хотя бы одна с российской (warn, серьёзный риск); \
«Преимущество» — российской продукции даётся ценовая фора (warn, небольшой риск).
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


def _mentions_brand(tender: Tender) -> bool:
    text = _text(tender)
    return any(re.search(p, text) for p in criteria.BRAND_PATTERNS)


def _okpd2_codes(tender: Tender) -> list[str]:
    return [c for c in [tender.okpd2.code, *(i.okpd2 for i in tender.items)] if c]


def _money(value: float) -> str:
    return f"{value / 1e6:.1f} млн руб." if value >= 1e6 else f"{value:,.0f} руб.".replace(",", " ")


def _prefilter(tender: Tender, now: datetime) -> Evaluation | None:
    """Отсев без вызова Claude. None — тендер нужно оценить Claude.

    Правила по порядку: срок подачи прошёл; национальный режим «Запрет» на все
    позиции с нашими кодами; ни один код ОКПД2 не наш и бренды не упомянуты
    (кроме «торговых» кодов, если в названии техника);
    в названии стоп-слово (услуги, запчасти, навесное…) и бренды не упомянуты;
    НМЦК ниже порога.
    """
    days = _days_left(tender, now)
    brand = _mentions_brand(tender)
    codes = _okpd2_codes(tender)
    our_codes = [c for c in codes if criteria.okpd2_matches(c)]
    code_label = ", ".join(dict.fromkeys(codes)) or "код не указан"
    title = tender.title.lower()

    checks = Checks(
        okpd2=Check(
            status="ok" if our_codes else "bad",
            note=f"{our_codes[0]} — целевая группа" if our_codes else f"{code_label} — вне целевых групп",
        ),
        brand=Check(status="ok" if brand else "warn", note="упомянуты наши бренды" if brand else "бренды не указаны"),
        price=Check(status="warn", note="НМЦК не указана")
        if not tender.nmck
        else Check(status="ok", note=_money(tender.nmck)),
        deadline=Check(
            status="bad" if days < 0 else "warn" if days < 7 else "ok",
            note="срок подачи прошёл" if days < 0 else f"осталось {days} дн.",
        ),
    )

    target_items = [i for i in tender.items if criteria.okpd2_matches(i.okpd2)]
    banned = bool(target_items) and all(
        (i.national_regime or "").lower() == criteria.NATIONAL_REGIME_BAN for i in target_items
    )
    trade_code = any(c.startswith(tuple(criteria.OKPD2_TRADE)) for c in codes)
    equipment = any(re.search(p, title) for p in criteria.EQUIPMENT_PATTERNS)
    stop_word = next((m.group(0) for p in criteria.STOP_PATTERNS if (m := re.search(p, title))), None)

    if days < 0:
        score, summary = 5, f"Приём заявок закрылся {tender.deadline:%d.%m.%Y} — участвовать уже нельзя."
    elif banned:
        score, summary = 10, "Национальный режим «Запрет»: иностранная техника к закупке не допускается."
        checks.brand = Check(status="bad", note="нацрежим «Запрет» — импорт не допускается")
    elif not our_codes and not brand and not (trade_code and equipment):
        score, summary = 0, f"Не наш профиль: ОКПД2 {code_label}, наши бренды не упоминаются."
    elif stop_word and not brand:
        score, summary = 0, f"Не поставка техники: в названии «{stop_word}…», наши бренды не упоминаются."
        checks.okpd2 = Check(status="bad", note=f"«{stop_word}…» — сопутствующее, не техника")
    elif tender.nmck and tender.nmck < criteria.MIN_NMCK_RUB:
        score, summary = 5, f"НМЦК {_money(tender.nmck)} — ниже порога {_money(criteria.MIN_NMCK_RUB)} для новой техники."
        checks.price = Check(status="bad", note=f"{_money(tender.nmck)} — ниже порога")
    else:
        return None

    return Evaluation(
        tender_id=tender.id,
        score=score,
        level="low",
        summary=summary,
        checks=checks,
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
НМЦК: {f'{tender.nmck:,.0f} {tender.currency}' if tender.nmck else 'не указана'}
Регион: {tender.region}{f", {tender.delivery_place}" if tender.delivery_place else ""}
Опубликован: {tender.published_at:%d.%m.%Y}
Окончание подачи заявок: {tender.deadline:%d.%m.%Y %H:%M} ({deadline_fact})
Заказчик: {tender.customer.name}{f" ({tender.customer.industry})" if tender.customer.industry else ""}"""


def _ask_claude(tender: Tender, now: datetime) -> Evaluation:
    answer, model = ask_json(SYSTEM_PROMPT, _tender_prompt(tender, now), _LlmAnswer, max_tokens=1200)
    score = max(0, min(100, answer.score))
    return Evaluation(
        tender_id=tender.id,
        score=score,
        level=_level(score),
        summary=answer.summary.strip(),
        checks=answer.checks,
        method="llm",
        model=model,
        evaluated_at=now,
    )


# --- кэш ---


def _fingerprint(tender: Tender) -> str:
    # Дата входит в отпечаток: «осталось N дней» должно пересчитываться каждый день.
    today = datetime.now(timezone.utc).date().isoformat()
    return JsonCache.fingerprint(tender.model_dump_json(), PROMPT_VERSION, today)


def get_cached(tender: Tender) -> Evaluation | None:
    value = cache.get(tender.id, _fingerprint(tender))
    return Evaluation.model_validate(value) if value else None


def quick(tender: Tender) -> Evaluation | None:
    """Оценка без вызова Claude: из кэша или по предфильтру. Иначе None."""
    if cached := get_cached(tender):
        return cached
    now = datetime.now(timezone.utc)
    if result := _prefilter(tender, now):
        cache.put(tender.id, _fingerprint(tender), result.model_dump(mode="json"))
    return result


def evaluate(tender: Tender, force: bool = False) -> Evaluation:
    """Оценка одного тендера (из кэша, если он актуален)."""
    if not force and (cached := get_cached(tender)):
        return cached

    now = datetime.now(timezone.utc)
    result = _prefilter(tender, now) or _ask_claude(tender, now)
    cache.put(tender.id, _fingerprint(tender), result.model_dump(mode="json"))
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
