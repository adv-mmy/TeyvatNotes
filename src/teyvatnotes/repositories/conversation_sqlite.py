"""桌面专用会话仓储；SQLite 事务不跨网络请求。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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

APPLICATION_ID = 0x544E4354
SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """预检中的 inserted 表示可导入数；冲突时整批不写。"""

    inserted: int
    unchanged: int
    conflicts: int


def _payload(conversation: GuideConversation) -> str:
    return json.dumps(
        conversation_to_data(conversation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _open(path: Path, mode: str) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.absolute().as_uri()}?mode={mode}", uri=True, timeout=5)


def _profile(db: sqlite3.Connection, *, initialize: bool = False) -> UUID:
    identity = db.execute("PRAGMA application_id").fetchone()
    version = db.execute("PRAGMA user_version").fetchone()
    if (
        initialize
        and identity == (0,)
        and version == (0,)
        and not db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    ):
        owner = uuid4()
        db.execute(
            "CREATE TABLE client_profile ("
            "slot INTEGER PRIMARY KEY CHECK(slot = 1), owner_id TEXT NOT NULL UNIQUE)"
        )
        db.execute(
            "CREATE TABLE client_conversations ("
            "id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL, "
            "revision INTEGER NOT NULL CHECK(revision >= 0), title TEXT NOT NULL, "
            "game_version TEXT, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX client_conversations_recent ON "
            "client_conversations(owner_id, updated_at DESC, id DESC)"
        )
        db.execute("INSERT INTO client_profile VALUES (1, ?)", (str(owner),))
        db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return owner
    if identity != (APPLICATION_ID,) or version != (SCHEMA_VERSION,):
        raise ConversationError("conversation_storage_version")
    profile = db.execute("SELECT owner_id FROM client_profile WHERE slot = 1").fetchone()
    try:
        if profile is not None and isinstance(profile[0], str):
            return UUID(profile[0])
    except ValueError:
        pass
    raise ConversationError("conversation_storage_corrupt")


def _summary(value: GuideConversation) -> tuple[str, str | None, str]:
    return (
        (value.context.entity or value.context.question)[:120],
        value.context.game_version,
        value.updated_at.astimezone(UTC).isoformat(timespec="microseconds"),
    )


def _stored(db: sqlite3.Connection, identifier: UUID, owner: UUID) -> GuideConversation | None:
    row = db.execute(
        "SELECT revision, payload FROM client_conversations WHERE id = ? AND owner_id = ?",
        (str(identifier), str(owner)),
    ).fetchone()
    if row is None:
        return None
    try:
        revision, raw = row
        value = conversation_from_data(json.loads(raw))
        if value.id == identifier and value.owner_id == owner and value.revision == revision:
            return value
    except (ValueError, TypeError, RecursionError):
        pass
    raise ConversationError("conversation_storage_corrupt")


def _insert(db: sqlite3.Connection, conversation: GuideConversation) -> None:
    db.execute(
        "INSERT INTO client_conversations "
        "(id, owner_id, revision, title, game_version, updated_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(conversation.id),
            str(conversation.owner_id),
            conversation.revision,
            *_summary(conversation),
            _payload(conversation),
        ),
    )


class SqliteConversationRepository:
    """每次操作独占短连接；多进程以 BEGIN IMMEDIATE 串行化修订和导入。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialize(self) -> UUID:
        return await asyncio.to_thread(self._initialize)

    def _initialize(self) -> UUID:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(_open(self._path, "rwc")) as db, db:
                db.execute("BEGIN IMMEDIATE")
                return _profile(db, initialize=True)
        except (sqlite3.Error, OSError):
            raise ConversationError("conversation_storage_unavailable") from None

    async def add(self, conversation: GuideConversation) -> None:
        await asyncio.to_thread(self._add, conversation)

    def _add(self, conversation: GuideConversation) -> None:
        if conversation.revision or conversation.turns or conversation.closed:
            raise ConversationError("invalid_conversation_state")
        try:
            with closing(_open(self._path, "rw")) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if _profile(db) != conversation.owner_id:
                    raise ConversationError("invalid_conversation_owner")
                _insert(db, conversation)
        except sqlite3.IntegrityError:
            raise ConversationError("conversation_already_exists") from None
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
        return await asyncio.to_thread(self._get, owner_id, conversation_id)

    def _get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
        try:
            with closing(_open(self._path, "ro")) as db:
                return _stored(db, conversation_id, owner_id) if _profile(db) == owner_id else None
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def list_recent(
        self, owner_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]:
        return await asyncio.to_thread(self._list_recent, owner_id, offset, limit)

    def _list_recent(
        self, owner_id: UUID, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ConversationError("invalid_conversation_pagination")
        try:
            with closing(_open(self._path, "ro")) as db:
                if _profile(db) != owner_id:
                    return ()
                records = db.execute(
                    "SELECT id, title, game_version, updated_at FROM client_conversations "
                    "WHERE owner_id = ? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                    (str(owner_id), limit, offset),
                )
                return tuple(
                    ConversationSummary(UUID(key), title, version, datetime.fromisoformat(updated))
                    for key, title, version, updated in records
                )
        except (ValueError, TypeError):
            raise ConversationError("conversation_storage_corrupt") from None
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def compare_and_swap(
        self, conversation: GuideConversation, expected_revision: int
    ) -> None:
        await asyncio.to_thread(self._compare_and_swap, conversation, expected_revision)

    def _compare_and_swap(self, conversation: GuideConversation, expected_revision: int) -> None:
        try:
            with closing(_open(self._path, "rw")) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if _profile(db) != conversation.owner_id:
                    raise ConversationError("conversation_not_found")
                previous = _stored(db, conversation.id, conversation.owner_id)
                if previous is None:
                    raise ConversationError("conversation_not_found")
                validate_transition(previous, conversation, expected_revision)
                updated = db.execute(
                    "UPDATE client_conversations SET revision = ?, title = ?, game_version = ?, "
                    "updated_at = ?, payload = ? WHERE id = ? AND owner_id = ? AND revision = ?",
                    (
                        conversation.revision,
                        *_summary(conversation),
                        _payload(conversation),
                        str(conversation.id),
                        str(conversation.owner_id),
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise ConversationError("conversation_conflict")
        except sqlite3.Error:
            raise ConversationError("conversation_storage_unavailable") from None

    async def import_conversations(
        self, conversations: tuple[GuideConversation, ...], *, dry_run: bool = True
    ) -> ImportSummary:
        """仅供独立迁移命令；保留会话 ID，重绑定到目标私有用户。"""
        return await asyncio.to_thread(self._import, conversations, dry_run)

    @staticmethod
    def _compare_import(
        db: sqlite3.Connection | None,
        owner: UUID,
        conversations: tuple[GuideConversation, ...],
    ) -> tuple[ImportSummary, tuple[GuideConversation, ...]]:
        pending: dict[UUID, GuideConversation] = {}
        unchanged = conflicts = 0
        for raw in conversations:
            value = replace(raw, owner_id=owner)
            encoded = _payload(value)
            if value.active_turn is not None:
                raise ConversationError("conversation_import_pending")
            current = pending.get(value.id)
            if current is None and db is not None:
                current = _stored(db, value.id, owner)
            if current is None:
                pending[value.id] = value
            elif _payload(current) == encoded:
                unchanged += 1
            else:
                conflicts += 1
        return ImportSummary(len(pending) if not conflicts else 0, unchanged, conflicts), tuple(
            pending.values()
        )

    def _import(self, conversations: tuple[GuideConversation, ...], dry_run: bool) -> ImportSummary:
        try:
            # 预检和内部冲突都不能隐式初始化目标。
            if not self._path.exists():
                result, _ = self._compare_import(None, uuid4(), conversations)
                if dry_run or result.conflicts or not result.inserted:
                    return result
            if dry_run:
                with closing(_open(self._path, "ro")) as db:
                    db.execute("BEGIN")
                    return self._compare_import(db, _profile(db), conversations)[0]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(_open(self._path, "rwc")) as db, db:
                db.execute("BEGIN IMMEDIATE")
                owner = _profile(db, initialize=True)
                result, pending = self._compare_import(db, owner, conversations)
                if not result.conflicts:
                    for value in pending:
                        _insert(db, value)
                return result
        except (OSError, sqlite3.Error):
            raise ConversationError("conversation_storage_unavailable") from None
