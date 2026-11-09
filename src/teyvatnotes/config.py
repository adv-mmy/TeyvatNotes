"""服务端配置；不读取旧任务或桌面模型配置。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from teyvatnotes.database.session import normalize_database_url


@dataclass(frozen=True, slots=True)
class GuideSettings:
    """IP 哈希密钥固定部署注入；跨重启保持相同额度身份。"""

    database_url: str = field(repr=False)
    ip_hash_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        try:
            database = make_url(normalize_database_url(self.database_url))
        except (ArgumentError, ValueError):
            raise ValueError("GUIDE_DATABASE_URL 必须指向新攻略 PostgreSQL 数据库") from None
        if database.drivername != "postgresql+asyncpg" or not database.database:
            raise ValueError("GUIDE_DATABASE_URL 必须指向新攻略 PostgreSQL 数据库")
        if len(self.ip_hash_key) < 32:
            raise ValueError("GUIDE_IP_HASH_KEY 至少需要 32 个字节")

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "GuideSettings":
        env = os.environ if values is None else values
        return cls(env.get("GUIDE_DATABASE_URL", ""), env.get("GUIDE_IP_HASH_KEY", "").encode())
