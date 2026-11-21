"""服务端配置；不读取旧任务或桌面模型配置。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from teyvatnotes.application.model_ports import (
    DEFAULT_ANSWER_OUTPUT_TOKENS,
    MAX_MODEL_OUTPUT_TOKENS,
    MIN_ANSWER_OUTPUT_TOKENS,
)
from teyvatnotes.database.session import normalize_database_url


@dataclass(frozen=True, slots=True)
class GuideSettings:
    """IP 哈希密钥固定部署注入；跨重启保持相同额度身份。"""

    database_url: str = field(repr=False)
    ip_hash_key: bytes = field(repr=False)
    answer_max_output_tokens: int = DEFAULT_ANSWER_OUTPUT_TOKENS
    deployment_mode: Literal["public", "local"] = "public"

    def __post_init__(self) -> None:
        if self.deployment_mode not in ("public", "local"):
            raise ValueError("GUIDE_DEPLOYMENT_MODE 必须为 public 或 local")
        limit = self.answer_max_output_tokens
        if (
            type(limit) is not int
            or not MIN_ANSWER_OUTPUT_TOKENS <= limit <= MAX_MODEL_OUTPUT_TOKENS
        ):
            raise ValueError("GUIDE_ANSWER_MAX_OUTPUT_TOKENS 必须是 256–16384 的整数")
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
        raw = env.get("GUIDE_ANSWER_MAX_OUTPUT_TOKENS", str(DEFAULT_ANSWER_OUTPUT_TOKENS))
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 5:
            raise ValueError("GUIDE_ANSWER_MAX_OUTPUT_TOKENS 必须是 256–16384 的整数")
        mode = env.get("GUIDE_DEPLOYMENT_MODE", "public")
        if mode not in ("public", "local"):
            raise ValueError("GUIDE_DEPLOYMENT_MODE 必须为 public 或 local")
        return cls(
            env.get("GUIDE_DATABASE_URL", ""),
            env.get("GUIDE_IP_HASH_KEY", "").encode(),
            int(raw),
            "local" if mode == "local" else "public",
        )
