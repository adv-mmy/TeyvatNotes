"""API 测试 fixture。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.repositories import InMemoryTaskRepository


@pytest.fixture
def repository() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def client(repository: InMemoryTaskRepository) -> Iterator[TestClient]:
    application = create_app(repository)
    with TestClient(application) as test_client:
        yield test_client
