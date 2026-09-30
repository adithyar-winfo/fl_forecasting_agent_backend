import re
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud, schemas
from app.config import settings
from app.database import get_db, initialize_database


DbDep = Annotated[Session, Depends(get_db)]


def _to_float(value: str) -> float | None:
    cleaned = value.replace("**", "").replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group(0)) if match else None


def _extract_summary(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("#", "|")):
            continue
        return line
    return None


def _extract_forecast_points_from_markdown(text: str) -> list[schemas.ForecastPoint]:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(lines) < 3:
        return []

    headers = [cell.strip().lower() for cell in lines[0].strip("|").split("|")]

    date_idx = next((i for i, h in enumerate(headers) if "date" in h), None)
    demand_idx = next((i for i, h in enumerate(headers) if "predicted" in h), None)
    ci_idx = next((i for i, h in enumerate(headers) if "confidence" in h or "interval" in h), None)

    if date_idx is None or demand_idx is None:
        return []

    points: list[schemas.ForecastPoint] = []
    for row in lines[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if max(date_idx, demand_idx) >= len(cells):
            continue

        date_value = cells[date_idx].replace("**", "").strip()
        if not date_value or "total" in date_value.lower():
            continue

        demand_value = _to_float(cells[demand_idx])
        if demand_value is None:
            continue

        lower_bound = None
        upper_bound = None
        if ci_idx is not None and ci_idx < len(cells):
            ci_numbers = re.findall(r"-?\d+(?:\.\d+)?", cells[ci_idx].replace(",", ""))
            if len(ci_numbers) >= 2:
                lower_bound = float(ci_numbers[0])
                upper_bound = float(ci_numbers[1])

        points.append(
            schemas.ForecastPoint(
                date=date_value,
                predicted_demand_units=demand_value,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )
        )

    return points


def _resolve_agent_base_url() -> str | None:
    if settings.agent_base_url:
        return settings.agent_base_url.rstrip("/")

    if not settings.agent_chat_url:
        return None

    chat_url = settings.agent_chat_url.rstrip("/")
    for suffix in ("/errortriage/chat", "/chat"):
        if chat_url.endswith(suffix):
            return chat_url[: -len(suffix)]
    return chat_url


def _agent_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.agent_auth_token:
        headers[settings.agent_auth_header] = settings.agent_auth_token
    return headers


def _post_agent_json(url: str, payload: dict) -> dict:
    try:
        response = httpx.post(
            url,
            json=payload,
            headers=_agent_headers(),
            timeout=settings.agent_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent call failed: {exc}") from exc


def _resolve_refresh_url() -> str | None:
    if settings.agent_refresh:
        return settings.agent_refresh.strip()
    base_url = _resolve_agent_base_url()
    if not base_url:
        return None
    return f"{base_url}/errortriage/refresh_graph"


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/chat", status_code=status.HTTP_200_OK)
def agent_chat(payload: schemas.AgentChatRequest) -> dict:
    base_url = _resolve_agent_base_url()
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CHAT_BACKEND_AGENT_BASE_URL or CHAT_BACKEND_AGENT_CHAT_URL is not configured",
        )
    target = f"{base_url}/errortriage/chat"
    return _post_agent_json(target, payload.model_dump(exclude_none=True))


@app.post("/agent/refresh_agent", status_code=status.HTTP_200_OK)
def refresh_agent(payload: schemas.AgentRefreshRequest) -> dict:
    refresh_url = _resolve_refresh_url()
    if not refresh_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CHAT_BACKEND_AGENT_REFRESH, CHAT_BACKEND_AGENT_BASE_URL or CHAT_BACKEND_AGENT_CHAT_URL is not configured",
        )
    return _post_agent_json(refresh_url, payload.payload)


@app.post("/v1/sessions", response_model=schemas.SessionRead, status_code=status.HTTP_201_CREATED)
def create_session(payload: schemas.SessionCreate, db: DbDep) -> schemas.SessionRead:
    return crud.create_session(db, payload)


@app.get("/v1/sessions", response_model=list[schemas.SessionRead])
def list_sessions(
    db: DbDep,
    user_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[schemas.SessionRead]:
    return crud.list_sessions(db, user_id=user_id, limit=limit, offset=offset)


@app.get("/v1/sessions/{session_id}", response_model=schemas.SessionRead)
def get_session(session_id: str, db: DbDep) -> schemas.SessionRead:
    item = crud.get_session(db, session_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return item


@app.patch("/v1/sessions/{session_id}", response_model=schemas.SessionRead)
def patch_session(session_id: str, payload: schemas.SessionUpdate, db: DbDep) -> schemas.SessionRead:
    item = crud.get_session(db, session_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return crud.update_session(db, item, payload)


@app.post("/v1/sessions/{session_id}/messages", response_model=schemas.MessageRead, status_code=status.HTTP_201_CREATED)
def create_message(
    session_id: str,
    payload: schemas.MessageCreate,
    db: DbDep,
) -> schemas.MessageRead:
    if not crud.get_session(db, session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return crud.create_message(db, session_id, payload)


@app.get("/v1/sessions/{session_id}/messages", response_model=list[schemas.MessageRead])
def list_messages(
    session_id: str,
    db: DbDep,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[schemas.MessageRead]:
    if not crud.get_session(db, session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return crud.list_messages(db, session_id, limit, offset)


@app.post("/v1/sessions/{session_id}/events", response_model=schemas.EventRead, status_code=status.HTTP_201_CREATED)
def create_event(
    session_id: str,
    payload: schemas.EventCreate,
    db: DbDep,
) -> schemas.EventRead:
    if not crud.get_session(db, session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return crud.create_event(db, session_id, payload)


@app.get("/v1/sessions/{session_id}/events", response_model=list[schemas.EventRead])
def list_events(
    session_id: str,
    db: DbDep,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[schemas.EventRead]:
    if not crud.get_session(db, session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return crud.list_events(db, session_id, limit, offset)


@app.post("/chat", response_model=schemas.ChatTurnResponse, status_code=status.HTTP_201_CREATED)
def chat(payload: schemas.ChatTurnRequest, db: DbDep) -> schemas.ChatTurnResponse:
    if not settings.agent_chat_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CHAT_BACKEND_AGENT_CHAT_URL is not configured",
        )

    # Resolve or create session first so every chat turn is persisted.
    session = None
    if payload.session_id:
        session = crud.get_session(db, payload.session_id)
        if not session:
            session = crud.create_session_with_id(
                db,
                payload.session_id,
                schemas.SessionCreate(title=payload.message[:80]),
            )
    if not session:
        session = crud.create_session(
            db,
            schemas.SessionCreate(
                title=payload.message[:80],
            ),
        )

    user_message = crud.create_message(
        db,
        session.id,
        schemas.MessageCreate(
            role="user",
            message_type="chat_request",
            content=payload.message,
            request_payload={"message": payload.message},
        ),
    )

    outbound_payload = {
        "session_id": session.id,
        "message": payload.message,
    }

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.agent_auth_token:
        headers[settings.agent_auth_header] = settings.agent_auth_token

    started = perf_counter()
    try:
        response = httpx.post(
            settings.agent_chat_url,
            json=outbound_payload,
            headers=headers,
            timeout=settings.agent_timeout_seconds,
        )
        response.raise_for_status()
        agent_body = response.json()
        status_text = "ok"
    except Exception as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        crud.create_event(
            db,
            session.id,
            schemas.EventCreate(
                message_id=user_message.id,
                event_type="agent_chat_call",
                status="failed",
                details={"error": str(exc), "agent_url": settings.agent_chat_url, "latency_ms": latency_ms},
            ),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent call failed: {exc}") from exc

    latency_ms = int((perf_counter() - started) * 1000)
    assistant_text = str(
        agent_body.get("response")
        or agent_body.get("answer")
        or agent_body.get("message")
        or agent_body
    )
    summary = _extract_summary(assistant_text)
    chart_points = _extract_forecast_points_from_markdown(assistant_text)
    chart = schemas.ForecastChart(points=chart_points) if chart_points else None
    assistant_message = crud.create_message(
        db,
        session.id,
        schemas.MessageCreate(
            role="assistant",
            message_type="chat_response",
            content=assistant_text,
            response_payload=agent_body,
            latency_ms=latency_ms,
        ),
    )

    crud.create_event(
        db,
        session.id,
        schemas.EventCreate(
            message_id=assistant_message.id,
            event_type="agent_chat_call",
            status=status_text,
            details={"agent_url": settings.agent_chat_url, "http_status": response.status_code, "latency_ms": latency_ms},
        ),
    )

    payload_dict = agent_body if isinstance(agent_body, dict) else None
    return schemas.ChatTurnResponse(
        session_id=session.id,
        reply=assistant_text,
        latency_ms=latency_ms,
        summary=summary,
        chart=chart,
        agent_payload=payload_dict,
    )
