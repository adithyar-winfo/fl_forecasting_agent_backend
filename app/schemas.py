from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    user_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=255)
    channel: str | None = Field(default=None, max_length=64)
    metadata_json: dict[str, Any] | None = None


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    status: Literal["active", "archived", "closed"] | None = None
    metadata_json: dict[str, Any] | None = None


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str | None
    title: str | None
    status: str
    channel: str | None
    metadata_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    message_type: str = Field(default="text", max_length=64)
    content: str = Field(min_length=1)
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    message_type: str
    content: str
    request_payload: dict[str, Any] | None
    response_payload: dict[str, Any] | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    created_at: datetime


class EventCreate(BaseModel):
    message_id: str | None = None
    event_type: str = Field(max_length=64)
    status: str = Field(default="ok", max_length=32)
    details: dict[str, Any] | None = None


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    message_id: str | None
    event_type: str
    status: str
    details: dict[str, Any] | None
    created_at: datetime


class ChatTurnRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1)


class AgentChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1)


class AgentRefreshRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class ForecastPoint(BaseModel):
    date: str
    predicted_demand_units: float
    lower_bound: float | None = None
    upper_bound: float | None = None


class ForecastChart(BaseModel):
    chart_type: Literal["line"] = "line"
    x_field: str = "date"
    y_field: str = "predicted_demand_units"
    points: list[ForecastPoint]


class ChatTurnResponse(BaseModel):
    session_id: str
    reply: str
    latency_ms: int
    summary: str | None = None
    chart: ForecastChart | None = None
    agent_payload: dict[str, Any] | None = None
