from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas


def create_session(db: Session, payload: schemas.SessionCreate) -> models.ChatSession:
    item = models.ChatSession(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def create_session_with_id(db: Session, session_id: str, payload: schemas.SessionCreate) -> models.ChatSession:
    item = models.ChatSession(id=session_id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_sessions(db: Session, user_id: str | None, limit: int, offset: int) -> list[models.ChatSession]:
    stmt = select(models.ChatSession).order_by(models.ChatSession.created_at.desc())
    if user_id:
        stmt = stmt.where(models.ChatSession.user_id == user_id)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


def get_session(db: Session, session_id: str) -> models.ChatSession | None:
    return db.get(models.ChatSession, session_id)


def update_session(db: Session, item: models.ChatSession, payload: schemas.SessionUpdate) -> models.ChatSession:
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(item, key, value)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def create_message(db: Session, session_id: str, payload: schemas.MessageCreate) -> models.ChatMessage:
    item = models.ChatMessage(session_id=session_id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_messages(db: Session, session_id: str, limit: int, offset: int) -> list[models.ChatMessage]:
    stmt = (
        select(models.ChatMessage)
        .where(models.ChatMessage.session_id == session_id)
        .order_by(models.ChatMessage.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())


def create_event(db: Session, session_id: str, payload: schemas.EventCreate) -> models.ChatEvent:
    item = models.ChatEvent(session_id=session_id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_events(db: Session, session_id: str, limit: int, offset: int) -> list[models.ChatEvent]:
    stmt = (
        select(models.ChatEvent)
        .where(models.ChatEvent.session_id == session_id)
        .order_by(models.ChatEvent.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all())
