import json
import os
import threading
from unittest.mock import patch

import pytest

import webhook_server as ws

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


class ImmediateThread:
    """Stand-in for threading.Thread that runs the target synchronously,
    so tests can assert on state without waiting on a real thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def client():
    ws.app.testing = True
    return ws.app.test_client()


@pytest.fixture(autouse=True)
def reset_lock():
    # Guard against a previous failed test leaving the lock held.
    if ws._lock.locked():
        ws._lock.release()
    yield
    if ws._lock.locked():
        ws._lock.release()


@pytest.fixture(autouse=True)
def isolate_run_history(tmp_path, monkeypatch):
    # Redirect persistence at a scratch path so tests don't write into the
    # repo, and clear in-memory history so runs from other tests don't leak.
    monkeypatch.setattr(ws, "RUN_HISTORY_PATH", str(tmp_path / "run_history.json"))
    ws._runs.clear()
    yield
    ws._runs.clear()


@pytest.fixture(autouse=True)
def webhook_token(monkeypatch):
    monkeypatch.setattr(ws.tkn, "get", lambda target: "test-token" if target == "webhook" else "unused")


class TestWebhookAuth:
    def test_missing_auth_header_returns_401(self, client):
        response = client.post("/", json={"message": "hello"})

        assert response.status_code == 401

    def test_wrong_token_returns_401(self, client):
        response = client.post("/", json={"message": "hello"}, headers={"Authorization": "Bearer wrong-token"})

        assert response.status_code == 401

    def test_non_bearer_scheme_returns_401(self, client):
        response = client.post("/", json={"message": "hello"}, headers={"Authorization": "Basic test-token"})

        assert response.status_code == 401

    def test_invalid_auth_does_not_start_build(self, client):
        with patch.object(ws, "_run_build") as mock_run_build:
            client.post("/", json={"message": "hello"})

        mock_run_build.assert_not_called()

    def test_invalid_auth_does_not_acquire_lock(self, client):
        client.post("/", json={"message": "hello"})

        assert not ws._lock.locked()


class TestWebhookMessageValidation:
    def test_missing_message_returns_400(self, client):
        response = client.post("/", json={}, headers=AUTH_HEADERS)

        assert response.status_code == 400
        assert response.get_json() == {"error": "missing required 'message' field"}

    def test_empty_message_returns_400(self, client):
        response = client.post("/", json={"message": ""}, headers=AUTH_HEADERS)

        assert response.status_code == 400

    def test_no_body_returns_400(self, client):
        response = client.post("/", headers=AUTH_HEADERS)

        assert response.status_code == 400

    def test_missing_message_does_not_start_build(self, client):
        with patch.object(ws, "_run_build") as mock_run_build:
            client.post("/", json={}, headers=AUTH_HEADERS)

        mock_run_build.assert_not_called()

    def test_missing_message_does_not_acquire_lock(self, client):
        client.post("/", json={}, headers=AUTH_HEADERS)

        assert not ws._lock.locked()

    def test_missing_message_leaves_run_history_untouched(self, client):
        client.post("/", json={}, headers=AUTH_HEADERS)

        with ws._runs_lock:
            assert len(ws._runs) == 0


class TestWebhookValidMessage:
    def test_valid_message_triggers_build(self, client):
        with patch.object(threading, "Thread", ImmediateThread), \
             patch.object(ws.config_grabber, "build", return_value="ok") as mock_build, \
             patch.object(ws, "_post_to_slack"):
            response = client.post("/", json={"message": "hello"}, headers=AUTH_HEADERS)

        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "accepted"
        assert body["message"] == "hello"
        mock_build.assert_called_once_with("hello")

    def test_second_concurrent_request_is_rejected(self, client):
        ws._lock.acquire()
        try:
            response = client.post("/", json={"message": "hello"}, headers=AUTH_HEADERS)
        finally:
            ws._lock.release()

        assert response.status_code == 409


class TestRunHistoryPersistence:
    def test_build_persists_run_history_to_disk(self, client):
        with patch.object(threading, "Thread", ImmediateThread), \
             patch.object(ws.config_grabber, "build", return_value="ok"), \
             patch.object(ws, "_post_to_slack"):
            client.post("/", json={"message": "hello"}, headers=AUTH_HEADERS)

        assert os.path.exists(ws.RUN_HISTORY_PATH)
        with open(ws.RUN_HISTORY_PATH) as f:
            saved = json.load(f)
        assert len(saved) == 1
        assert saved[0]["status"] == "success"
        assert saved[0]["message"] == "hello"

    def test_load_runs_restores_history_from_disk(self):
        run = {
            "id": "abc12345",
            "message": "restored run",
            "status": "success",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00",
            "result": "ok",
            "lines": ["some log line"],
        }
        os.makedirs(os.path.dirname(ws.RUN_HISTORY_PATH), exist_ok=True)
        with open(ws.RUN_HISTORY_PATH, "w") as f:
            json.dump([run], f)

        ws._load_runs()

        assert list(ws._runs.keys()) == ["abc12345"]
        assert ws._runs["abc12345"]["status"] == "success"

    def test_load_runs_marks_interrupted_runs_from_a_prior_process(self):
        run = {
            "id": "def45678",
            "message": "cut short",
            "status": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None,
            "result": None,
            "lines": [],
        }
        os.makedirs(os.path.dirname(ws.RUN_HISTORY_PATH), exist_ok=True)
        with open(ws.RUN_HISTORY_PATH, "w") as f:
            json.dump([run], f)

        ws._load_runs()

        restored = ws._runs["def45678"]
        assert restored["status"] == "interrupted"
        assert restored["finished_at"] is not None

    def test_load_runs_does_nothing_when_no_file_exists(self):
        ws._load_runs()

        assert len(ws._runs) == 0