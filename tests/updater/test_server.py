"""Tests for the updater sidecar HTTP server.

These tests verify the server.py module's request handler logic
without actually running the HTTP server.
"""

from __future__ import annotations

import json
import os
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch

# Add the updater scripts directory to the path so we can import server.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "updater"))

import server as updater_server  # noqa: E402


class FakeHeaders(dict):
    """A dict subclass that mimics HTTP header lookup."""

    def get(self, key, default=None):
        return super().get(key, default)


class FakeHandler(updater_server.UpdateHandler):
    """Minimal mock of UpdateHandler that captures responses without sockets."""

    def __init__(self, path: str, method: str = "GET", headers: dict | None = None):
        # Skip BaseHTTPRequestHandler.__init__ which expects a socket
        self.path = path
        self.command = method
        self.headers = FakeHeaders(headers or {})
        self.response_code = None
        self.response_headers = {}
        self._wfile = BytesIO()
        self.wfile = self._wfile

    def send_response(self, code):
        self.response_code = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def get_response_json(self) -> dict:
        """Parse the response body as JSON."""
        return json.loads(self._wfile.getvalue().decode())


class TestAuthValidation:
    """Tests for shared secret authorization."""

    def test_rejects_missing_auth(self):
        """Should return 403 when Authorization header is missing."""
        handler = FakeHandler("/update", "POST", {})
        result = handler._check_auth()
        assert result is False
        assert handler.response_code == 403

    def test_rejects_wrong_secret(self):
        """Should return 403 when Authorization header has wrong secret."""
        handler = FakeHandler(
            "/update",
            "POST",
            {"Authorization": "Bearer wrong-secret"},
        )
        with patch.object(updater_server, "UPDATER_SECRET", "correct-secret"):
            result = handler._check_auth()
        assert result is False
        assert handler.response_code == 403

    def test_accepts_correct_secret(self):
        """Should return True when Authorization header matches."""
        handler = FakeHandler(
            "/update",
            "POST",
            {"Authorization": "Bearer test-secret"},
        )
        with patch.object(updater_server, "UPDATER_SECRET", "test-secret"):
            result = handler._check_auth()
        assert result is True


class TestPostUpdate:
    """Tests for POST /update endpoint."""

    def test_spawns_subprocess(self):
        """Should spawn update.sh when triggered with correct auth."""
        handler = FakeHandler(
            "/update",
            "POST",
            {"Authorization": "Bearer changeme"},
        )
        with (
            patch.object(updater_server, "UPDATER_SECRET", "changeme"),
            patch.object(updater_server, "_update_lock") as mock_lock,
            patch("server.threading.Thread") as mock_thread,
            patch("builtins.open", MagicMock()),
        ):
            mock_lock.acquire.return_value = True
            handler.do_POST()

        assert handler.response_code == 202
        body = handler.get_response_json()
        assert body["status"] == "started"
        mock_thread.assert_called_once()

    def test_rejects_concurrent_update(self):
        """Should return 409 when update is already running."""
        handler = FakeHandler(
            "/update",
            "POST",
            {"Authorization": "Bearer changeme"},
        )
        with (
            patch.object(updater_server, "UPDATER_SECRET", "changeme"),
            patch.object(updater_server, "_update_lock") as mock_lock,
        ):
            mock_lock.acquire.return_value = False
            handler.do_POST()

        assert handler.response_code == 409
        body = handler.get_response_json()
        assert "already in progress" in body["error"].lower()


class TestGetStatus:
    """Tests for GET /status endpoint."""

    def test_returns_idle_when_no_status_file(self):
        """Should return idle status when no update has been run."""
        handler = FakeHandler(
            "/status",
            "GET",
            {"Authorization": "Bearer changeme"},
        )
        with (
            patch.object(updater_server, "UPDATER_SECRET", "changeme"),
            patch.object(
                updater_server,
                "_read_status",
                return_value={
                    "status": "idle",
                    "step": None,
                    "error": None,
                    "started_at": None,
                },
            ),
        ):
            handler.do_GET()

        assert handler.response_code == 200
        body = handler.get_response_json()
        assert body["status"] == "idle"

    def test_returns_404_for_unknown_route(self):
        """Should return 404 for unknown routes."""
        handler = FakeHandler(
            "/unknown",
            "GET",
            {"Authorization": "Bearer changeme"},
        )
        with patch.object(updater_server, "UPDATER_SECRET", "changeme"):
            handler.do_GET()

        assert handler.response_code == 404


class TestUpdateTimeout:
    """Tests for the configurable update timeout (v1.9.13)."""

    def test_default_timeout_is_thirty_minutes(self):
        """Default must be 1800s — the old 600s killed real deploys
        mid-build (backend + frontend routinely exceed 10 min on
        homelab hardware)."""
        assert updater_server.UPDATE_TIMEOUT_SECONDS == 1800

    def test_timeout_is_env_configurable(self):
        """UPDATE_TIMEOUT_SECONDS env var overrides the default."""
        import importlib

        with patch.dict(os.environ, {"UPDATE_TIMEOUT_SECONDS": "2400"}):
            importlib.reload(updater_server)
            assert updater_server.UPDATE_TIMEOUT_SECONDS == 2400
        # Restore module state for other tests
        importlib.reload(updater_server)

    def test_run_update_uses_configured_timeout(self):
        """subprocess.run must receive the configured timeout."""
        with (
            patch.object(updater_server, "_update_lock") as mock_lock,
            patch("server.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            updater_server._run_update()

        mock_lock.release.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["timeout"] == updater_server.UPDATE_TIMEOUT_SECONDS


class TestUpdateScript:
    """Static checks on update.sh (no docker available in tests)."""

    SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "updater", "update.sh")

    def _read(self) -> str:
        with open(self.SCRIPT, encoding="utf-8") as f:
            return f.read()

    def test_backend_build_does_not_use_no_cache(self):
        """--no-cache burned ~6 min of every update window; layer cache
        must stay enabled for the backend build."""
        content = self._read()
        assert "build --no-cache" not in content

    def test_frontend_build_is_conditional_on_changes(self):
        """The frontend buildx step must be skippable when frontend/ is
        unchanged since the last successful deploy."""
        content = self._read()
        assert "FRONTEND_CHANGED" in content
        assert "git diff --name-only" in content
        assert "frontend/" in content

    def test_records_deployed_commit_on_success(self):
        """A successful run must record HEAD for next-run change detection."""
        content = self._read()
        assert ".updater_last_deployed" in content
