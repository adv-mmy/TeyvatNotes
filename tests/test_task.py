"""任务状态机测试。"""

from itertools import product

import pytest

from teyvatnotes.domain import (
    InvalidTaskTransition,
    Task,
    TaskResult,
    TaskStatus,
)

LEGAL_TRANSITIONS = [
    (TaskStatus.QUEUED, TaskStatus.RUNNING),
    (TaskStatus.QUEUED, TaskStatus.CANCEL_REQUESTED),
    (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.RUNNING, TaskStatus.CANCEL_REQUESTED),
    (TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED),
    (TaskStatus.FAILED, TaskStatus.QUEUED),
]
LEGAL_TRANSITION_SET = frozenset(LEGAL_TRANSITIONS)
ILLEGAL_TRANSITIONS = [
    transition
    for transition in product(TaskStatus, repeat=2)
    if transition not in LEGAL_TRANSITION_SET
]

PATHS_FROM_QUEUED = {
    TaskStatus.QUEUED: (),
    TaskStatus.RUNNING: (TaskStatus.RUNNING,),
    TaskStatus.SUCCEEDED: (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
    TaskStatus.CANCEL_REQUESTED: (TaskStatus.CANCEL_REQUESTED,),
    TaskStatus.CANCELLED: (
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.FAILED: (TaskStatus.RUNNING, TaskStatus.FAILED),
}


def _task_in_status(status: TaskStatus) -> Task:
    task = Task()
    for target_status in PATHS_FROM_QUEUED[status]:
        task.transition_to(target_status)
    return task


def test_new_task_defaults_to_queued() -> None:
    task = Task()

    assert task.status is TaskStatus.QUEUED
    assert task.result is None
    assert task.created_at.tzinfo is not None
    assert task.updated_at.tzinfo is not None


def test_status_cannot_be_assigned_directly() -> None:
    task = Task()

    with pytest.raises(AttributeError):
        task.status = TaskStatus.SUCCEEDED  # type: ignore[misc]

    assert task.status is TaskStatus.QUEUED


def test_task_result_stores_output_and_error() -> None:
    successful_result = TaskResult(output={"total": 6})
    failed_result = TaskResult(error="执行失败")

    assert successful_result.output == {"total": 6}
    assert successful_result.error is None
    assert failed_result.output is None
    assert failed_result.error == "执行失败"


def test_transition_increments_version() -> None:
    task = Task()

    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.SUCCEEDED, result=TaskResult(output={"total": 1}))

    assert task.version == 2


def test_rehydrate_restores_persisted_state_without_synthetic_transition() -> None:
    source = Task(idempotency_key="persisted")
    source.transition_to(TaskStatus.RUNNING)
    source.transition_to(TaskStatus.FAILED, result=TaskResult(error="失败"))

    restored = Task.rehydrate(
        task_id=source.id,
        task_type=source.task_type,
        payload=dict(source.payload),
        idempotency_key=source.idempotency_key,
        status=source.status,
        result=source.result,
        version=source.version,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )

    assert restored == source
    assert restored.status is TaskStatus.FAILED
    assert restored.version == 2


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    LEGAL_TRANSITIONS,
)
def test_allows_legal_transition(
    current_status: TaskStatus,
    target_status: TaskStatus,
) -> None:
    task = _task_in_status(current_status)
    previous_updated_at = task.updated_at

    task.transition_to(target_status)

    assert task.status is target_status
    assert task.updated_at >= previous_updated_at


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    ILLEGAL_TRANSITIONS,
)
def test_rejects_illegal_transition_without_changing_state(
    current_status: TaskStatus,
    target_status: TaskStatus,
) -> None:
    task = _task_in_status(current_status)
    previous_updated_at = task.updated_at

    with pytest.raises(InvalidTaskTransition) as error_info:
        task.transition_to(target_status)

    assert task.status is current_status
    assert task.updated_at == previous_updated_at
    assert error_info.value.current_status == current_status.value
    assert error_info.value.target_status == target_status.value


@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.SUCCEEDED, TaskStatus.CANCELLED],
)
@pytest.mark.parametrize("target_status", list(TaskStatus))
def test_terminal_status_rejects_every_transition(
    terminal_status: TaskStatus,
    target_status: TaskStatus,
) -> None:
    task = _task_in_status(terminal_status)

    with pytest.raises(InvalidTaskTransition):
        task.transition_to(target_status)
