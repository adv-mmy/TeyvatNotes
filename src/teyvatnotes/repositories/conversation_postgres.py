"""短事务行锁+revision核对；事务结束后才允许模型/工具执行。"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationSummary,
    GuideConversation,
)
from teyvatnotes.database.conversations import GuideConversationRecord
from teyvatnotes.repositories.conversation_codec import (
    conversation_from_data,
    conversation_to_data,
    validate_transition,
)


class PostgresConversationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def add(self, conversation: GuideConversation) -> None:
        payload = conversation_to_data(conversation)
        if conversation.revision != 0 or conversation.turns or conversation.closed:
            raise ConversationError("invalid_conversation_state")
        try:
            async with self._sessions.begin() as session:
                session.add(
                    GuideConversationRecord(
                        id=conversation.id,
                        owner_id=conversation.owner_id,
                        revision=conversation.revision,
                        closed=conversation.closed,
                        created_at=conversation.created_at,
                        updated_at=conversation.updated_at,
                        payload=payload,
                    )
                )
        except IntegrityError:
            raise ConversationError("conversation_exists") from None
        except SQLAlchemyError:
            raise ConversationError("conversation_storage_unavailable") from None

    def _decode(self, row: GuideConversationRecord) -> GuideConversation:
        result = conversation_from_data(row.payload)
        if (
            result.id != row.id
            or result.owner_id != row.owner_id
            or result.revision != row.revision
            or result.closed != row.closed
            or result.created_at != row.created_at
            or result.updated_at != row.updated_at
        ):
            raise ConversationError("conversation_storage_corrupt")
        return result

    async def get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
        try:
            async with self._sessions() as session:
                row = await session.scalar(
                    select(GuideConversationRecord).where(
                        GuideConversationRecord.id == conversation_id,
                        GuideConversationRecord.owner_id == owner_id,
                    )
                )
                return self._decode(row) if row else None
        except SQLAlchemyError:
            raise ConversationError("conversation_storage_unavailable") from None

    async def list_recent(
        self, owner_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]:
        record = GuideConversationRecord
        context = record.payload["context"]
        title = func.coalesce(context["entity"].as_string(), context["question"].as_string())
        query = (
            select(
                record.id,
                func.left(title, 120),
                context["game_version"].as_string(),
                record.updated_at,
            )
            .where(record.owner_id == owner_id)
            .order_by(record.updated_at.desc(), record.id.desc())
            .offset(offset)
            .limit(limit)
        )
        try:
            async with self._sessions() as session:
                result = await session.execute(query)
                return tuple(ConversationSummary(*row) for row in result)
        except SQLAlchemyError:
            raise ConversationError("conversation_storage_unavailable") from None

    async def compare_and_swap(
        self, conversation: GuideConversation, expected_revision: int
    ) -> None:
        payload = conversation_to_data(conversation)
        try:
            async with self._sessions.begin() as session:
                row = await session.scalar(
                    select(GuideConversationRecord)
                    .where(
                        GuideConversationRecord.id == conversation.id,
                        GuideConversationRecord.owner_id == conversation.owner_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ConversationError("conversation_not_found")
                validate_transition(self._decode(row), conversation, expected_revision)
                row.revision, row.closed = conversation.revision, conversation.closed
                row.updated_at, row.payload = conversation.updated_at, payload
        except SQLAlchemyError:
            raise ConversationError("conversation_storage_unavailable") from None
