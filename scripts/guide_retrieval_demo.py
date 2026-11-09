"""无模型演示：确定性追问、关键词检索、可核查引用及前置步骤回读。"""

import asyncio
from pathlib import Path

from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.domain.json_types import JsonValue
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.evidence_json import preparation_to_json
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.questions import load_question

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def run_guide_retrieval_demo() -> dict[str, JsonValue]:
    """明确不发布虚构样本，演示输出不是现实游戏建议或模型生成回答。"""
    repository = await create_demo_repository()
    service = QuestionPreparationService(repository, demo_only=True)
    cases: list[JsonValue] = []
    for name in ("clarification.json", "character.json", "quest.json"):
        request = await asyncio.to_thread(
            load_question, PROJECT_ROOT / "samples" / "questions" / name
        )
        cases.append({"case": name, **preparation_to_json(await service.prepare(request))})
    return {"storage": "memory_demo", "external_requests": 0, "model_called": False, "cases": cases}


def main() -> None:
    """输出完整证据 JSON，进程结束后演示数据不保留。"""
    emit_json(asyncio.run(run_guide_retrieval_demo()))


if __name__ == "__main__":
    main()
