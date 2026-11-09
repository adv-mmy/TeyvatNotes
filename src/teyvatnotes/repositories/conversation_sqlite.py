"""桌面会话的本地存储；短写事务串行化修订检查，不跨模型请求。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationSummary,
    GuideConversation,
)
from teyvatnotes.repositories.conversation_codec import (
    conversation_from_data,
    conversation_to_data,
    validate_transition,
)


def _payload(conversation: GuideConversation) -> str:
    return json.dumps(conversation_to_data(conversation), ensure_ascii=False, allow_nan=False)


def _summary_values(conversation: GuideConversation) -> tuple[str, str | None, str]:
    return (
        (conversation.context.entity or conversation.context.question)[:120],
        conversation.context.game_version,
        conversation.updated_at.isoformat(),
    )


class SqliteConversationRepository:
    """使用标准库 SQLite 和既有会话编解码器，每次操作独立连接。"""

    def __init__(self, path: Path) -> None:
        self._path = path.absolute()

    async def initialize(self) -> UUID:
        """首次启动创建 schema 和标识；重复或并发启动复用已有值。"""
        return await asyncio.to_thread(self._initialize)

    def _initialize(self) -> UUID:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    db.execute(
                        "CREATE TABLE local_profile (slot INTEGER PRIMARY KEY CHECK(slot = 1), "
                        "owner_id TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE TABLE guide_conversations (id TEXT PRIMARY KEY NOT NULL, "
                        "owner_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL, "
                        "game_version TEXT, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE INDEX conversations_recent ON "
                        "guide_conversations(owner_id, updated_at DESC, id DESC)"
                    )
                    db.execute("INSERT INTO local_profile VALUES (1, ?)", (str(uuid4()),))
                    db.execute("PRAGMA user_version = 1")
                elif version != 1:
                    raise ConversationError("conversation_storage_version")
                row = db.execute("SELECT owner_id FROM local_profile WHERE slot = 1").fetchone()
                if row is None:
                    raise ConversationError("conversation_storage_corrupt")
                return UUID(row[0])
        except (TypeError, ValueError):
            raise ConversationError("conversation_storage_corrupt") from None
        except (OSError, sqlite3.Error):
            raise ConversationError("conversation_storage_unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=5)

    @staticmethod
    def _read(db: sqlite3.Connection, owner_id: UUID, identifier: UUID) -> GuideConversation | None:
        row = db.execute(
            "SELECT revision, payload FROM guide_conversations WHERE owner_id = ? AND id = ?",
            (str(owner_id), str(identifier)),
        ).fetchone()
        if row is None:
            return None
        try:
            value = conversation_from_data(json.loads(row[1]))
        except (TypeError, ValueError):
            raise ConversationError("conversation_storage_corrupt") from None
        if value.id != identifier or value.owner_id != owner_id or value.revision != row[0]:
            raise ConversationError("conversation_storage_corrupt")
        return value

    async def add(self, conversation: GuideConversation) -> None:
        if conversation.revision != 0 or conversation.turns or conversation.closed:
            raise ConversationError("invalid_conversation_state")
        await asyncio.to_thread(self._add, conversation)

    def _add(self, conversation: GuideConversation) -> None:
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO guide_conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(conversation.id),
                        str(conversation.owner_id),
                        conversation.revision,
                        *_summary_values(conversation),
                        _payload(conversation),
                    ),
                )
        except sqlite3.IntegrityError:
            raise ConversationError("conversation_exists") from None
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
        return await asyncio.to_thread(self._get, owner_id, conversation_id)

    def _get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
        try:
            with closing(self._connect()) as db:
                return self._read(db, owner_id, conversation_id)
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def list_recent(
        self, owner_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]:
        return await asyncio.to_thread(self._list_recent, owner_id, offset, limit)

    def _list_recent(
        self, owner_id: UUID, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]:
        try:
            with closing(self._connect()) as db:
                rows = db.execute(
                    "SELECT id, title, game_version, updated_at FROM guide_conversations "
                    "WHERE owner_id = ? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                    (str(owner_id), limit, offset),
                )
                return tuple(
                    ConversationSummary(UUID(key), title, version, datetime.fromisoformat(updated))
                    for key, title, version, updated in rows
                )
        except (TypeError, ValueError):
            raise ConversationError("conversation_storage_corrupt") from None
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def compare_and_swap(
        self, conversation: GuideConversation, expected_revision: int
    ) -> None:
        await asyncio.to_thread(self._compare_and_swap, conversation, expected_revision)

    def _compare_and_swap(self, conversation: GuideConversation, expected_revision: int) -> None:
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                before = self._read(db, conversation.owner_id, conversation.id)
                if before is None:
                    raise ConversationError("conversation_not_found")
                validate_transition(before, conversation, expected_revision)
                db.execute(
                    "UPDATE guide_conversations SET revision = ?, title = ?, game_version = ?, "
                    "updated_at = ?, payload = ? WHERE owner_id = ? AND id = ?",
                    (
                        conversation.revision,
                        *_summary_values(conversation),
                        _payload(conversation),
                        str(conversation.owner_id),
                        str(conversation.id),
                    ),
                )
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None
