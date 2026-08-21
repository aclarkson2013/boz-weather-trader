"""Minimal HTTP server for the updater sidecar.

Listens on port 9999, provides:
- POST /update  — trigger git pull + docker compose rebuild + restart
- GET  /status  — return current update status as JSON

Secured via shared secret in the Authorization header.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("updater")

UPDATER_SECRET = os.environ.get("UPDATER_SECRET", "changeme")
STATUS_FILE = "/tmp/update_status.json"
PORT = 9999

# Update timeout. History:
#   600s  — killed real updates mid-build (v1.9.12 deploy, 2026-08-06).
#   1800s — still too short for a cold frontend build: the v1.9.14 deploy
#           (2026-08-21) spent ~28 min in `docker buildx build` for the
#           frontend and was killed before it could restart anything.
# The homelab VM has 4 cores and 4 GB RAM, so a cold Next.js production build
# inside BuildKit is slow by nature rather than hung. 3600s gives it room.
# Most updates never approach this: the change detection in update.sh skips
# the frontend build entirely when frontend/ is untouched.
UPDATE_TIMEOUT_SECONDS = int(os.environ.get("UPDATE_TIMEOUT_SECONDS", "3600"))

# Global lock to prevent concurrent updates
_update_lock = threading.Lock()


def _read_status() -> dict:
    """Read current update status from the status file."""
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "idle", "step": None, "error": None, "started_at": None}


def _stream_output(pipe) -> None:  # noqa: ANN001
    """Log each line of the update script's output as it is produced.

    The script's progress must be logged *live*, not collected at exit: with
    the old ``capture_output=True`` the buffered output was discarded on
    timeout, so a 30-minute hang produced zero log lines and there was no way
    to tell which step was stuck (observed 2026-08-21, v1.9.14 deploy).
    """
    try:
        for line in iter(pipe.readline, ""):
            line = line.rstrip()
            if line:
                logger.info("update | %s", line)
    finally:
        pipe.close()


def _run_update() -> None:
    """Execute the update script in a background thread, streaming its output."""
    logger.info("Starting update process...")
    proc = None
    try:
        proc = subprocess.Popen(  # noqa: S603
            ["/updater/update.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        reader = threading.Thread(target=_stream_output, args=(proc.stdout,), daemon=True)
        reader.start()

        returncode = proc.wait(timeout=UPDATE_TIMEOUT_SECONDS)
        reader.join(timeout=5)

        if returncode != 0:
            logger.error("Update failed (exit %d) — see streamed output above", returncode)
        else:
            logger.info("Update completed successfully")
    except subprocess.TimeoutExpired:
        logger.error(
            "Update timed out after %d seconds — killing update.sh", UPDATE_TIMEOUT_SECONDS
        )
        # Popen does not kill on timeout the way subprocess.run does, so the
        # script would otherwise keep running against the same Docker daemon
        # while the next update attempt starts.
        if proc is not None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error("update.sh did not die after kill()")
        status = {
            "status": "error",
            "step": "timeout",
            "error": f"Update timed out after {UPDATE_TIMEOUT_SECONDS // 60} minutes",
            "started_at": datetime.now(UTC).isoformat(),
        }
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception as exc:
        logger.exception("Unexpected error during update: %s", exc)
    finally:
        _update_lock.release()


class UpdateHandler(BaseHTTPRequestHandler):
    """HTTP request handler for update operations."""

    def _check_auth(self) -> bool:
        """Validate the shared secret from the Authorization header."""
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {UPDATER_SECRET}"
        if auth != expected:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Forbidden"}).encode())
            return False
        return True

    def _send_json(self, status_code: int, data: dict) -> None:
        """Send a JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST requests."""
        if self.path != "/update":
            self._send_json(404, {"error": "Not found"})
            return

        if not self._check_auth():
            return

        # Try to acquire the lock (non-blocking)
        if not _update_lock.acquire(blocking=False):
            self._send_json(409, {"error": "Update already in progress"})
            return

        # Write initial status
        started_at = datetime.now(UTC).isoformat()
        status = {
            "status": "pulling",
            "step": "starting",
            "error": None,
            "started_at": started_at,
        }
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)

        # Spawn update in background thread
        thread = threading.Thread(target=_run_update, daemon=True)
        thread.start()

        self._send_json(202, {"status": "started", "message": "Update process initiated"})

    def do_GET(self) -> None:  # noqa: N802
        """Handle GET requests."""
        if self.path != "/status":
            self._send_json(404, {"error": "Not found"})
            return

        if not self._check_auth():
            return

        status = _read_status()
        self._send_json(200, status)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Override to use Python logging instead of stderr."""
        logger.info(format, *args)


def main() -> None:
    """Start the updater HTTP server."""
    server = HTTPServer(("0.0.0.0", PORT), UpdateHandler)
    logger.info("Updater sidecar listening on port %d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down updater server")
        server.shutdown()


if __name__ == "__main__":
    main()
