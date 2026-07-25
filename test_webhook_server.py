import threading
from unittest.mock import patch

import pytest

import webhook_server as ws


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


class TestWebhookMessageValidation:
    def test_missing_message_returns_400(self, client):
        response = client.get("/")

        assert response.status_code == 400
        assert response.get_json() == {"error": "missing required 'message' parameter"}

    def test_empty_message_returns_400(self, client):
        response = client.get("/?message=")

        assert response.status_code == 400

    def test_missing_message_does_not_start_build(self, client):
        with patch.object(ws, "_run_build") as mock_run_build:
            client.get("/")

        mock_run_build.assert_not_called()

    def test_missing_message_does_not_acquire_lock(self, client):
        client.get("/")

        assert not ws._lock.locked()

    def test_missing_message_leaves_run_history_untouched(self, client):
        client.get("/")

        with ws._runs_lock:
            assert len(ws._runs) == 0


class TestWebhookValidMessage:
    def test_valid_message_triggers_build(self, client):
        with patch.object(threading, "Thread", ImmediateThread), \
             patch.object(ws.config_grabber, "build", return_value="ok") as mock_build, \
             patch.object(ws, "_post_to_slack"):
            response = client.get("/?message=hello")

        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "accepted"
        assert body["message"] == "hello"
        mock_build.assert_called_once_with("hello")

    def test_second_concurrent_request_is_rejected(self, client):
        ws._lock.acquire()
        try:
            response = client.get("/?message=hello")
        finally:
            ws._lock.release()

        assert response.status_code == 409