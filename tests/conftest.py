"""API 测试 fixture。"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.repositories import InMemoryTaskRepository

TEST_API_KEY = "test-only-api-key-32-characters-long"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}
os.environ["AGENT_RUNTIME_API_KEY"] = TEST_API_KEY


@pytest.fixture
def repository() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def client(repository: InMemoryTaskRepository) -> Iterator[TestClient]:
    application = create_app(repository)
    with TestClient(application, headers=AUTH_HEADERS) as test_client:
        yield test_client
