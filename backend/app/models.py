"""Единый формат тендера внутри РАДАРа.

Любой источник (mock-файл, Tenderplan API, в будущем ЕИС) приводится к этой
модели. Остальная система — оценка, черновики, дашборд — работает только с ней
и не знает, откуда пришли данные.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class Okpd2(BaseModel):
    code: str = Field(description="Код ОКПД2, например 28.92.21")
    name: str


class ContactPerson(BaseModel):
    """Контактное лицо из извещения о закупке."""

    name: str
    position: str | None = None
    email: str | None = None


class Customer(BaseModel):
    name: str
    inn: str | None = None
    industry: str | None = Field(default=None, description="Отрасль заказчика")
    contact: ContactPerson | None = None


class LotItem(BaseModel):
    """Позиция из таблицы объектов закупки."""

    name: str
    okpd2: str = Field(default="", description="Код ОКПД2 (из кода КТРУ, если указан он)")
    quantity: str | None = None
    national_regime: str | None = Field(default=None, description="Запрет / Ограничение / Преимущество")


class Tender(BaseModel):
    id: str = Field(description="Внутренний ID в РАДАРе")
    source: str = Field(description="Откуда получен: mock / tenderplan")
    platform: str = Field(description="Электронная площадка")
    law: str = Field(description="44-ФЗ, 223-ФЗ или коммерческая закупка")
    number: str = Field(description="Номер закупки на площадке")
    url: str | None = None

    title: str = Field(description="Название лота")
    description: str
    okpd2: Okpd2
    quantity: int | None = Field(default=None, description="Количество единиц")
    items: list[LotItem] = Field(default_factory=list, description="Позиции лота, если известны")

    nmck: float | None = Field(default=None, description="НМЦК, руб.; None — не указана")
    currency: str = "RUB"
    region: str
    delivery_place: str | None = None

    published_at: datetime
    deadline: datetime = Field(description="Окончание подачи заявок")

    customer: Customer
