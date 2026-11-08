"""任务领域异常。"""


class InvalidTaskTransition(ValueError):
    """任务状态转换不符合状态机规则。"""

    def __init__(self, current_status: str, target_status: str) -> None:
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"任务状态不能从 {current_status!r} 转换为 {target_status!r}")
