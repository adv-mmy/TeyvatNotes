"""会话JSON编解码与仓储共用的不变量；不保存模型请求/opaque续接。"""

import json
from datetime import datetime
from typing import cast
from uuid import UUID

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationTurn,
    GuideConversation,
    QuestionPatch,
    merge_question,
    question_from_data,
    question_to_data,
    validate_conversation,
)
from teyvatnotes.domain import JsonValue

MAX_CONVERSATION_BYTES = 2_000_000


def conversation_to_data(value: GuideConversation) -> dict[str, JsonValue]:
    """只保存显式字段；整体有界，不使用pickle或动态类型加载。"""
    validate_conversation(value)
    payload: dict[str, JsonValue] = {
        "id": str(value.id),
        "owner_id": str(value.owner_id),
        "context": question_to_data(value.context),
        "demo_only": value.demo_only,
        "revision": value.revision,
        "closed": value.closed,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
        "turns": [
            {
                "id": str(turn.id),
                "number": turn.number,
                "request": question_to_data(turn.request),
                "started_at": turn.started_at.isoformat(),
                "status": turn.status,
                "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
                "reply": turn.reply,
            }
            for turn in value.turns
        ],
    }
    if (
        len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        > MAX_CONVERSATION_BYTES
    ):
        raise ConversationError("conversation_history_too_large")
    # 独立复制嵌套reply，调用方不能修改数据库/内存仓储正在保存的引用。
    return cast(
        dict[str, JsonValue], json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    )


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def conversation_from_data(payload: object) -> GuideConversation:
    """库内损坏报稳定错误；不把未经校验的JSON直接当应用对象。"""
    try:
        value = _object(
            payload,
            {
                "id",
                "owner_id",
                "context",
                "demo_only",
                "revision",
                "closed",
                "created_at",
                "updated_at",
                "turns",
            },
        )
        raw_turns = value["turns"]
        if not isinstance(raw_turns, list):
            raise ValueError
        turns: list[ConversationTurn] = []
        for raw in raw_turns:
            turn = _object(
                raw, {"id", "number", "request", "started_at", "status", "finished_at", "reply"}
            )
            status = turn["status"]
            if status not in {"pending", "completed", "error", "cancelled", "interrupted"}:
                raise ValueError
            reply = turn["reply"]
            if reply is not None and not isinstance(reply, dict):
                raise ValueError
            # 上面显式白名单完成运行时缩窄，下面仍由应用不变量重验所有轮次。
            turns.append(
                ConversationTurn(
                    UUID(_string(turn["id"])),
                    _integer(turn["number"]),
                    question_from_data(turn["request"]),
                    datetime.fromisoformat(_string(turn["started_at"])),
                    status,
                    datetime.fromisoformat(_string(turn["finished_at"]))
                    if turn["finished_at"] is not None
                    else None,
                    cast(dict[str, JsonValue] | None, reply),
                )
            )
        result = GuideConversation(
            UUID(_string(value["id"])),
            UUID(_string(value["owner_id"])),
            question_from_data(value["context"]),
            _boolean(value["demo_only"]),
            _integer(value["revision"]),
            _boolean(value["closed"]),
            tuple(turns),
            datetime.fromisoformat(_string(value["created_at"])),
            datetime.fromisoformat(_string(value["updated_at"])),
        )
        validate_conversation(result)
        return result
    except (ValueError, TypeError, KeyError, OverflowError, ConversationError):
        raise ConversationError("conversation_storage_corrupt") from None


def validate_transition(before: GuideConversation, after: GuideConversation, expected: int) -> None:
    """只允许领取、完成/中断和关闭，历史与范围不能被CAS顺带改写。"""
    validate_conversation(after)
    if type(expected) is not int or before.revision != expected or after.revision != expected + 1:
        raise ConversationError("conversation_conflict")
    if before.closed:
        raise ConversationError("conversation_closed")
    if (
        before.id != after.id
        or before.owner_id != after.owner_id
        or before.demo_only != after.demo_only
        or before.created_at != after.created_at
    ):
        raise ConversationError("invalid_conversation_transition")
    merge_question(before.context, QuestionPatch(question_to_data(after.context)))
    if after.closed:
        valid = (
            before.active_turn is None
            and before.context == after.context
            and before.turns == after.turns
        )
    elif len(after.turns) == len(before.turns) + 1:
        valid = (
            before.active_turn is None
            and after.active_turn is not None
            and after.turns[:-1] == before.turns
        )
    elif before.active_turn is not None and len(after.turns) == len(before.turns):
        old, new = before.turns[-1], after.turns[-1]
        valid = (
            after.active_turn is None
            and after.turns[:-1] == before.turns[:-1]
            and before.context == after.context
            and old.id == new.id
            and old.number == new.number
            and old.request == new.request
            and old.started_at == new.started_at
        )
    else:
        valid = False
    if not valid or after.updated_at < before.updated_at:
        raise ConversationError("invalid_conversation_transition")
