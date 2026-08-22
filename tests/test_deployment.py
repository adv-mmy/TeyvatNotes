"""第七周 Dockerfile 与 Compose 静态验收。"""

from pathlib import Path

import pytest
import yaml

from agent_runtime_foundation.config import AppSettings, ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_runs_as_non_root_and_has_a_healthcheck() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/health/live" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "AGENT_RUNTIME_API_KEY=" not in dockerfile


def test_compose_has_persistent_database_migration_and_embedded_worker() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"postgres", "migrate", "api"}
    assert services["postgres"]["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == ("service_healthy")
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["api"]["environment"]["AGENT_RUNTIME_EXECUTION_MODE"] == ("embedded")
    assert services["api"]["read_only"] is True
    assert services["api"]["user"] == "10001:10001"
    assert services["api"]["init"] is True
    assert services["api"]["stop_grace_period"] == "15s"
    assert "/health/ready" in services["api"]["healthcheck"]["test"][-1]
    assert "postgres-data" in compose["volumes"]


def test_real_environment_file_is_ignored_and_example_has_no_real_secret() -> None:
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert ".env" in gitignore.splitlines()
    assert "sk-" not in example

    example_values = dict(
        line.split("=", maxsplit=1)
        for line in example.splitlines()
        if line and not line.startswith("#")
    )
    assert example_values["AGENT_RUNTIME_API_KEY"] == ""
    assert example_values["POSTGRES_PASSWORD"] == ""
    with pytest.raises(ConfigurationError, match="AGENT_RUNTIME_API_KEY"):
        AppSettings.from_env(example_values)


def test_compose_requires_secrets_instead_of_supplying_public_defaults() -> None:
    compose_text = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "${AGENT_RUNTIME_API_KEY:?" in compose_text
    assert "${POSTGRES_PASSWORD:?" in compose_text


def test_ci_runs_quality_and_real_postgres_in_separate_jobs() -> None:
    workflow_text = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"quality", "postgres"}
    quality_commands = [step["run"] for step in jobs["quality"]["steps"] if "run" in step]
    assert "uv run ruff check ." in quality_commands
    assert "uv run ruff format --check ." in quality_commands
    assert "uv run mypy" in quality_commands
    assert "uv sync --locked --dev" in quality_commands
    assert 'uv run pytest -m "not postgres" -q' in quality_commands
    assert any("scripts.concurrency_baseline" in command for command in quality_commands)

    postgres_job = jobs["postgres"]
    assert postgres_job["services"]["postgres"]["image"] == "postgres:17-bookworm"
    assert "--health-cmd" in postgres_job["services"]["postgres"]["options"]
    postgres_commands = [step["run"] for step in postgres_job["steps"] if "run" in step]
    assert "uv run alembic upgrade head" in postgres_commands
    assert "uv run pytest -m postgres -q" in postgres_commands
    assert postgres_commands.index("uv run alembic upgrade head") < postgres_commands.index(
        "uv run pytest -m postgres -q"
    )
    assert "TEST_DATABASE_URL" in postgres_job["env"]
    assert "|| true" not in workflow_text
    assert "continue-on-error" not in workflow_text


def test_stage_one_docs_contain_required_diagrams_and_recovery_boundaries() -> None:
    architecture = (PROJECT_ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "docs/recovery-runbook.md").read_text(encoding="utf-8")
    acceptance = (PROJECT_ROOT / "docs/stage-one-acceptance.md").read_text(encoding="utf-8")

    assert architecture.count("```mermaid") >= 3
    assert "stateDiagram-v2" in architecture
    assert "erDiagram" in architecture
    assert "不要用 `docker compose down -v`" in runbook
    assert "重启 API 会丢失队列引用" in runbook
    assert "公开任务 GET / list" in architecture
    assert "公开 health/live" in architecture
    assert "MetricsRegistry" in architecture
    assert "Client --> Auth" not in architecture
    assert "Commands --> Runtime" not in architecture
    assert "Public -->" not in architecture
    for scenario in (
        "正常创建并完成",
        "重复幂等键",
        "取消运行任务",
        "超时和自动重试",
        "强制结束 Worker",
        "重启 Worker 恢复",
        "SSE 和日志解释",
    ):
        assert scenario in acceptance
    assert "真正跨进程 PostgreSQL 恢复仍等待第四周 SQL" in acceptance
