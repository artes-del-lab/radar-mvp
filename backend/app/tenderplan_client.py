"""Клиент Tenderplan API с мок-режимом.

Как это работает у Тендерплана: в личном кабинете вы создаёте «ключ» —
сохранённый поиск (слова, коды ОКПД2, регионы). Тендерплан сам находит
подходящие тендеры, а API отдаёт найденное по ключу.

- Нет TENDERPLAN_API_KEY → тендеры берутся из data/mock_tenders.json.
- Ключ есть → запрос к https://tenderplan.ru/api/tenders/v2/getlist.

Описание API: https://tenderplan.ru/api/doc/

Проверено на реальном ключе пользователя (24.09.2026):
- getlist отдаёт по 50 коротких моделей: без ОКПД2, описания и контактов.
  Их берём из полной карточки tenders/get (getmanydata тоже отдаёт короткие).
- type — код площадки, а не тип закупки; расшифровка — data/tenderplan_dicts.json.
- В полной карточке: okpd2 (бывает null — тогда код КТРУ из таблицы объектов),
  href — ссылка на извещение, platform — ЭТП, json — дерево полей с таблицей
  объектов (с национальным режимом), контактным лицом и местом поставки.
- maxPrice бывает null или 0 — НМЦК не указана. currency — в нижнем регистре.
- Нужен ключ пользователя с правами relations:read и keys:read: сервисный
  ключ приложения получает 403.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from . import config
from .models import ContactPerson, Customer, Okpd2, Tender

log = logging.getLogger(__name__)

BASE_URL = "https://tenderplan.ru/api"
TIMEOUT = 20.0
PAGE_SIZE = 50  # столько тендеров отдаёт getlist за страницу
LIST_PAGES = 4  # не больше 200 свежих тендеров за раз
MAX_OBJECTS = 10  # позиций лота в описании для Claude
FULL_CACHE_FILE = config.CACHE_DIR / "tenderplan_tenders.json"
_cache_lock = threading.Lock()

# Справочники Тендерплана (/api/tools/types/list и /api/tools/regions/list):
# код площадки → название и закон, код региона → название. Коды регионов
# совпадают с кодами субъектов РФ.
_DICTS = json.loads((config.DATA_DIR / "tenderplan_dicts.json").read_text(encoding="utf-8"))
PLATFORMS: dict[str, dict] = _DICTS["platforms"]
REGIONS: dict[str, str] = _DICTS["regions"]


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
    data = _get_full(tender_id)
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
        # Проверено на реальном ключе: сервисный ключ приложения (права только
        # resources:external) получает 403 на списки тендеров и ключей поиска.
        raise TenderplanError(
            "Тендерплан: у ключа нет прав на этот метод (403). Нужен ключ пользователя "
            "(Personal Access Token из личного кабинета) с правами relations:read и keys:read, "
            "а не сервисный ключ приложения"
        )
    if resp.status_code == 429:
        raise TenderplanError("Тендерплан: превышен лимит запросов, подождите минуту (429)")
    resp.raise_for_status()
    return resp.json()


def _fetch_from_tenderplan() -> list[Tender]:
    # В списке приходят короткие модели: без ОКПД2, описания и контактов.
    # Поэтому по каждому тендеру дополнительно берём полную карточку.
    short_items = []
    for page in range(LIST_PAGES):
        params = {
            "type": 0,  # выборка по ключу
            "page": page,
            "publicationDateTime": -1,  # сначала свежие
            # только тендеры с ещё открытым приёмом заявок
            "fromSubmissionCloseDateTime": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        if config.TENDERPLAN_SEARCH_KEY_ID:
            params["id"] = config.TENDERPLAN_SEARCH_KEY_ID
        batch = _request("/tenders/v2/getlist", params).get("tenders", [])
        short_items += batch
        if len(batch) < PAGE_SIZE:
            break

    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(_full_or_short, short_items))

    tenders = []
    for item in items:
        try:
            tenders.append(_from_tenderplan(item))
        except Exception:
            # Один кривой тендер не должен ломать весь список.
            log.exception("Не удалось разобрать тендер %s", item.get("_id"))
    return tenders


def _full_or_short(short: dict) -> dict:
    try:
        return _get_full(short["_id"])
    except TenderplanError:
        log.warning("Нет полной карточки тендера %s, беру короткую", short.get("_id"))
        return short


def _get_full(tender_id: str) -> dict | None:
    """Полная карточка тендера (кэш на день: карточки меняются редко)."""
    today = datetime.now(timezone.utc).date().isoformat()
    with _cache_lock:
        cached = _load_full_cache().get(tender_id)
    if cached and cached["date"] == today:
        return cached["item"]
    item = _request("/tenders/get", {"id": tender_id})
    if item:
        with _cache_lock:
            data = _load_full_cache()
            data[tender_id] = {"date": today, "item": item}
            FULL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            FULL_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return item


def _load_full_cache() -> dict:
    try:
        return json.loads(FULL_CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _ms_to_dt(value) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _from_tenderplan(item: dict) -> Tender:
    """Перевод тендера из формата Тендерплана в формат РАДАРа.

    Работает и с полной карточкой (tenders/get), и с короткой моделью из списка.
    """
    customers = item.get("customers") or [{}]
    first_customer = customers[0]
    region_code = item.get("region") or first_customer.get("region")
    # Поле type — код площадки по справочнику: 0 — ЕИС 223-ФЗ, 1 — ЕИС 44-ФЗ,
    # 2 — B2B-Center… В полной карточке ещё есть platform — конкретная ЭТП.
    platform_ref = PLATFORMS.get(str(item.get("type")), {})
    platform_name = (item.get("platform") or {}).get("name") or platform_ref.get("name")
    details = _parse_details(item.get("json"))
    objects = details.get("objects", [])

    okpd2_code = next((c for c in item.get("okpd2") or [] if isinstance(c, str)), "")
    if not okpd2_code and objects:
        # В 44-ФЗ вместо ОКПД2 бывает код КТРУ: 28.92.20.000-00000020 → 28.92.20.000
        okpd2_code = objects[0]["code"].split("-")[0]

    return Tender(
        id=item["_id"],
        source="tenderplan",
        platform=platform_name or f"площадка {item.get('type')}",
        law=platform_ref.get("law") or "—",
        number=str(item.get("number") or ""),
        # href — ссылка на извещение в ЕИС или на площадке (есть в полной карточке).
        url=item.get("href") or None,
        title=item.get("orderName") or "Без названия",
        description=_description(item, objects),
        okpd2=Okpd2(code=okpd2_code, name=objects[0]["name"] if objects else ""),
        quantity=_quantity(objects[0]["quantity"]) if len(objects) == 1 else None,
        nmck=float(item["maxPrice"]) if item.get("maxPrice") else None,
        currency=(item.get("currency") or "RUB").upper(),
        region=REGIONS.get(str(region_code), f"Регион {region_code}") if region_code is not None else "—",
        delivery_place=details.get("delivery_place"),
        published_at=_ms_to_dt(item.get("publicationDateTime")),
        deadline=_ms_to_dt(item.get("submissionCloseDateTime")),
        customer=Customer(
            name=first_customer.get("name") or "—",
            contact=details.get("contact"),
        ),
    )


def _description(item: dict, objects: list[dict]) -> str:
    """Описание лота из таблицы объектов закупки: позиции, количество, нацрежим."""
    if not objects:
        return item.get("tenderSearch") or item.get("orderName") or ""
    lines = []
    for obj in objects[:MAX_OBJECTS]:
        line = obj["name"] + (f" — {obj['quantity']}" if obj["quantity"] else "")
        if obj["code"]:
            line += f" (код {obj['code']})"
        if obj["regime"]:
            line += f"; национальный режим: {obj['regime']}"
        lines.append(line)
    if len(objects) > MAX_OBJECTS:
        lines.append(f"…и ещё позиций: {len(objects) - MAX_OBJECTS}")
    return "Объекты закупки: " + "; ".join(lines)


def _quantity(raw: str) -> int | None:
    # «1 шт», «2.0 шт»; для «Условная единица» и подобного количества нет.
    try:
        return int(float(raw.split()[0].replace(",", ".")))
    except (ValueError, IndexError):
        return None


def _parse_details(raw: str | None) -> dict:
    """Разбор поля json полной карточки тендера.

    Это дерево полей вида {"fn": "FIO", "fv": "..."}. Таблица объектов закупки —
    поле Objects: {"th": заголовок, "tb": {"0": {"0": {"fn": "Name", ...}, ...}}}.
    """
    if not raw:
        return {}
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    found = {}
    objects = []

    def walk(node):
        if not isinstance(node, dict):
            return
        fn, fv = node.get("fn"), node.get("fv")
        if fn == "Objects" and isinstance(fv, dict):
            for row in (fv.get("tb") or {}).values():
                cells = {c.get("fn"): c.get("fv") for c in row.values() if isinstance(c, dict)}
                if cells.get("Name"):
                    objects.append({
                        "name": str(cells["Name"]).strip(),
                        "code": str(cells.get("Code") or "").strip(),
                        "quantity": str(cells.get("Quantity") or "").strip(),
                        "regime": str(cells.get("NationalRegime") or "").strip(),
                    })
            return
        if fn in ("FIO", "Email", "deliveryPlace") and isinstance(fv, str):
            found.setdefault(fn, fv.strip())
        for value in node.values():
            walk(value)

    walk(tree)
    result = {"objects": objects}
    if found.get("deliveryPlace"):
        # Бывает «Иркутская область, Адрес: Не заполнено».
        place = found["deliveryPlace"].replace("Адрес:", "").replace("Не заполнено", "").strip(" ,")
        result["delivery_place"] = place or None
    if found.get("FIO"):
        result["contact"] = ContactPerson(name=found["FIO"], email=found.get("Email"))
    return result
