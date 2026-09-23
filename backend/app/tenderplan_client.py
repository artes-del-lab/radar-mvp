"""Клиент Tenderplan API с мок-режимом.

Как это работает у Тендерплана: в личном кабинете вы создаёте «ключ» —
сохранённый поиск (слова, коды ОКПД2, регионы). Тендерплан сам находит
подходящие тендеры, а API отдаёт найденное по ключу.

- Нет TENDERPLAN_API_KEY → тендеры берутся из data/mock_tenders.json.
- Ключ есть → запрос к https://tenderplan.ru/api/tenders/v2/getlist.

Описание API: https://tenderplan.ru/api/doc/
Структура ответа взята из примеров в их OpenAPI-спецификации. Полная схема
там не описана, поэтому разбор ответа (_from_tenderplan) нужно будет сверить
на первом реальном запросе.
"""

import json
import logging
from datetime import datetime, timezone

import httpx

from . import config
from .models import ContactPerson, Customer, Okpd2, Tender

log = logging.getLogger(__name__)

BASE_URL = "https://tenderplan.ru/api"
TIMEOUT = 20.0

# Коды регионов Тендерплана (коды субъектов РФ). Список неполный — дополнится
# по мере встречаемости; неизвестный код покажется как «Регион 42».
REGIONS = {
    14: "Республика Саха (Якутия)",
    23: "Краснодарский край",
    24: "Красноярский край",
    38: "Иркутская область",
    42: "Кемеровская область — Кузбасс",
    47: "Ленинградская область",
    50: "Московская область",
    51: "Мурманская область",
    52: "Нижегородская область",
    63: "Самарская область",
    66: "Свердловская область",
    71: "Тульская область",
    76: "Ярославская область",
    77: "г. Москва",
    78: "г. Санкт-Петербург",
    86: "Ханты-Мансийский автономный округ — Югра",
}


class TenderplanError(Exception):
    pass


def get_tenders() -> list[Tender]:
    """Список тендеров из текущего источника (mock или Тендерплан)."""
    if config.USE_MOCK_TENDERS:
        return _load_mock()
    return _fetch_from_tenderplan()


def get_tender(tender_id: str) -> Tender | None:
    """Один тендер по ID."""
    if config.USE_MOCK_TENDERS:
        return next((t for t in _load_mock() if t.id == tender_id), None)
    data = _request("/tenders/get", {"id": tender_id})
    return _from_tenderplan(data) if data else None


def source_name() -> str:
    return "mock" if config.USE_MOCK_TENDERS else "tenderplan"


# --- mock ---


def _load_mock() -> list[Tender]:
    with open(config.MOCK_TENDERS_FILE, encoding="utf-8") as f:
        return [Tender(**item) for item in json.load(f)]


# --- Tenderplan ---


def _request(path: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {config.TENDERPLAN_API_KEY}"}
    try:
        resp = httpx.get(BASE_URL + path, params=params, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise TenderplanError(f"Тендерплан недоступен: {e}") from e
    if resp.status_code == 401:
        raise TenderplanError("Тендерплан: ключ API недействителен (401)")
    if resp.status_code == 403:
        raise TenderplanError("Тендерплан: у ключа нет прав на этот метод (403)")
    if resp.status_code == 429:
        raise TenderplanError("Тендерплан: превышен лимит запросов, подождите минуту (429)")
    resp.raise_for_status()
    return resp.json()


def _fetch_from_tenderplan() -> list[Tender]:
    params = {
        "type": 0,  # выборка по ключу
        "page": 0,
        "publicationDateTime": -1,  # сначала свежие
        # только тендеры с ещё открытым приёмом заявок
        "fromSubmissionCloseDateTime": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    if config.TENDERPLAN_SEARCH_KEY_ID:
        params["id"] = config.TENDERPLAN_SEARCH_KEY_ID
    data = _request("/tenders/v2/getlist", params)

    tenders = []
    for item in data.get("tenders", []):
        try:
            tenders.append(_from_tenderplan(item))
        except Exception:
            # Один кривой тендер не должен ломать весь список.
            log.exception("Не удалось разобрать тендер %s", item.get("_id"))
    return tenders


def _ms_to_dt(value) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _from_tenderplan(item: dict) -> Tender:
    """Перевод тендера из формата Тендерплана в формат РАДАРа."""
    customers = item.get("customers") or [{}]
    first_customer = customers[0]
    region_code = item.get("region") or first_customer.get("region")
    platform = item.get("platform") or {}
    details = _parse_details(item.get("json"))

    okpd2_list = item.get("okpd2") or []
    okpd2_code = okpd2_list[0] if okpd2_list and isinstance(okpd2_list[0], str) else ""

    return Tender(
        id=item["_id"],
        source="tenderplan",
        platform=platform.get("name") or "—",
        law=_law_from_type(item.get("type")),
        number=str(item.get("number") or ""),
        # Формат ссылки на карточку — предположение, проверить на реальном тендере.
        url=f"https://tenderplan.ru/app?tender={item['_id']}",
        title=item.get("orderName") or "Без названия",
        description=item.get("tenderSearch") or item.get("orderName") or "",
        okpd2=Okpd2(code=okpd2_code, name=""),
        nmck=float(item.get("maxPrice") or 0),
        currency=item.get("currency") or "RUB",
        region=REGIONS.get(region_code, f"Регион {region_code}" if region_code else "—"),
        published_at=_ms_to_dt(item.get("publicationDateTime")),
        deadline=_ms_to_dt(item.get("submissionCloseDateTime")),
        customer=Customer(
            name=first_customer.get("name") or "—",
            contact=details.get("contact"),
        ),
    )


def _law_from_type(tender_type) -> str:
    # Точные коды типов закупок у Тендерплана пока не известны —
    # уточним по справочнику https://tenderplan.ru/api/tools/... после получения ключа.
    return f"тип {tender_type}" if tender_type is not None else "—"


def _parse_details(raw: str | None) -> dict:
    """Достаёт контактное лицо из поля json полной модели тендера.

    Это вложенное дерево полей вида {"fn": "FIO", "fv": "..."}.
    """
    if not raw:
        return {}
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    found = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("fn") in ("FIO", "Email") and isinstance(node.get("fv"), str):
                found.setdefault(node["fn"], node["fv"].strip())
            for value in node.values():
                walk(value)

    walk(tree)
    if "FIO" not in found:
        return {}
    return {"contact": ContactPerson(name=found["FIO"], email=found.get("Email"))}
