"""FastAPI 应用工厂。"""

from teyvatnotes.api.app import create_app
from teyvatnotes.config import ExecutionMode

__all__ = ["ExecutionMode", "create_app"]
