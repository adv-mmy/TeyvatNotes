"""桌面仅访问攻略 HTTPS；无旧配置回退、无自动重放。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from teyvatnotes.desktop.config import DesktopError, load_config, save_config
from teyvatnotes.desktop.service import Workbench


def test_desktop_history_and_no_replay(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    save_config(load_config(path), "https://127.0.0.1:8443")
    calls: list[str] = []
    token = "T" * 43

    def fake(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/sessions"):
            return httpx.Response(
                201,
                json={
                    "access_token": token,
                    "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                },
            )
        assert request.headers["authorization"] == f"Bearer {token}"
        assert b"history" not in request.content and b"API_KEY" not in request.content
        if calls.count("/v1/guides/answer") == 1:
            return httpx.Response(
                200,
                json={
                    "status": "insufficient_evidence",
                    "reason": "source_disabled",
                    "questions": [],
                    "gaps": [],
                    "claims": [],
                    "conflicts": [],
                },
            )
        return httpx.Response(504, json={"code": "model_timeout"})

    async def run() -> None:
        workbench = Workbench(httpx.MockTransport(fake))
        assert not (await workbench.connect(tmp_path)).items
        result = await workbench.submit(None, "钟离怎么配队？")
        assert result.conversation.latest_reply["status"] == "needs_clarification"
        assert not calls
        result = await workbench.submit(
            result.conversation, "版本：7.1；角色列表：钟离；装备列表：无"
        )
        assert result.conversation.latest_reply["reason"] == "source_disabled"
        await workbench.close()

        reopened = Workbench(httpx.MockTransport(fake))
        page = await reopened.connect(tmp_path)
        assert page.items[0].id == result.conversation.id
        previous = await reopened.get(page.items[0].id)
        assert previous.turns == result.conversation.turns
        timed_out = await reopened.submit(previous, "她怎么配队？")
        assert timed_out.conversation.latest_reply["reason"] == "model_timeout"
        assert calls == ["/v1/guides/sessions", "/v1/guides/answer", "/v1/guides/answer"]
        await reopened.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1", "https://user:pwd@localhost", "https://localhost/path"]
)
def test_desktop_config_accepts_https_origin_only(tmp_path: Path, url: str) -> None:
    config = load_config(tmp_path / "client.toml")
    with pytest.raises(DesktopError, match="client_config_invalid"):
        save_config(config, url)
