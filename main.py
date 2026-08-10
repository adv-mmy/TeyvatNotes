"""FastAPI 本地开发入口。"""

from agent_runtime_foundation.api import create_app

app = create_app()

__all__ = ["app"]
