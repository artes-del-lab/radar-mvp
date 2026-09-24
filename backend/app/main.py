"""Веб-сервер РАДАРа: API для дашборда и сам дашборд (папка frontend/).

Запуск:
    cd backend && uvicorn app.main:app --reload
Дашборд: http://localhost:8000
"""

import base64
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, outreach, scoring
from .llm import LlmError
from .models import Tender
from .tenderplan_client import TenderplanError, get_tender, get_tenders, source_name

FRONTEND_DIR = config.ROOT_DIR / "frontend"

app = FastAPI(title="РАДАР", docs_url="/api/docs")


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Вход по паролю, если задан RADAR_PASSWORD: стандартное окно браузера.

    Браузер запоминает пароль и сам отправляет его со всеми запросами дашборда.
    """
    if config.RADAR_PASSWORD and not _password_ok(request.headers.get("authorization", "")):
        return Response(
            "Нужен логин и пароль РАДАРа",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="RADAR", charset="UTF-8"'},
            media_type="text/plain; charset=utf-8",
        )
    return await call_next(request)


def _password_ok(header: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    # compare_digest — сравнение за постоянное время, чтобы пароль не подбирали по таймингу.
    return secrets.compare_digest(user.encode(), config.RADAR_USER.encode()) and secrets.compare_digest(
        password.encode(), config.RADAR_PASSWORD.encode()
    )


class TenderItem(BaseModel):
    tender: Tender
    evaluation: scoring.Evaluation | None
    has_draft: bool


class ForceBody(BaseModel):
    force: bool = False


def _load_tender(tender_id: str) -> Tender:
    try:
        tender = get_tender(tender_id)
    except TenderplanError as e:
        raise HTTPException(502, str(e)) from e
    if tender is None:
        raise HTTPException(404, "Тендер не найден")
    return tender


@app.get("/api/status")
def status():
    return {
        "source": source_name(),
        "anthropic_configured": config.ANTHROPIC_CONFIGURED,
        "model": config.ANTHROPIC_MODEL,
        "auto_check": config.RADAR_AUTO_CHECK,
        # Список тендеров берётся из источника при каждом открытии дашборда.
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/tenders", response_model=list[TenderItem])
def list_tenders():
    """Все тендеры с уже готовыми оценками. Claude здесь не вызывается."""
    try:
        tenders = get_tenders()
    except TenderplanError as e:
        raise HTTPException(502, str(e)) from e
    return [
        TenderItem(
            tender=t,
            evaluation=scoring.quick(t),
            has_draft=outreach.get_cached(t) is not None,
        )
        for t in tenders
    ]


@app.post("/api/tenders/{tender_id}/evaluate", response_model=scoring.Evaluation)
def evaluate(tender_id: str, body: ForceBody | None = None):
    tender = _load_tender(tender_id)
    try:
        return scoring.evaluate(tender, force=bool(body and body.force))
    except LlmError as e:
        raise HTTPException(503, str(e)) from e


@app.get("/api/tenders/{tender_id}/draft", response_model=outreach.Draft)
def get_draft(tender_id: str):
    draft = outreach.get_cached(_load_tender(tender_id))
    if draft is None:
        raise HTTPException(404, "Черновик ещё не создан")
    return draft


@app.post("/api/tenders/{tender_id}/draft", response_model=outreach.Draft)
def create_draft(tender_id: str, body: ForceBody | None = None):
    tender = _load_tender(tender_id)
    try:
        return outreach.generate(tender, scoring.quick(tender), force=bool(body and body.force))
    except LlmError as e:
        raise HTTPException(503, str(e)) from e


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
