"""Worker 错误分类与租约所有权异常。"""


class RetryableTaskError(RuntimeError):
    """短暂故障，在最大尝试次数内可重试。"""


class PermanentTaskError(RuntimeError):
    """永久故障，再次执行不会改变结果。"""


class LeaseOwnershipError(RuntimeError):
    """不允许过期或非当前 Worker 的 attempt 写入结果。"""
