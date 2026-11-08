"""异步任务运行时异常。"""


class RuntimeNotAcceptingError(RuntimeError):
    """运行时尚未启动或正在停止，不能接收新任务。"""

    def __init__(self) -> None:
        super().__init__("任务运行时当前不接收新任务")


class TaskExecutionError(RuntimeError):
    """任务类型或参数无法被执行器处理。"""
