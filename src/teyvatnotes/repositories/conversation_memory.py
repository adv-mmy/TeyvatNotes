"""线程安全、原子修订与复制隔离的会话仓储；演示退出后不持久。"""

from threading import Lock
from uuid import UUID

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationSummary,
    GuideConversation,
)
from teyvatnotes.domain import JsonValue
from teyvatnotes.repositories.conversation_codec import (
    conversation_from_data,
    conversation_to_data,
    validate_transition,
)


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._values: dict[UUID, dict[str, JsonValue]] = {}
        self._lock = Lock()

    async def add(self, conversation: GuideConversation) -> None:
        payload = conversation_to_data(conversation)
        if conversation.revision != 0 or conversation.turns or conversation.closed:
            raise ConversationError("invalid_conversation_state")
        with self._lock:
            if conversation.id in self._values:
                raise ConversationError("conversation_exists")
            self._values[conversation.id] = payload

    async def get(self, owner_id: UUID, conversation_id: UUID) -> GuideConversation | None:
        with self._lock:
            raw = self._values.get(conversation_id)
            if raw is None or raw["owner_id"] != str(owner_id):
                return None
            return conversation_from_data(raw)

    async def list_recent(
        self, owner_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConversationSummary, ...]:
        with self._lock:
            records = [
                conversation_from_data(raw)
                for raw in self._values.values()
                if raw["owner_id"] == str(owner_id)
            ]
        records.sort(key=lambda value: (value.updated_at, value.id), reverse=True)
        return tuple(
            ConversationSummary(
                record.id,
                (record.context.entity or record.context.question)[:120],
                record.context.game_version,
                record.updated_at,
            )
            for record in records[offset : offset + limit]
        )

    async def compare_and_swap(
        self, conversation: GuideConversation, expected_revision: int
    ) -> None:
        with self._lock:
            raw = self._values.get(conversation.id)
            if raw is None or raw["owner_id"] != str(conversation.owner_id):
                raise ConversationError("conversation_not_found")
            validate_transition(conversation_from_data(raw), conversation, expected_revision)
            self._values[conversation.id] = conversation_to_data(conversation)
