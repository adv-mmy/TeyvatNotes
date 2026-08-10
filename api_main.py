"""API 与 Worker 拆分后的纯 API 进程入口。"""

from agent_runtime_foundation.api import create_app

app = create_app(execution_mode="api_only")

__all__ = ["app"]
