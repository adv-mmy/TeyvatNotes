"""API 与 Worker 拆分后的纯 API 进程入口。"""

from teyvatnotes.api import create_app
from teyvatnotes.observability import configure_logging

configure_logging()
app = create_app(execution_mode="api_only")

__all__ = ["app"]
