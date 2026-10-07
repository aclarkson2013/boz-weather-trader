"""Every docker-compose service must cap its container log size (json-file rotation)."""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_every_service_has_rotating_json_file_logs() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    services = compose["services"]
    assert services, "no services found"
    for name, svc in services.items():
        logging_cfg = svc.get("logging")
        assert logging_cfg, f"service '{name}' has no logging config (unbounded container logs)"
        assert logging_cfg["driver"] == "json-file", name
        assert logging_cfg["options"]["max-size"] == "20m", name
        assert logging_cfg["options"]["max-file"] == "5", name
