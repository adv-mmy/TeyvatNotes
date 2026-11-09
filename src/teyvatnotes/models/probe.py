"""用户主动执行的单次连通性验证：少量 Token，不需要数据库。"""

import asyncio
from time import perf_counter

from teyvatnotes.application.model_ports import ModelError, ModelMessage, ModelRequest
from teyvatnotes.domain import JsonValue
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.models.responses import configured_client


async def probe() -> dict[str, JsonValue]:
    """仅发送“你好”，不上传攻略、玩家信息或任何本机文件。"""
    client = configured_client()
    started = perf_counter()
    response = await client.generate(ModelRequest((ModelMessage("user", "你好"),), 128))
    return {
        "status": "success",
        "provider": "responses_compatible",
        "model": response.model,
        "model_called": True,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "elapsed_ms": round((perf_counter() - started) * 1000),
        "cost": None,
    }


def main() -> int:
    """只输出连接结果和用量，不回显密钥或原始响应。"""
    try:
        result = asyncio.run(probe())
    except ModelError as exc:
        result = {"status": "error", "error": exc.code}
    except Exception:
        result = {"status": "error", "error": "model_unavailable"}
    emit_json(result)
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
