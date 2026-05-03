"""Smoke tests for FastAPI route binding.

Regression coverage for a bug where ``RunBody`` / ``DriveAuthBody`` were
defined inside ``create_app`` and FastAPI could not resolve the (string-
form, due to ``from __future__ import annotations``) type hints. As a
result the parameter was treated as a query parameter and POST /api/run
returned 422 with ``loc=["query","body"]``.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SIBPARSER_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("SIBPARSER_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SIBPARSER_HEADFUL", "false")
    # Reset the module-level Settings cache so the patched env is picked up.
    from sibparser import config as config_mod

    config_mod._settings = None
    from sibparser.server import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
    config_mod._settings = None


def test_run_endpoint_accepts_json_body(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/run must parse a JSON body, not 422 with loc=query/body.

    We patch ``Runner.run`` so it returns immediately and doesn't actually
    launch Playwright; this isolates the test to body parsing + endpoint
    plumbing.
    """
    from sibparser import runner as runner_mod

    def _noop_run(self: runner_mod.Runner, request: runner_mod.RunRequest) -> None:
        return None

    monkeypatch.setattr(runner_mod.Runner, "run", _noop_run)

    body = {
        "selected_category_paths": ["Питание/Батончики"],
        "products_per_category_limit": 1,
        "upload_to_drive": False,
    }
    r = client.post("/api/run", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True}


def test_drive_auth_endpoint_accepts_json_body(client: TestClient) -> None:
    """POST /api/auth/drive must parse a JSON body, not 422 with loc=query/body."""
    r = client.post("/api/auth/drive", json={"credentials_path": "/no/such/file.json"})
    # 400 because the path doesn't exist (which means the body parsed and
    # the handler reached the existence check).
    assert r.status_code == 400, r.text
    assert "credentials.json" in r.json()["detail"]


def test_status_endpoint_returns_dict(client: TestClient) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    for key in (
        "credentials_present",
        "token_present",
        "drive_authorized",
        "tree_loaded",
        "headful",
    ):
        assert key in data
