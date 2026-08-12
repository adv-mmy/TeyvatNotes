"""FastAPI 本地开发入口。"""

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.observability import configure_logging

configure_logging()
app = create_app()

__all__ = ["app"]
