"""API 测试 fixture。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.api.dependencies import get_task_repository
from agent_runtime_foundation.application import TaskRepository
from agent_runtime_foundation.repositories import InMemoryTaskRepository


@pytest.fixture
def repository() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def client(repository: InMemoryTaskRepository) -> Iterator[TestClient]:
    application = create_app()

    def override_repository() -> TaskRepository:
        return repository

    application.dependency_overrides[get_task_repository] = override_repository
    with TestClient(application) as test_client:
        yield test_client
