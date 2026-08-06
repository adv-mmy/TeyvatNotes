"""内存任务仓储测试。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from agent_runtime_foundation.domain import (
    InvalidTaskTransition,
    Task,
    TaskResult,
    TaskStatus,
    TaskType,
)
from agent_runtime_foundation.repositories import InMemoryTaskRepository


def test_add_and_get_task() -> None:
    repository = InMemoryTaskRepository()
    task = Task(task_type=TaskType.SUM_NUMBERS, payload={"numbers": [1, 2]})

    repository.add(task)

    stored = repository.get(task.id)
    assert stored == task
    assert stored is not task
    assert repository.get(uuid4()) is None


def test_add_rejects_duplicate_id_without_overwriting() -> None:
    repository = InMemoryTaskRepository()
    original = Task(payload={"numbers": [1]})
    duplicate = Task(id=original.id, payload={"numbers": [999]})
    repository.add(original)

    with pytest.raises(ValueError):
        repository.add(duplicate)

    assert repository.get(original.id) == original


def test_list_tasks_filters_before_pagination() -> None:
    repository = InMemoryTaskRepository()
    first_queued = Task(task_type=TaskType.WORD_COUNT, payload={"text": "first"})
    running = Task(task_type=TaskType.WORD_COUNT, payload={"text": "running"})
    running.transition_to(TaskStatus.RUNNING)
    second_queued = Task(task_type=TaskType.WORD_COUNT, payload={"text": "second"})
    for task in (first_queued, running, second_queued):
        repository.add(task)

    tasks, total = repository.list_tasks(
        status=TaskStatus.QUEUED,
        offset=1,
        limit=1,
    )

    assert total == 2
    assert tasks == [second_queued]


def test_transition_returns_none_for_unknown_task() -> None:
    repository = InMemoryTaskRepository()

    assert repository.transition(uuid4(), TaskStatus.RUNNING) is None


def test_get_returns_copy_that_cannot_mutate_stored_task() -> None:
    repository = InMemoryTaskRepository()
    task = Task()
    repository.add(task)
    fetched = repository.get(task.id)
    assert fetched is not None

    fetched.transition_to(TaskStatus.RUNNING)

    stored = repository.get(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.QUEUED


def test_transition_updates_task_atomically() -> None:
    repository = InMemoryTaskRepository()
    task = Task()
    repository.add(task)

    transitioned = repository.transition(task.id, TaskStatus.RUNNING)

    assert transitioned is not None
    assert transitioned.status is TaskStatus.RUNNING
    stored = repository.get(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.RUNNING


def test_transition_atomically_saves_and_clears_result() -> None:
    repository = InMemoryTaskRepository()
    task = Task()
    repository.add(task)
    repository.transition(task.id, TaskStatus.RUNNING)

    failed = repository.transition(
        task.id,
        TaskStatus.FAILED,
        result=TaskResult(error="执行失败"),
    )

    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.result == TaskResult(error="执行失败")
    retried = repository.transition(task.id, TaskStatus.QUEUED)
    assert retried is not None
    assert retried.status is TaskStatus.QUEUED
    assert retried.result is None


def test_invalid_transition_does_not_replace_existing_result() -> None:
    repository = InMemoryTaskRepository()
    task = Task()
    repository.add(task)
    repository.transition(task.id, TaskStatus.RUNNING)
    failed = repository.transition(
        task.id,
        TaskStatus.FAILED,
        result=TaskResult(error="原始错误"),
    )
    assert failed is not None
    previous_updated_at = failed.updated_at

    with pytest.raises(InvalidTaskTransition):
        repository.transition(
            task.id,
            TaskStatus.SUCCEEDED,
            result=TaskResult(output={"unexpected": True}),
        )

    stored = repository.get(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    assert stored.result == TaskResult(error="原始错误")
    assert stored.updated_at == previous_updated_at


def test_concurrent_transition_has_exactly_one_success() -> None:
    repository = InMemoryTaskRepository()
    task = Task()
    repository.add(task)

    def request_cancel() -> bool:
        try:
            repository.transition(task.id, TaskStatus.CANCEL_REQUESTED)
        except InvalidTaskTransition:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _: request_cancel(), range(8)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
