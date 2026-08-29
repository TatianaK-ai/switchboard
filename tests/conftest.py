"""Test bootstrap.

`switchboard.config` reads the environment once, at import time, so every variable the
suite depends on must be set BEFORE any project module is imported. That is why this
happens at module scope rather than in a fixture.

Tests run against an isolated DATA_DIR so a test run can never write tickets into the
directory the real agent reads from. The runbooks and the employee directory are copied
in rather than pointed at, so a test that mutates them cannot corrupt the originals.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DATA = ROOT / "data-test"

os.environ.setdefault("DATA_DIR", "data-test")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
# Guardrail tests must not depend on a model being reachable; none of them call one.
os.environ.setdefault("NEBIUS_API_KEY", "")

TEST_DATA.mkdir(exist_ok=True)
shutil.copytree(ROOT / "data" / "runbooks", TEST_DATA / "runbooks", dirs_exist_ok=True)
shutil.copy2(ROOT / "data" / "employees.json", TEST_DATA / "employees.json")

for stale in ("switchboard.db", "checkpoints.db"):
    (TEST_DATA / stale).unlink(missing_ok=True)

import pytest  # noqa: E402

from switchboard.memory import store  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    store.init()
    yield
