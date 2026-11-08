"""离线评测入口：私有JSON报告，发现回归问题时非零退出，不偷偷调用模型。"""

import argparse
import asyncio

from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.knowledge.evaluation import run_evaluation
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.reliability import run_reliability
from teyvatnotes.knowledge.understanding_evaluation import run_understanding_evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定虚构攻略评测与离线可靠性演示")
    parser.add_argument("command", choices=("demo", "reliability", "understanding"))
    parser.add_argument(
        "--mode", choices=("all", "rules", "simulated"), help="仅understanding使用，默认all"
    )
    arguments = parser.parse_args(argv)
    if arguments.command != "understanding" and arguments.mode is not None:
        parser.error("--mode仅用于understanding")
    try:
        if arguments.command == "understanding":
            result = asyncio.run(run_understanding_evaluation(arguments.mode or "all"))
        else:
            result = asyncio.run(
                run_evaluation() if arguments.command == "demo" else run_reliability()
            )
    except GuideImportError as exc:
        result = {"status": "error", "error": exc.code}
    except Exception:
        result = {"status": "error", "error": "evaluation_unavailable"}
    emit_json(result)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
