"""本地部署与安装包约束。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_is_loopback_only_and_has_no_old_runtime() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:${GUIDE_PORT:-8443}:8443" in compose
    assert compose.count("    ports:") == 1 and "  worker:" not in compose
    assert "guide-postgres-data" in compose
    assert '"/tls/service-key.pem"' in compose and '"--no-proxy-headers"' in compose
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in docker and "worker_main" not in docker


def test_installer_leaves_existing_config_and_does_not_package_private_key() -> None:
    installer = (ROOT / "scripts/desktop_installer.iss").read_text(encoding="utf-8")
    assert 'DestName: "client.toml"; Flags: onlyifdoesntexist uninsneveruninstall' in installer
    assert 'DestName: "service-ca.pem"; Flags: onlyifdoesntexist uninsneveruninstall' in installer
    assert "service-key" not in installer and "LLM_API_KEY" not in installer
