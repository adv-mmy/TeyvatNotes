"""确定性任务执行器及其抽象协议。"""

from typing import Protocol

from agent_runtime_foundation.domain import JsonValue, Task, TaskType
from agent_runtime_foundation.runtime.errors import TaskExecutionError


class TaskExecutor(Protocol):
    """运行时可调用的异步任务执行端口。"""

    async def execute(self, task: Task) -> JsonValue:
        """执行任务并返回可 JSON 序列化的结果。"""


class DeterministicTaskExecutor:
    """执行当前白名单中的数字求和和单词计数任务。"""

    async def execute(self, task: Task) -> JsonValue:
        if task.task_type is TaskType.SUM_NUMBERS:
            return self._sum_numbers(task)
        if task.task_type is TaskType.WORD_COUNT:
            return self._word_count(task)
        raise TaskExecutionError(f"不支持的任务类型：{task.task_type}")

    @staticmethod
    def _sum_numbers(task: Task) -> JsonValue:
        numbers = task.payload.get("numbers")
        if not isinstance(numbers, list):
            raise TaskExecutionError("sum_numbers 的 numbers 必须是整数列表")

        total = 0
        for number in numbers:
            if type(number) is not int:
                raise TaskExecutionError("sum_numbers 的 numbers 必须只包含整数")
            total += number
        return {"total": total}

    @staticmethod
    def _word_count(task: Task) -> JsonValue:
        text = task.payload.get("text")
        if not isinstance(text, str):
            raise TaskExecutionError("word_count 的 text 必须是字符串")
        return {"word_count": len(text.split())}
