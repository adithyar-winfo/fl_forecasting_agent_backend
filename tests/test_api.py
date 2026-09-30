import os

os.environ["CHAT_BACKEND_DATABASE_URL"] = "sqlite:///./test_chat_backend.db"

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

def test_session_message_event_flow() -> None:
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as client:
        create_session_resp = client.post(
            "/v1/sessions",
            json={
                "user_id": "user-1",
                "title": "Forecasting Questions",
                "channel": "web",
                "metadata_json": {"region": "NA"},
            },
        )
        assert create_session_resp.status_code == 201
        session = create_session_resp.json()
        session_id = session["id"]

        create_message_resp = client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "role": "user",
                "message_type": "forecast_query",
                "content": "What is labor need for DC01 tomorrow?",
                "request_payload": {"dc_id": "DC01", "horizon": 1},
            },
        )
        assert create_message_resp.status_code == 201

        create_event_resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "event_type": "forecast_labor",
                "status": "ok",
                "details": {"source": "mcp.forecast_labor"},
            },
        )
        assert create_event_resp.status_code == 201

        list_messages_resp = client.get(f"/v1/sessions/{session_id}/messages")
        assert list_messages_resp.status_code == 200
        messages = list_messages_resp.json()
        assert len(messages) == 1
        assert messages[0]["message_type"] == "forecast_query"

        list_events_resp = client.get(f"/v1/sessions/{session_id}/events")
        assert list_events_resp.status_code == 200
        events = list_events_resp.json()
        assert len(events) == 1
        assert events[0]["event_type"] == "forecast_labor"
