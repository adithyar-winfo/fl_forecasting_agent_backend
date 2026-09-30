# Chat Backend (FastAPI)

Standalone backend for storing chatbot sessions and responses for forecasting/labor use cases.

## What it stores
- Chat sessions (`chat_sessions`)
- Chat messages (`chat_messages`)
- Optional processing events (`chat_events`) for model/tool traces

## Quick start

```bash
cd chat_backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8010
```

Open docs: http://127.0.0.1:8010/docs

## Configuration
Set environment variables (optional):

- `CHAT_BACKEND_DATABASE_URL` (default: existing `mcp_servers/db_mcp/config.py` Postgres; fallback `sqlite:///./chat_backend.db`)
- `CHAT_BACKEND_APP_NAME` (default: `FL Agent Chat Backend`)
- `CHAT_BACKEND_AGENT_CHAT_URL` (required for `POST /chat`)
- `CHAT_BACKEND_AGENT_AUTH_HEADER` (default: `Authorization`)
- `CHAT_BACKEND_AGENT_AUTH_TOKEN` (optional)
- `CHAT_BACKEND_AGENT_TIMEOUT_SECONDS` (default: `120`)

Example for PostgreSQL:

```bash
set CHAT_BACKEND_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/fl_agent
```

## API summary
- `POST /v1/sessions` create a session
- `GET /v1/sessions` list sessions
- `GET /v1/sessions/{session_id}` get one session
- `PATCH /v1/sessions/{session_id}` update title/status/metadata
- `POST /v1/sessions/{session_id}/messages` add a message
- `GET /v1/sessions/{session_id}/messages` list messages in a session
- `POST /v1/sessions/{session_id}/events` add a processing event
- `GET /v1/sessions/{session_id}/events` list processing events
- `POST /chat` single endpoint: persists session + user payload, calls cloud agent, persists response/event

## Run tests

```bash
cd chat_backend
pytest -q
```
