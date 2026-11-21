"""模型答案的严格 JSON 边界和由知识库派生的引用展示。"""

import json
from dataclasses import replace
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from teyvatnotes.application.guide_answering import (
    AnswerClaim,
    AnswerResult,
    AnswerStructureError,
    ClaimKind,
    ClaimSection,
    GeneratedAnswer,
    player_payload,
)
from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.domain.json_types import JsonValue
from teyvatnotes.knowledge.evidence_json import hit_to_json

Text = Annotated[str, Field(strict=True, min_length=1, max_length=1000, pattern=r"\S")]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=80, pattern=r"\S")]
EvidenceID = Annotated[str, Field(strict=True, min_length=36, max_length=36)]


class ClaimSchema(BaseModel):
    """模型不可给出 URL 或未知字段；每项关键结论独立绑定引用。"""

    model_config = ConfigDict(extra="forbid")
    text: Text
    kind: ClaimKind
    section: ClaimSection
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=8)
    required_equipment: list[Name] = Field(max_length=30)
    required_characters: list[Name] = Field(max_length=30)

    @model_validator(mode="after")
    def valid_identifiers(self) -> "ClaimSchema":
        """规范 UUID 字符串及唯一性；不从外部链接推导证据身份。"""
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate_evidence")
        for identifier in self.evidence_ids:
            if str(UUID(identifier)) != identifier:
                raise ValueError("invalid_evidence")
        return self


class AnswerSchema(BaseModel):
    """所有字段必填，空数组也需明确输出；不接受 markdown 围栏或额外说明。"""

    model_config = ConfigDict(extra="forbid")
    status: Literal["answer", "needs_clarification", "insufficient_evidence"]
    claims: list[ClaimSchema] = Field(max_length=20)
    questions: list[Text] = Field(max_length=8)
    gaps: list[Text] = Field(max_length=10)
    conflicts: list[Text] = Field(max_length=10)

    @model_validator(mode="after")
    def coherent_status(self) -> "AnswerSchema":
        """只报告首个冲突；日志值仅来自固定规则、已校验枚举和有界的条数。"""
        rule = None
        if self.status == "answer":
            if not self.claims:
                rule = "answer_requires_claims"
            elif self.questions:
                rule = "answer_forbids_questions"
            elif self.conflicts:
                rule = "answer_forbids_conflicts"
        elif self.claims:
            rule = "non_answer_forbids_claims"
        elif self.status == "needs_clarification" and not self.questions:
            rule = "clarification_requires_questions"
        elif self.status == "insufficient_evidence" and not (self.gaps or self.conflicts):
            rule = "insufficient_requires_gaps_or_conflicts"
        if rule is not None:
            raise AnswerStructureError(
                f"status_conflict rule={rule} answer_status={self.status} "
                f"claims_count={len(self.claims)} questions_count={len(self.questions)} "
                f"gaps_count={len(self.gaps)} conflicts_count={len(self.conflicts)}"
            )
        return self


def _validation_detail(error: ValidationError) -> str:
    """仅报告首处错误；不记录 msg、ctx、input 或模型自定的键名。"""
    first = error.errors(include_url=False, include_input=False, include_context=False)[0]
    kind = first["type"]
    if kind == "json_invalid":
        return kind
    if kind == "extra_forbidden":
        return "unexpected_field"
    if kind == "missing":
        category = "missing_field"
    elif kind in {"model_type", "list_type", "string_type"}:
        category = "invalid_type"
    else:
        category = "invalid_value"
    # loc 也可能来自上游，不能直接拼接。只输出模型定义的字段和有界的数组下标。
    path = ""
    for part in first["loc"]:
        if isinstance(part, str) and (
            part in AnswerSchema.model_fields or part in ClaimSchema.model_fields
        ):
            path += ("." if path else "") + part
        elif type(part) is int and 0 <= part < 30:
            path += f"[{part}]"
        else:
            return category
    return f"{category}:{path}" if path else category


class JsonAnswerDecoder:
    """Pydantic 只在边界使用，用例得到纯 Python 数据类。"""

    def schema(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], AnswerSchema.model_json_schema())

    def decode(self, text: str) -> GeneratedAnswer:
        if len(text) > 32_000:
            raise AnswerStructureError("answer_text_too_long")
        try:
            value = AnswerSchema.model_validate_json(text)
        except ValidationError as exc:
            raise AnswerStructureError(_validation_detail(exc)) from None
        except ValueError:
            # 不保留底层异常消息（如无法解码的字符串）。
            raise AnswerStructureError("schema_invalid") from None
        return GeneratedAnswer(
            value.status,
            tuple(
                AnswerClaim(
                    claim.text,
                    claim.kind,
                    claim.section,
                    tuple(claim.evidence_ids),
                    tuple(claim.required_equipment),
                    tuple(claim.required_characters),
                )
                for claim in value.claims
            ),
            tuple(value.questions),
            tuple(value.gaps),
            tuple(value.conflicts),
        )


def answer_to_json(
    result: AnswerResult, request: QuestionRequest, *, simulated_model: bool = False
) -> dict[str, JsonValue]:
    """引用位置/链接只从已取证据建立，收费金额未知时始终为 null。"""
    citations: dict[str, dict[str, JsonValue]] = {}
    for window in result.evidence:
        for chunk in window.chunks:
            citations[str(chunk.id)] = hit_to_json(replace(window.hit, chunk=chunk))
    claims: list[JsonValue] = [
        {
            "text": claim.text,
            "kind": claim.kind,
            "section": claim.section,
            "evidence_ids": list(claim.evidence_ids),
            "required_equipment": list(claim.required_equipment),
            "required_characters": list(claim.required_characters),
            "citations": [citations[identifier] for identifier in claim.evidence_ids],
        }
        for claim in result.claims
    ]
    response = result.response
    warnings: list[JsonValue] = [
        "citation_support_not_verified",
        "not_fact_verified",
        "cost_unknown",
    ]
    if result.demo_only:
        warnings.append("fictional_demo_only")
    if simulated_model:
        warnings.append("simulated_model_no_network")
    return {
        "status": result.status,
        "claims": claims,
        "questions": list(result.questions),
        "gaps": list(result.gaps),
        "conflicts": list(result.conflicts),
        "reason": result.reason,
        "used_player_conditions": player_payload(request),
        "demo_only": result.demo_only,
        "model_called": result.model_called and not simulated_model,
        "generation_attempts": int(result.model_called),
        "provider": "offline_demo" if simulated_model else "responses_compatible",
        "model": response.model if response else None,
        "input_tokens": response.usage.input_tokens if response else None,
        "output_tokens": response.usage.output_tokens if response else None,
        "cost": None,
        "elapsed_ms": result.elapsed_ms,
        "warnings": warnings,
    }


def demo_answer_json(player: dict[str, JsonValue], evidence: list[JsonValue]) -> str:
    """仅用于明确虚构的离线演示，摘录原文而非伪装模型推理。"""
    claims: list[JsonValue] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ModelError("invalid_model_request")
        section = "summary"
        if player.get("guide_kind") == "quest":
            label = item.get("section")
            section = "prerequisite" if label == "前置条件" else "step"
            text = item.get("text")
            if isinstance(text, str) and text.startswith("完成标志"):
                section = "completion"
        claims.append(
            {
                "text": item.get("text"),
                "kind": "fact",
                "section": section,
                "evidence_ids": [item.get("evidence_id")],
                "required_equipment": [],
                "required_characters": [],
            }
        )
    return json.dumps(
        {"status": "answer", "claims": claims, "questions": [], "gaps": [], "conflicts": []},
        ensure_ascii=False,
    )
