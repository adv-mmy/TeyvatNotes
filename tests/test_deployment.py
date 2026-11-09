"""攻略 PostgreSQL 部署与 CI 配置的合同。"""

import json
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_docker_image_uses_the_guide_cli() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = json.loads(
        next(
            line.removeprefix("CMD ") for line in dockerfile.splitlines() if line.startswith("CMD ")
        )
    )

    assert "USER 10001:10001" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert command == ["python", "-m", "teyvatnotes.knowledge.cli", "--help"]


def test_compose_has_local_persistent_postgres_and_explicit_migration() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"postgres", "migrate"}
    assert services["postgres"]["ports"] == ["127.0.0.1:${POSTGRES_PORT:-55432}:5432"]
    assert services["postgres"]["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["migrate"]["read_only"] is True
    assert services["migrate"]["user"] == "10001:10001"
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
    assert example_values["LLM_API_KEY"] == ""
    assert example_values["DATABASE_URL"] == ""
    assert example_values["POSTGRES_PASSWORD"] == ""


def test_compose_requires_secrets_instead_of_supplying_public_defaults() -> None:
    compose_text = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

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
    assert "uv run mypy --platform win32" in quality_commands
    assert "uv sync --locked --dev" in quality_commands
    assert 'uv run pytest -m "not postgres" -q' in quality_commands

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
