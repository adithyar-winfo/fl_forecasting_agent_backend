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


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
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

    return schemas.ChatTurnResponse(session_id=session.id, reply=assistant_text, latency_ms=latency_ms)
