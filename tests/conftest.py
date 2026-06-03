"""Shared fixtures for agentmemory integration tests."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

BASE = "http://localhost:3111"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def skip_if_down():
    """Skip the entire test module if agentmemory is unreachable."""
    try:
        r = httpx.get(f"{BASE}/agentmemory/health", timeout=3.0)
        r.raise_for_status()
    except Exception:
        pytest.skip(
            "agentmemory service unreachable at localhost:3111 — "
            "run: systemctl --user start agentmemory"
        )


@pytest.fixture(scope="session")
def client(skip_if_down):
    with httpx.Client(base_url=BASE, timeout=20.0) as c:
        yield c


@pytest.fixture
def snapshot():
    """Snapshot helper: saves data to fixtures/ on first call, loads on subsequent calls.

    Usage inside a test:
        saved = snapshot("health", live_data)

    First run: writes fixtures/health.json and returns live_data.
    Later runs: loads fixtures/health.json and returns the saved structure.

    WHY: documents the "known good" API shape at test-write time so structural
    regressions (missing keys, changed types) surface on re-runs without
    requiring a network call to validate.
    """

    def _snapshot(name: str, data: object) -> object:
        FIXTURES_DIR.mkdir(exist_ok=True)
        path = FIXTURES_DIR / f"{name}.json"
        if not path.exists():
            path.write_text(json.dumps(data, indent=2, default=str))
        return json.loads(path.read_text())

    return _snapshot
