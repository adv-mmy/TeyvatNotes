"""共享攻略服务进程入口。"""

import logging

from teyvatnotes.api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
app = create_app()

__all__ = ["app"]
