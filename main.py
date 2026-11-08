"""FastAPI 本地开发入口。"""

from teyvatnotes.api import create_app
from teyvatnotes.observability import configure_logging

configure_logging()
app = create_app()

__all__ = ["app"]
