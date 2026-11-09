"""新攻略库基线不包含旧任务、会话或向量表。"""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from teyvatnotes.database import Base, normalize_database_url

TABLES = {
    "guide_sources",
    "guide_documents",
    "guide_chunks",
    "guide_ingestion_steps",
    "guide_access_sessions",
    "guide_daily_quotas",
}


def test_clean_metadata_and_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUIDE_DATABASE_URL", "postgresql+asyncpg://test@127.0.0.1/test")
    output = StringIO()
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini", output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert set(Base.metadata.tables) == TABLES
    assert all(f"CREATE TABLE {name}" in sql for name in TABLES)
    assert all(
        name not in sql for name in ("CREATE TABLE tasks", "guide_conversations", "DROP TABLE")
    )


def test_migration_never_uses_legacy_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GUIDE_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test@127.0.0.1/legacy")
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini", output_buffer=StringIO())
    with pytest.raises(RuntimeError, match="GUIDE_DATABASE_URL"):
        command.upgrade(config, "head", sql=True)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_normalize_database_url(scheme: str) -> None:
    assert normalize_database_url(f"{scheme}://test@localhost/db") == (
        "postgresql+asyncpg://test@localhost/db"
    )
