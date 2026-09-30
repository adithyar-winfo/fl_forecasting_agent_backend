import os

os.environ["CHAT_BACKEND_DATABASE_URL"] = "sqlite:///./test_chat_backend_chat.db"
os.environ["CHAT_BACKEND_AGENT_CHAT_URL"] = "https://agent.example.com/chat"
os.environ["CHAT_BACKEND_AGENT_TIMEOUT_SECONDS"] = "5"

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app
from app.config import settings


def test_agent_chat_persists_and_returns_agent_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_chat_url", "https://agent.example.com/chat")

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"response": "Labor need is 24 heads for DC01 tomorrow."}

    def _fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)

    Base.metadata.create_all(bind=engine)
    with TestClient(app) as client:
        resp = client.post(
            "/agent/chat",
            json={
                "message": "What labor is needed for DC01 tomorrow?",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "session_id" in body
        assert body["reply"].startswith("Labor need")

        msg_resp = client.get(f"/v1/sessions/{body['session_id']}/messages")
        assert msg_resp.status_code == 200
        msgs = msg_resp.json()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"


def test_agent_chat_uses_provided_session_id(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_chat_url", "https://agent.example.com/chat")

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"response": "ok"}

    def _fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)

    Base.metadata.create_all(bind=engine)
    with TestClient(app) as client:
        resp = client.post(
            "/agent/chat",
            json={"session_id": "sess-1", "message": "hello"},
        )
        assert resp.status_code == 201
        assert resp.json()["session_id"] == "sess-1"


def test_agent_refresh_proxies_to_fixed_refresh_graph_url(monkeypatch) -> None:
    called = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"status": "refreshed"}

    def _fake_post(*args, **kwargs):
        called["url"] = args[0]
        called["json"] = kwargs.get("json")
        called["headers"] = kwargs.get("headers")
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)

    with TestClient(app) as client:
        resp = client.post("/agent/refresh_agent")
        assert resp.status_code == 200
        assert resp.json() == {"status": "refreshed"}
        assert called["url"] == (
            "https://adithya-ramesh-winfosolut-c67622.graysand-97adfa6b.eastus.azurecontainerapps.io"
            "/errortriage/refresh_graph"
        )
        assert called["json"] == {}
