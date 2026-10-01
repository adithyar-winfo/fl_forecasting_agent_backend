from contextlib import asynccontextmanager
from datetime import date, timedelta
import logging
import re
from time import perf_counter, strptime
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud, schemas
from app.config import settings
from app.database import get_db, initialize_database


DbDep = Annotated[Session, Depends(get_db)]
REFRESH_GRAPH_URL = "https://adithya-ramesh-winfosolut-c67622.graysand-97adfa6b.eastus.azurecontainerapps.io/errortriage/refresh_graph"
logger = logging.getLogger(__name__)


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


def _extract_markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    lines = [line.strip() for line in text.splitlines()]
    tables: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            current.append(line)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)

    parsed: list[tuple[list[str], list[list[str]]]] = []
    for block in tables:
        if len(block) < 2:
            continue
        rows = [r.strip("|") for r in block]
        header = [c.strip() for c in rows[0].split("|")]

        data_lines = rows[1:]
        if data_lines and re.fullmatch(r"\s*:?-+:?\s*(\|\s*:?-+:?\s*)*", data_lines[0]):
            data_lines = data_lines[1:]

        data_rows = [[c.strip() for c in row.split("|")] for row in data_lines if row.strip()]
        if not header or not data_rows:
            continue
        parsed.append((header, data_rows))

    return parsed


def _extract_chart_data_from_markdown(text: str) -> dict[str, object] | None:
    tables = _extract_markdown_tables(text)
    if not tables:
        return None

    def _is_total_row(cells: list[str]) -> bool:
        joined = " ".join(cells).lower()
        return bool(re.search(r"\b(total|subtotal|grand\s+total|overall|sum)\b", joined))

    for headers, rows in tables:
        lower = [h.lower() for h in headers]
        date_idx = next((i for i, h in enumerate(lower) if "date" in h), None)
        actual_idx = next((i for i, h in enumerate(lower) if any(k in h for k in ["actual", "ordered", "baseline"])), None)
        forecast_idx = next((i for i, h in enumerate(lower) if any(k in h for k in ["forecast", "predicted", "expected"])), None)

        if date_idx is None or forecast_idx is None:
            continue

        forecast_points: list[dict[str, object]] = []
        actual_points: list[dict[str, object]] = []

        for cells in rows:
            if max(date_idx, forecast_idx) >= len(cells):
                continue
            if _is_total_row(cells):
                continue

            date_value = cells[date_idx].replace("**", "").strip()
            if not date_value:
                continue

            f_val = _to_float(cells[forecast_idx])
            if f_val is not None:
                forecast_points.append({"date": date_value, "value": f_val})

            if actual_idx is not None and actual_idx < len(cells):
                a_val = _to_float(cells[actual_idx])
                if a_val is not None:
                    actual_points.append({"date": date_value, "value": a_val})

        if forecast_points:
            series: list[dict[str, object]] = []
            if actual_points:
                series.append(
                    {
                        "name": "Actual",
                        "kind": "actual",
                        "line_style": "solid",
                        "data": actual_points,
                    }
                )
            series.append(
                {
                    "name": "Forecast",
                    "kind": "forecast",
                    "line_style": "dot",
                    "data": forecast_points,
                }
            )
            return {
                "chart_type": "line",
                "title": "Forecast Trend",
                "x_field": "date",
                "y_field": "value",
                "granularity": "day",
                "measure": headers[forecast_idx],
                "series": series,
            }

    # Labor daily fallback from summary table + date range in narrative.
    def _extract_date_range(msg: str) -> tuple[date, date] | None:
        m = re.search(
            r"spanning\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})\s+through\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
            msg,
            flags=re.IGNORECASE,
        )
        if not m:
            return None
        start_raw, end_raw = m.group(1).strip(), m.group(2).strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                s = strptime(start_raw, fmt)
                e = strptime(end_raw, fmt)
                start_dt = date(s.tm_year, s.tm_mon, s.tm_mday)
                end_dt = date(e.tm_year, e.tm_mon, e.tm_mday)
                if end_dt >= start_dt:
                    return start_dt, end_dt
            except ValueError:
                continue
        return None

    date_range = _extract_date_range(text)
    if date_range is not None:
        start_dt, end_dt = date_range
        all_dates: list[str] = []
        current = start_dt
        while current <= end_dt:
            all_dates.append(current.isoformat())
            current += timedelta(days=1)

        for headers, rows in tables:
            lower = [h.lower() for h in headers]
            process_idx = next((i for i, h in enumerate(lower) if "process" in h), None)
            avg_idx = next((i for i, h in enumerate(lower) if "avg daily labor" in h), None)
            if process_idx is None or avg_idx is None:
                continue

            process_values: list[tuple[str, float]] = []
            for cells in rows:
                if max(process_idx, avg_idx) >= len(cells):
                    continue
                process = cells[process_idx].replace("**", "").strip()
                if not process:
                    continue
                if _is_total_row(cells):
                    continue
                avg_value = _to_float(cells[avg_idx])
                if avg_value is None:
                    continue
                process_values.append((process, avg_value))

            if process_values and all_dates:
                series: list[dict[str, object]] = []
                for process, avg_value in process_values:
                    series.append(
                        {
                            "name": f"Forecast {process}",
                            "kind": "forecast",
                            "line_style": "dot",
                            "data": [{"date": d, "value": avg_value} for d in all_dates],
                        }
                    )

                total_avg = round(sum(v for _, v in process_values), 2)
                series.append(
                    {
                        "name": "Forecast Total",
                        "kind": "forecast",
                        "line_style": "solid",
                        "data": [{"date": d, "value": total_avg} for d in all_dates],
                    }
                )
                return {
                    "chart_type": "line",
                    "title": "Labor Forecast (Daily from Summary)",
                    "x_field": "date",
                    "y_field": "value",
                    "granularity": "day",
                    "measure": "Labor Hours",
                    "series": series,
                }

    # Labor/category fallback when no date-wise rows are present.
    for headers, rows in tables:
        lower = [h.lower() for h in headers]
        category_idx = next((i for i, h in enumerate(lower) if any(k in h for k in ["process", "category", "shift"])), None)
        value_idx = None
        for key in ["14-day total labor hours", "daily labor hours", "labor hours", "required daily staffing", "required headcount"]:
            value_idx = next((i for i, h in enumerate(lower) if key in h), None)
            if value_idx is not None:
                break
        if category_idx is None or value_idx is None:
            continue

        points: list[dict[str, object]] = []
        for cells in rows:
            if max(category_idx, value_idx) >= len(cells):
                continue
            if _is_total_row(cells):
                continue
            category = cells[category_idx].replace("**", "").strip()
            if not category:
                continue
            value = _to_float(cells[value_idx])
            if value is None:
                continue
            points.append({"category": category, "value": value})

        if points:
            return {
                "chart_type": "bar",
                "title": "Labor Forecast by Category",
                "x_field": "category",
                "y_field": "value",
                "granularity": "category",
                "measure": headers[value_idx],
                "series": [
                    {
                        "name": headers[value_idx],
                        "kind": "forecast",
                        "line_style": "solid",
                        "data": points,
                    }
                ],
            }

    return None


def _extract_chart_data_from_payload(agent_body: dict) -> dict[str, object] | None:
    if not isinstance(agent_body, dict):
        return None

    payload_type = str(agent_body.get("type") or "").lower()
    records = agent_body.get("datewise_table") or agent_body.get("data")
    if not isinstance(records, list) or not records:
        return None

    # Detect date-like field.
    date_key = None
    sample_keys = [str(k).lower() for k in records[0]]
    for candidate in ["date", "forecast_date", "expected_arrival_date"]:
        if candidate in sample_keys:
            date_key = candidate
            break
    if date_key is None:
        return None

    def _val(row: dict, *keys: str) -> float | None:
        lowered = {str(k).lower(): v for k, v in row.items()}
        for key in keys:
            if key in lowered:
                raw = lowered[key]
                if raw is None:
                    continue
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    num = _to_float(str(raw))
                    if num is not None:
                        return num
        return None

    # Labor: series by process on date-wise labor hours.
    if "labor" in payload_type:
        # Preferred labor-wide format: date + process columns (+ Total)
        total_points: list[dict[str, object]] = []
        for row in records:
            if not isinstance(row, dict):
                continue
            lower_row = {str(k).lower(): v for k, v in row.items()}
            date_value = lower_row.get(date_key)
            if not date_value:
                continue

            total_value = _val(row, "total", "total_labor", "total_labor_hours")
            if total_value is None:
                continue
            total_points.append({"date": str(date_value), "value": total_value})

        if total_points:
            return {
                "chart_type": "line",
                "title": "Labor Forecast (Daily Total)",
                "x_field": "date",
                "y_field": "value",
                "granularity": "day",
                "measure": "Labor Hours",
                "series": [
                    {
                        "name": "Forecast Total Labor Hours",
                        "kind": "forecast",
                        "line_style": "dot",
                        "data": total_points,
                    }
                ],
            }

        # Long labor format fallback: date + process + labor metric.
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in records:
            if not isinstance(row, dict):
                continue
            lower_row = {str(k).lower(): v for k, v in row.items()}
            date_value = lower_row.get(date_key)
            process = str(lower_row.get("process") or "Total").strip()
            if not date_value:
                continue
            value = _val(
                row,
                "forecast_labor_hours",
                "required_labor_hours",
                "adjusted_labor_hours",
                "daily_labor_hours",
                "value",
            )
            if value is None:
                continue
            grouped.setdefault(process, []).append({"date": str(date_value), "value": value})

        if grouped:
            series = []
            for process, points in grouped.items():
                series.append(
                    {
                        "name": f"Forecast {process}",
                        "kind": "forecast",
                        "line_style": "dot",
                        "data": points,
                    }
                )
            return {
                "chart_type": "line",
                "title": "Labor Forecast Trend by Process",
                "x_field": "date",
                "y_field": "value",
                "granularity": "day",
                "measure": "Labor Hours",
                "series": series,
            }

    # Generic date-wise actual vs forecast line chart.
    actual_points: list[dict[str, object]] = []
    forecast_points: list[dict[str, object]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        lower_row = {str(k).lower(): v for k, v in row.items()}
        date_value = lower_row.get(date_key)
        if not date_value:
            continue

        actual = _val(row, "actual_demand_units", "actual_ordered_qty", "actual", "ordered_qty")
        forecast = _val(
            row,
            "forecast_demand_units",
            "forecast_received_qty",
            "predicted_demand_units",
            "expected_quantity",
            "forecast",
            "value",
        )

        if actual is not None:
            actual_points.append({"date": str(date_value), "value": actual})
        if forecast is not None:
            forecast_points.append({"date": str(date_value), "value": forecast})

    if forecast_points:
        series: list[dict[str, object]] = []
        if actual_points:
            series.append(
                {
                    "name": "Actual",
                    "kind": "actual",
                    "line_style": "solid",
                    "data": actual_points,
                }
            )
        series.append(
            {
                "name": "Forecast",
                "kind": "forecast",
                "line_style": "dot",
                "data": forecast_points,
            }
        )
        return {
            "chart_type": "line",
            "title": "Forecast Trend",
            "x_field": "date",
            "y_field": "value",
            "granularity": "day",
            "measure": "Forecast",
            "series": series,
        }

    return None


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


def _post_agent_empty(url: str) -> dict:
    try:
        headers = {"accept": "*/*"}
        if settings.agent_auth_token:
            headers[settings.agent_auth_header] = settings.agent_auth_token
        response = httpx.post(
            url,
            content=b"",
            headers=headers,
            timeout=settings.agent_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent call failed: {exc}") from exc


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/chat", response_model=schemas.ChatTurnResponse, status_code=status.HTTP_201_CREATED)
def agent_chat(payload: schemas.AgentChatRequest, db: DbDep) -> schemas.ChatTurnResponse:
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

    headers = _agent_headers()

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
    chart_data = agent_body.get("chart_data") if isinstance(agent_body, dict) else None
    chart_source = "upstream"
    if not isinstance(chart_data, dict) and isinstance(agent_body, dict):
        chart_data = _extract_chart_data_from_payload(agent_body)
        chart_source = "payload" if isinstance(chart_data, dict) else chart_source
    if not isinstance(chart_data, dict):
        chart_data = _extract_chart_data_from_markdown(assistant_text)
        chart_source = "markdown" if isinstance(chart_data, dict) else "none"

    if isinstance(agent_body, dict):
        logger.info(
            "agent_chat chart source=%s upstream_chart=%s payload_keys=%s chart_type=%s series_count=%s",
            chart_source,
            isinstance(agent_body.get("chart_data"), dict),
            sorted(agent_body.keys())[:25],
            chart_data.get("chart_type") if isinstance(chart_data, dict) else None,
            len(chart_data.get("series", [])) if isinstance(chart_data, dict) else 0,
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

    payload_dict = agent_body if isinstance(agent_body, dict) else None
    return schemas.ChatTurnResponse(
        session_id=session.id,
        reply=assistant_text,
        latency_ms=latency_ms,
        summary=summary,
        chart=chart,
        chart_data=chart_data if isinstance(chart_data, dict) else None,
        agent_payload=payload_dict,
    )


@app.post("/agent/refresh_agent", status_code=status.HTTP_200_OK)
def refresh_agent() -> dict:
    return _post_agent_empty(REFRESH_GRAPH_URL)


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
