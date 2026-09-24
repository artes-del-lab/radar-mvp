"""Черновик первого касания по тендеру.

Claude пишет письмо контактному лицу заказчика: упоминает конкретный лот и
объясняет, чем компания может быть полезна. Письмо никуда не отправляется —
это текст для менеджера, который он проверит, поправит и отправит сам.

Всё, чего система не знает (имя менеджера, телефон, сроки поставки, цены),
модель оставляет в квадратных скобках — менеджер заполнит.

Запуск из консоли:
    cd backend && python -m app.outreach TND-001
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from . import config, criteria
from .llm import JsonCache, LlmError, ask_json
from .models import Tender
from .scoring import Evaluation

PROMPT_VERSION = "2"
cache = JsonCache(config.CACHE_DIR / "drafts.json")

OutreachError = LlmError


class Draft(BaseModel):
    tender_id: str
    recipient: str = Field(description="Кому: имя и должность или «Контактному лицу»")
    subject: str
    body: str
    manager_notes: list[str] = Field(description="Что проверить перед отправкой")
    model: str
    generated_at: datetime


class _LlmDraft(BaseModel):
    subject: str
    body: str
    manager_notes: list[str]


SYSTEM_PROMPT = f"""Ты помогаешь менеджеру по продажам компании — эксклюзивного \
представителя немецкого поставщика спецтехники ({", ".join(criteria.BRANDS)}) \
на российском рынке. Нужно написать черновик первого делового письма контактному \
лицу заказчика по конкретному тендеру.

Цель письма — познакомиться и открыть диалог, а не продать в лоб: показать, что \
компания может поставить технику под этот лот, и предложить помощь — подобрать \
модель под требования, рассказать о сервисе и гарантии, ответить на вопросы по \
технике до подачи заявок.

Требования к письму:
- Деловой русский язык, обращение по имени и отчеству, если они известны.
- Письмо от лица одного менеджера, в первом лице («я», о компании — «мы»); в первом \
абзаце представиться: [Имя менеджера], [Название компании].
- Упомянуть конкретный лот: номер закупки и что закупается.
- Одна-две конкретные детали из описания лота, которые показывают, что мы вчитались \
(требования, условия эксплуатации, количество), и как наша техника под них подходит. \
Называй модели наших брендов, только если они прямо подходят под требования лота.
- 120–200 слов в теле письма. Без канцелярита, воды и превосходных степеней.
- Закончить понятным следующим шагом: короткий звонок или отправка информации по моделям.
- Подпись шаблонная: [Имя менеджера], [Должность], [Название компании], [Телефон], [Email].

Чего нельзя:
- Не выдумывай факты о компании: сроки поставки, наличие на складе, цены, скидки, \
расположение сервисных центров, опыт поставок, характер отношений с брендами \
(«напрямую», «официальный дилер»). Если это важно для письма — поставь \
заполнитель в квадратных скобках, например [срок поставки].
- Не приводи технические характеристики моделей (масса, объём ковша, мощность, \
грузоподъёмность), которых нет в описании лота: цифру по памяти легко перепутать. \
Вместо этого предложи прислать спецификацию.
- Не предлагай заказчику изменить требования закупки под нас, не проси преференций \
и не обсуждай условия в обход процедуры: по 44-ФЗ и 223-ФЗ все участники в \
равных условиях, вопросы по документации задаются через официальный запрос \
разъяснений на площадке.

subject — тема письма, до 80 символов.
body — текст письма от обращения до подписи включительно.
manager_notes — 2–4 коротких пункта для менеджера: что проверить или заполнить \
перед отправкой (например, кто на самом деле ЛПР, если контакт — специалист по \
закупкам; какие заполнители остались)."""


def _prompt(tender: Tender, evaluation: Evaluation | None) -> str:
    contact = tender.customer.contact
    contact_line = (
        f"{contact.name}{f', {contact.position}' if contact.position else ''}"
        if contact
        else "не указано в извещении"
    )
    lines = [
        f"Тендер {tender.number} ({tender.law}, площадка {tender.platform})",
        f"Лот: {tender.title}",
        f"Описание: {tender.description}",
        f"Количество: {tender.quantity or 'не указано'}",
        f"НМЦК: {f'{tender.nmck:,.0f} {tender.currency}' if tender.nmck else 'не указана'}",
        f"Место поставки: {tender.region}"
        + (f", {tender.delivery_place}" if tender.delivery_place else ""),
        f"Окончание подачи заявок: {tender.deadline:%d.%m.%Y}",
        f"Заказчик: {tender.customer.name}"
        + (f" ({tender.customer.industry})" if tender.customer.industry else ""),
        f"Контактное лицо: {contact_line}",
    ]
    if evaluation:
        lines.append(f"Оценка релевантности: {evaluation.score}/100 — {evaluation.summary}")
    return "\n".join(lines)


def _fingerprint(tender: Tender) -> str:
    return JsonCache.fingerprint(tender.model_dump_json(), PROMPT_VERSION)


def get_cached(tender: Tender) -> Draft | None:
    value = cache.get(tender.id, _fingerprint(tender))
    return Draft.model_validate(value) if value else None


def generate(tender: Tender, evaluation: Evaluation | None = None, force: bool = False) -> Draft:
    """Черновик письма по тендеру. force=True — написать новый вариант."""
    if not force and (cached := get_cached(tender)):
        return cached

    answer, model = ask_json(SYSTEM_PROMPT, _prompt(tender, evaluation), _LlmDraft, max_tokens=2000)
    contact = tender.customer.contact
    draft = Draft(
        tender_id=tender.id,
        recipient=(
            f"{contact.name}{f', {contact.position}' if contact.position else ''}"
            + (f" <{contact.email}>" if contact.email else "")
            if contact
            else "Контактному лицу заказчика"
        ),
        subject=answer.subject.strip(),
        body=answer.body.strip(),
        manager_notes=[n.strip() for n in answer.manager_notes if n.strip()],
        model=model,
        generated_at=datetime.now(timezone.utc),
    )
    cache.put(tender.id, _fingerprint(tender), draft.model_dump(mode="json"))
    return draft


if __name__ == "__main__":
    import sys

    from . import scoring
    from .tenderplan_client import get_tender

    if len(sys.argv) < 2:
        sys.exit("Укажите ID тендера: python -m app.outreach TND-001 [--force]")
    tender = get_tender(sys.argv[1])
    if tender is None:
        sys.exit(f"Тендер {sys.argv[1]} не найден")
    try:
        d = generate(tender, scoring.evaluate(tender), force="--force" in sys.argv)
    except OutreachError as err:
        sys.exit(f"Ошибка: {err}")
    print(f"Кому: {d.recipient}\nТема: {d.subject}\n\n{d.body}\n")
    print("Проверить перед отправкой:")
    for note in d.manager_notes:
        print(f"  • {note}")
