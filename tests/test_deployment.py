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
