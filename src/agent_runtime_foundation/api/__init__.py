"""FastAPI 应用工厂。"""

from agent_runtime_foundation.api.app import create_app
from agent_runtime_foundation.config import ExecutionMode

__all__ = ["ExecutionMode", "create_app"]
